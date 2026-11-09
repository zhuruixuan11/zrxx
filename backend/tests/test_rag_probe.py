"""RAG 检索回归：11 条探针命中 + 比较型拆解/rerank + 分路配额。

基线固定为仓库内置的 `_DEFAULT_KNOWLEDGE`（不打运行时生成的 knowledge.json），
用户数据集默认置空 —— 否则结果会随本地 hengce.db 里发布了什么而漂移。
"""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent import rag
from app.database import Base
from app.models.dataset import Dataset, DatasetSample

# 自动 fixture 会把用户库打成空，这里先留一份真实现，配额用例要还原它
_REAL_LOAD_USER_DOCS = rag._load_user_published_knowledge

# 11 条探针：(问题, 期望命中的知识条目)
PROBES = [
    ("A股交易费用有哪些", "k15"),
    ("佣金最低多少", "k15"),
    ("印花税怎么收", "k15"),
    ("ETF和股票有什么区别", "k3"),
    ("T+1和涨跌停的关系", "k13"),
    ("北交所和科创板哪个涨跌幅大", "k11"),
    ("如果我买了股票当天能卖吗", "k13"),
    ("PE和PB哪个更能反映估值", "k9"),
    ("止损应该怎么设置", "k6"),
    ("定投适合什么人", "k7"),
    ("科创板最小买入多少股", "k14"),
]


@pytest.fixture(autouse=True)
def _pin_knowledge(monkeypatch):
    """固定默认库、清空用户库与索引缓存，保证每个用例的检索基线一致。

    测试里关掉向量通道：一是避免每次 pytest 都加载 90MB 模型（约 1 分钟），
    二是让用例在没下载模型的环境里也能跑。三路融合的召回数字由
    `scripts/eval_rag_probes.py` 实跑报告，这里只锁两路降级架构的回归基线。
    """
    monkeypatch.setattr(rag, "_load_knowledge", lambda: rag._DEFAULT_KNOWLEDGE)
    monkeypatch.setattr(rag.settings, "RAG_EMBED_ENABLED", False)
    rag._EMBED_STATE.update(status="uninitialized", model=None, tokenizer=None, dim=0, error="")

    async def _no_user_docs():
        return []

    monkeypatch.setattr(rag, "_load_user_published_knowledge", _no_user_docs)
    rag._INDEX_CACHE.clear()
    yield
    rag._INDEX_CACHE.clear()


# ---------------- 查询拆解 ----------------

@pytest.mark.parametrize("query,concepts", [
    ("T+1和涨跌停的关系", ["T+1", "涨跌停"]),
    ("ETF和股票有什么区别", ["ETF", "股票"]),
    ("股票和基金的区别", ["股票", "基金"]),
    ("北交所和科创板哪个涨跌幅大", ["北交所", "科创板"]),
    ("定投还是一次性买入更适合长期投资", ["定投", "一次性买入"]),
    ("涨跌停限制和交易费用分别是什么", ["涨跌停限制", "交易费用"]),
])
def test_decompose_extracts_both_concepts(query, concepts):
    plan = rag._decompose(query)
    assert plan["is_comparison"] is True
    assert plan["concepts"] == concepts
    # 每个概念都要变成一个子查询，否则侧边概念召不回来
    assert len(plan["sub_queries"]) == len(concepts)


def test_decompose_peels_shared_attribute():
    plan = rag._decompose("北交所和科创板哪个涨跌幅大")
    assert plan["attribute"] == "涨跌幅"
    assert plan["sub_queries"] == ["北交所 涨跌幅", "科创板 涨跌幅"]


def test_decompose_does_not_touch_single_queries():
    for query in ("科创板最小买入多少股", "印花税怎么收", "止损应该怎么设置"):
        plan = rag._decompose(query)
        assert plan["is_comparison"] is False
        assert plan["sub_queries"] == [query]
        assert plan["concepts"] == []


def test_decompose_requires_two_concepts():
    # 有比较语气词但切不出两侧概念 → 必须退回单查询，不能空跑 RRF
    plan = rag._decompose("哪个更适合长期投资")
    assert plan["is_comparison"] is False


# ---------------- 别名与覆盖度 rerank ----------------

def test_concept_suffix_alias_bridges_wording_gap():
    assert rag._aliases("涨跌停限制") == ["涨跌停限制", "涨跌停"]
    assert rag._aliases("交易费用") == ["交易费用"]


def test_coverage_rerank_is_monotonic():
    idx = rag._get_index(rag._DEFAULT_KNOWLEDGE)
    plan = rag._decompose("涨跌停限制和交易费用分别是什么")
    rows = rag._multi_query_rank(idx, plan)
    coverages = [r["coverage"] for r in rows]
    assert coverages == sorted(coverages, reverse=True)
    # 「涨跌停限制」应该能命中「涨跌停」条目，而不是因为缺后缀匹配全 0
    assert max(coverages) == pytest.approx(0.5)


def test_comparison_query_keeps_coverage_after_quota_merge():
    # 配额合并后不能再用纯 score 重排，否则覆盖度 0 的长文档会挤掉覆盖度高的条目
    result = asyncio.run(rag.retrieve("T+1和涨跌停的关系"))
    assert result["fusion"]["method"] == "lexical+bm25_rrf+coverage"
    assert result["query_plan"]["is_comparison"] is True
    assert result["sources"][0]["coverage"] > 0


# ---------------- 探针回归 ----------------

def test_all_probes_hit_top5():
    misses = []
    for query, expected in PROBES:
        result = asyncio.run(rag.retrieve(query))
        ids = [s["id"] for s in result["sources"]]
        if expected not in ids:
            misses.append((query, expected, ids))
    assert not misses, f"top5 未命中：{misses}"


def test_top1_hits_do_not_regress():
    # 这里是「词法 + BM25」两路降级架构的基线：8/11（向量通道在测试里关闭，见 fixture）。
    # 三路融合实测 10/11，但依赖本地模型，不能作为离线用例的硬门槛。
    # 已知未命中 top1 的三条：ETF和股票（k1 抢位）、T+1和涨跌停（k16）、当天能卖吗（k1）。
    top1 = 0
    for query, expected in PROBES:
        result = asyncio.run(rag.retrieve(query))
        sources = result["sources"]
        if sources and sources[0]["id"] == expected:
            top1 += 1
    assert top1 >= 8, f"两路降级 top1 命中退化到 {top1}/11"


def test_single_query_result_has_channel_scores():
    result = asyncio.run(rag.retrieve("印花税怎么收"))
    assert result["fusion"]["method"] == "lexical+bm25_rrf"
    assert result["query_plan"]["is_comparison"] is False
    src = result["sources"][0]
    assert "lexical" in src and "bm25" in src
    assert src["vector"] is None  # 向量通道已降级，字段保留但不给分数


# ---------------- RRF 融合 / 降级 / 空结果 ----------------

def test_rrf_fuse_is_stable_and_ignores_zero_scores():
    lists = [[0.9, 0.5, 0.0, 0.2], [0.1, 0.8, 0.0, 0.3]]
    first = rag._rrf_fuse(lists, 60, 4)
    second = rag._rrf_fuse(lists, 60, 4)
    assert first == second
    # 两路都是 0 分的文档不获得任何融合分
    assert first[2] == 0.0
    # 名次决定一切：doc1 在两路都是 1/2 名，doc0 是 1/3 名；doc3 是 2/3 名
    assert first[1] > first[0] > first[3] > first[2]


def test_degraded_path_reports_channels(monkeypatch):
    monkeypatch.setattr(rag.settings, "RAG_EMBED_ENABLED", False)
    rag._EMBED_STATE.update(status="uninitialized", model=None, tokenizer=None, dim=0, error="")
    rag._INDEX_CACHE.clear()

    result = asyncio.run(rag.retrieve("印花税怎么收"))
    assert result["fusion"]["channels"] == ["lexical", "bm25"]
    assert result["fusion"]["degraded"] is True
    assert result["fusion"]["method"] == "lexical+bm25_rrf"
    assert result["fusion"]["degrade_reason"]
    assert result["embedding"]["status"] == "unavailable"
    # 降级不能影响召回本身
    assert result["sources"][0]["id"] == "k15"


def test_missing_model_dir_degrades_without_crashing(monkeypatch):
    monkeypatch.setattr(rag.settings, "RAG_EMBED_ENABLED", True)
    monkeypatch.setattr(rag.settings, "RAG_EMBED_MODEL_PATH", "E:/hengce/models/__definitely_missing__")
    rag._EMBED_STATE.update(status="uninitialized", model=None, tokenizer=None, dim=0, error="")
    rag._INDEX_CACHE.clear()

    result = asyncio.run(rag.retrieve("印花税怎么收"))
    assert result["fusion"]["degraded"] is True
    assert "模型目录不存在" in result["fusion"]["degrade_reason"]
    assert result["embedding"]["status"] == "unavailable"
    assert result["embedding"]["dim"] == 0
    assert result["sources"][0]["id"] == "k15"


def test_empty_result_does_not_raise():
    # 与常识库没有任何词面重合 → 候选门槛全部不通过，返回空而不是抛异常
    result = asyncio.run(rag.retrieve("量子纠缠"))
    assert result["sources"] == []
    assert result["context"] == ""
    assert result["fusion"]["channels"] == ["lexical", "bm25"]


def test_vector_channel_participates_when_embeddings_present(monkeypatch):
    docs = [
        {"id": "v_related", "topic": "股票", "content": "股票是股份公司发行的所有权凭证。", "source": "default"},
        {"id": "v_other", "topic": "物理", "content": "量子纠缠是一种物理现象。", "source": "default"},
    ]
    idx = rag._DocIndex(docs)
    assert idx.channels == ["lexical", "bm25"]  # 模型不可用时的默认状态
    # 直接注入假向量，避免加载真模型
    idx.embeddings = [[1.0, 0.0], [0.0, 1.0]]
    monkeypatch.setattr(rag, "_embed_texts", lambda texts: [[1.0, 0.0]] * len(texts))

    assert idx.channels == ["lexical", "bm25", "vector"]
    scores = idx.channel_scores("股票")
    assert scores["vector"][0] > scores["vector"][1]


def test_vector_only_match_is_not_a_candidate(monkeypatch):
    docs = [
        {"id": "v_short", "topic": "印花税", "content": "印花税仅在卖出时单边收取。", "source": "default"},
        {"id": "v_vec_only", "topic": "物理", "content": "量子纠缠是一种物理现象。", "source": "default"},
    ]
    idx = rag._DocIndex(docs)
    # 把 v_vec_only 的向量打成与查询完全一致，但词法/BM25 都零命中
    idx.embeddings = [[0.0, 1.0], [1.0, 0.0]]
    monkeypatch.setattr(rag, "_embed_texts", lambda texts: [[1.0, 0.0]] * len(texts))

    ids = [r["doc"]["id"] for r in rag._rank_single(idx, "印花税怎么收")]
    assert "v_vec_only" not in ids
    assert ids[0] == "v_short"


# ---------------- 长度不构成奖励：长文本不能击穿 min_score ----------------

_FILLER = "公司定期报告披露经营情况说明与董事会决议公告内容，详见交易所网站。"
_SHORT = {
    "id": "s_short", "topic": "印花税",
    "content": "印花税仅在卖出时按成交额千分之一收取，由卖方单边承担。",
    "source": "default",
}


def _long_doc(repeat: int) -> dict:
    """一条只在开头提到印花税、后面全是无关填充的长文档。"""
    return {
        "id": f"s_long_{repeat}", "topic": "综合资料汇编",
        "content": "印花税" + _FILLER * repeat,
        "source": "default",
    }


def test_long_document_gets_no_length_bonus(monkeypatch):
    docs_short = [_SHORT, _long_doc(60)]
    docs_long = [_SHORT, _long_doc(240)]  # 同样内容，长度翻 4 倍

    stats = {}
    for label, docs in (("short", docs_short), ("long", docs_long)):
        rag._INDEX_CACHE.clear()
        idx = rag._get_index(docs)
        sc = idx.channel_scores("印花税怎么收")
        rows = {r["doc"]["id"]: r["score"] for r in rag._rank_single(idx, "印花税怎么收")}
        stats[label] = (sc, rows)

    sc_s, rows_s = stats["short"]
    sc_l, rows_l = stats["long"]
    # BM25 自带长度归一：篇幅翻 4 倍不会让同一篇文档得分更高
    assert sc_l["bm25"][1] <= sc_s["bm25"][1] + 1e-9
    # 词法覆盖率只会被稀释，不会因长度增加
    assert sc_l["lexical"][1] <= sc_s["lexical"][1] + 1e-12
    # 排序上短而准的条目始终在长文之前
    assert next(iter(rows_s)) == "s_short"
    assert next(iter(rows_l)) == "s_short"


def test_long_document_cannot_cross_min_score(monkeypatch):
    # 用接近真实规模的语料（多篇相关短条目 + 一篇长而跑题的），避免 2 篇文档时
    # min-max 归一化退化成「只有 0 和 1」两个值，测不出门槛的实际效果。
    docs = [
        _SHORT,
        {"id": "s_commission", "topic": "佣金",
         "content": "佣金由券商收取，单笔最低 5 元，买卖双向。", "source": "default"},
        {"id": "s_transfer", "topic": "过户费",
         "content": "过户费按成交金额万分之零点一收取，沪深双边。", "source": "default"},
        {"id": "s_tplus", "topic": "T+1",
         "content": "A股实行 T+1，当日买入次日可卖。", "source": "default"},
        _long_doc(120),
    ]
    monkeypatch.setattr(rag, "_load_knowledge", lambda: docs)
    rag._INDEX_CACHE.clear()

    idx = rag._get_index(docs)
    rows = {r["doc"]["id"]: r["score"] for r in rag._rank_single(idx, "印花税怎么收")}
    assert rows["s_short"] >= 0.9              # 短而准的条目接近满分
    assert rows.get("s_long_120", 0.0) < 0.25  # 长而跑题的条目够不到默认门槛

    result = asyncio.run(rag.retrieve("印花税怎么收"))
    ids = [s["id"] for s in result["sources"]]
    assert ids[0] == "s_short"
    assert "s_long_120" not in ids


# ---------------- 分路配额 ----------------

def test_user_dataset_quota_is_respected(monkeypatch):
    # 这个用例要跑真实的用户库读取逻辑，先还原自动 fixture 打掉的那个函数
    monkeypatch.setattr(rag, "_load_user_published_knowledge", _REAL_LOAD_USER_DOCS)

    async def _run():
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # 发布一个「交易费用」主题的数据集，5 条样本都在讲费用，故意比常识库更“像”答案
        async with Session() as db:
            ds = Dataset(name="费用知识", type="raw_knowledge", is_published=True)
            db.add(ds)
            await db.flush()
            for i in range(5):
                db.add(DatasetSample(
                    dataset_id=ds.id,
                    topic=f"交易费用补充{i}",
                    content=(
                        "A股交易费用包含佣金、印花税与过户费。佣金由券商收取，"
                        "通常单笔最低 5 元；印花税由卖方单边缴纳；过户费沪深双边收取。"
                    ),
                ))
            await db.commit()

        monkeypatch.setattr("app.database.async_session", Session)
        result = await rag.retrieve("A股交易费用有哪些")
        await engine.dispose()
        return result

    result = asyncio.run(_run())
    sources = result["sources"]
    user_hits = [s for s in sources if s["source"] == "user_dataset"]
    default_hits = [s for s in sources if s["source"] != "user_dataset"]

    assert result["user_knowledge_count"] == 5
    assert result["quotas"] == {"default": 3, "user_dataset": 2}
    assert len(user_hits) <= 2, "用户数据集不得超过 2 条配额"
    assert len(default_hits) <= 3, "默认知识库不得超过 3 条配额"
    assert len(sources) <= 5
    # 两路都得有代表，说明不是被单一来源独占
    assert user_hits and default_hits
