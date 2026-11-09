"""本地 RAG：knowledge.json 默认库 + 数据库已发布数据集，分路召回。

三路混合召回（全部本地离线，无联网 API）：
- 词法通道：字符 bigram + 英文整词的查询覆盖率，负责精确字面命中；
- 稀疏通道：BM25（rank_bm25 + jieba 词级分词，k1=1.5、b=0.75），负责术语权重与长度惩罚；
- 稠密通道：本地 bge-small-zh-v1.5（transformers，CLS pooling + L2 归一化）余弦相似度；
- 融合：三路各自排序后 RRF(k=60，可配置) 融合并归一化；候选必须有词面重合（词法或 BM25 命中），
  纯向量相似不单独入选，避免语义漂移把无关长文拉进来。
- 降级：模型目录缺失 / 加载失败 / 推理抛错时，自动退回「词法 + BM25」，
  并在返回体 fusion.channels 与 embedding.status 中标注，不静默降级、不抛 500。
- 比较型问题（A和B的区别 / A还是B / 哪个更…）先做确定性查询拆解，子查询分别检索，
  三路 RRF 之后再按「概念覆盖度 → 融合分」二级重排。
两路索引各自按「默认库 3 条 / 用户数据集 2 条」配额出结果，避免长文本噪声淹没常识库。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import get_settings

settings = get_settings()


_DEFAULT_KNOWLEDGE = [
    {"id": "k1", "topic": "股票", "content": "股票是股份公司发行的所有权凭证，代表持有人对公司的部分所有权。A股交易以人民币计价，T+1 结算。"},
    {"id": "k2", "topic": "基金", "content": "基金是投资者共同出资交给基金公司管理的集合投资工具，分为货币基金、债券基金、股票基金、混合基金等。"},
    {"id": "k3", "topic": "ETF", "content": "ETF（交易所交易基金）是可在证券交易所买卖的指数基金，费用低、透明度高。适合被动投资策略。"},
    {"id": "k4", "topic": "风险收益", "content": "风险和收益正相关。高收益往往伴随高波动，投资者应根据自己的风险承受能力选择资产。"},
    {"id": "k5", "topic": "分散投资", "content": "不要把鸡蛋放在一个篮子里。建议至少 5-10 只标的，跨行业、跨资产类别配置。"},
    {"id": "k6", "topic": "止损", "content": "止损是保护本金的重要手段。常见策略有固定比例（如 -8%）、均线跌破、ATR 倍数等。"},
    {"id": "k7", "topic": "定投", "content": "定期定额投资可平滑市场波动，适合长期（3-5 年以上）积累财富的场景。"},
    {"id": "k8", "topic": "K线", "content": "K线图又称蜡烛图，显示一段时间内的开盘、收盘、最高、最低四个价格。阳线代表上涨，阴线代表下跌。"},
    {"id": "k9", "topic": "估值指标", "content": "PE = 股价/每股收益，越低越便宜；PB = 股价/每股净资产；ROE = 净利润/净资产，衡量盈利能力。"},
    {"id": "k10", "topic": "价值投资", "content": "价值投资以低于内在价值的价格买入优质公司股票，长期持有等待价值回归。代表人物：巴菲特。"},
    {"id": "k11", "topic": "A股涨跌幅限制", "content": "沪深主板 ±10%；创业板（300/301）与科创板（688）±20%；北交所（4/8/920 开头）±30%；主板 ST/*ST 为 ±5%。新股上市首日不设涨跌幅限制。"},
    {"id": "k12", "topic": "A股交易时间", "content": "集合竞价 9:15-9:25，连续竞价 9:30-11:30 与 13:00-15:00；科创板、创业板另有 15:00-15:30 盘后固定价格交易。法定节假日与周末休市。"},
    {"id": "k13", "topic": "T+1 交易制度", "content": "A股实行 T+1：当日买入的股票次日才能卖出；卖出所得资金当日可继续买入，但需 T+1 才能提现。融资融券账户规则不同。"},
    {"id": "k14", "topic": "最小交易单位", "content": "主板与创业板买入须为 100 股（1 手）的整数倍；科创板最小 1 股。卖出时不足 100 股的零股可一次性卖出。"},
    {"id": "k15", "topic": "A股交易费用", "content": "佣金约为成交额的万 2.5 至万 3（各券商不同，通常单笔最低 5 元）；印花税 0.05% 由卖方单边缴纳；沪深过户费 0.001%。"},
    {"id": "k16", "topic": "涨跌停与成交", "content": "涨停价买盘拥挤时可能买不到，跌停价卖盘拥挤时可能卖不出。判断能否成交应结合封单量与换手率，而不是只看是否触及涨跌停。"},
    {"id": "k17", "topic": "北交所规则", "content": "北交所代码以 4、8、920 开头，涨跌幅 ±30%；投资者需满足适当性要求（如 50 万元资产与 2 年交易经验），交易机制与沪深略有差异。"},
]

_KNOWLEDGE_FILE = os.path.join(os.path.dirname(__file__), "knowledge.json")


def _load_knowledge() -> List[Dict[str, str]]:
    if os.path.exists(_KNOWLEDGE_FILE):
        try:
            with open(_KNOWLEDGE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    with open(_KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
        json.dump(_DEFAULT_KNOWLEDGE, f, ensure_ascii=False, indent=2)
    return _DEFAULT_KNOWLEDGE


async def _load_user_published_knowledge() -> List[Dict[str, str]]:
    """从数据库取 is_published=True 的数据集样本，转成知识条目。"""
    try:
        from ..database import async_session
        from ..models.dataset import Dataset, DatasetSample
        from sqlalchemy import select
    except Exception:
        return []

    docs = []
    async with async_session() as session:
        # 先查已发布的数据集（限 10 个）
        datasets = (await session.execute(
            select(Dataset).where(Dataset.is_published.is_(True)).limit(10)
        )).scalars().all()
        if not datasets:
            return []
        ds_ids = [d.id for d in datasets]

        samples = (await session.execute(
            select(DatasetSample).where(DatasetSample.dataset_id.in_(ds_ids)).limit(500)
        )).scalars().all()

        for s in samples:
            # 每条样本 → 一条知识条目
            topic = s.topic or s.instruction or ""
            content = s.content or s.output or ""

            # SFT messages 风格：提取 user→assistant 对当知识
            if not content and s.messages_json:
                try:
                    msgs = json.loads(s.messages_json)
                    # 取最后一个 assistant 回复当 content
                    for m in reversed(msgs):
                        if m.get("role") == "assistant":
                            content = m.get("content", "")
                            break
                    # 取 user 提问当 topic
                    if not topic:
                        for m in msgs:
                            if m.get("role") == "user":
                                topic = m.get("content", "")[:60]
                                break
                except Exception:
                    pass

            # DPO 风格：chosen 当知识（偏好回答）
            if not content and s.chosen:
                content = s.chosen
                topic = topic or s.prompt or ""

            if content:
                docs.append({
                    "id": f"user_{s.id}",
                    "topic": topic[:120] or s.dataset_id[:8],
                    "content": content,
                    "source": "user_dataset",
                    "dataset_id": s.dataset_id,
                })
    return docs


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CN_RE = re.compile(r"[\u4e00-\u9fff]+")
_STOP = {
    "的", "了", "吗", "呢", "是", "和", "有", "在", "我", "你", "他", "它",
    "这", "那", "什", "么", "怎", "样", "如", "何", "个", "与", "或", "都",
    "要", "会", "能", "请", "问", "一", "下", "哪", "些", "为", "对",
}

_BM25_K1 = 1.5
_BM25_B = 0.75
_INDEX_TTL_SECONDS = 600
_INDEX_CACHE: Dict[str, Tuple[float, "_DocIndex"]] = {}

# 向量通道状态：uninitialized / ready / unavailable（进程级，首次使用时懒加载）
_EMBED_STATE: Dict[str, Any] = {
    "status": "uninitialized", "model": None, "tokenizer": None, "dim": 0, "error": "",
}
_EMBED_LOCK = threading.Lock()
_JIEBA_READY = False


def _tokenize(text: str) -> List[str]:
    """中文按字符 bigram、英文/数字按整词切分：词法通道用，兼顾无空格中文与 ETF/PE 这类术语。"""
    text = (text or "").lower()
    tokens = _TOKEN_RE.findall(text)
    for run in _CN_RE.findall(text):
        if len(run) == 1:
            if run not in _STOP:
                tokens.append(run)
            continue
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _jieba_tokenize(text: str) -> List[str]:
    """BM25 通道分词：jieba 词级切分并丢弃单字虚词；jieba 不可用时退回 bigram。"""
    global _JIEBA_READY
    try:
        import jieba
        if not _JIEBA_READY:
            jieba.setLogLevel(60)  # 关掉首次加载的构建日志
            _JIEBA_READY = True
        words = jieba.lcut((text or "").lower())
    except Exception:
        return _tokenize(text)
    out = []
    for w in words:
        w = w.strip()
        if not w or w in _STOP:
            continue
        if len(w) == 1 and _CN_RE.fullmatch(w):
            continue
        out.append(w)
    return out or _tokenize(text)


def _ensure_embed_model() -> Dict[str, Any]:
    """懒加载本地向量模型。任何失败都只置状态、不抛异常，由调用方降级。"""
    if not settings.RAG_EMBED_ENABLED:
        _EMBED_STATE.update(status="unavailable", error="RAG_EMBED_ENABLED=false")
        return _EMBED_STATE
    if _EMBED_STATE["status"] != "uninitialized":
        return _EMBED_STATE
    with _EMBED_LOCK:
        if _EMBED_STATE["status"] != "uninitialized":
            return _EMBED_STATE
        path = settings.RAG_EMBED_MODEL_PATH
        try:
            if not os.path.isdir(path):
                raise FileNotFoundError(f"模型目录不存在: {path}")
            from transformers import AutoModel, AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(path)
            model = AutoModel.from_pretrained(path)
            model.eval()
            _EMBED_STATE.update(
                status="ready", model=model, tokenizer=tokenizer,
                dim=int(model.config.hidden_size), error="",
            )
        except Exception as exc:  # noqa: BLE001 - 降级路径必须吃掉所有异常
            _EMBED_STATE.update(status="unavailable", error=f"{type(exc).__name__}: {exc}")
    return _EMBED_STATE


def _embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    """bge 官方用法：取 CLS 向量 + L2 归一化。失败返回 None（调用方降级）。"""
    state = _ensure_embed_model()
    if state["status"] != "ready" or not texts:
        return None
    try:
        import torch
        import torch.nn.functional as F

        tok, model = state["tokenizer"], state["model"]
        out: List[List[float]] = []
        with torch.no_grad():
            for i in range(0, len(texts), 16):
                enc = tok(texts[i:i + 16], padding=True, truncation=True,
                          max_length=512, return_tensors="pt")
                hidden = model(**enc).last_hidden_state[:, 0]
                hidden = F.normalize(hidden, p=2, dim=1)
                out.extend(hidden.cpu().tolist())
        return out
    except Exception as exc:  # noqa: BLE001
        _EMBED_STATE.update(status="unavailable", error=f"embed_failed: {type(exc).__name__}: {exc}")
        return None


class _DocIndex:
    """三路召回索引：词法 bigram 集合 / BM25(jieba) / bge 向量（模型可用时）。"""

    def __init__(self, docs: List[Dict[str, str]]):
        self.docs = docs
        self.n = len(docs)
        texts = [f"{d.get('topic', '')} {d.get('content', '')[:800]}" for d in docs]

        # 词法通道：bigram 词集合
        self.lex_sets = [set(_tokenize(t)) for t in texts]

        # BM25 通道：jieba 词序列
        self.bm25_tokens = [_jieba_tokenize(t) for t in texts]
        self.bm25 = None
        if self.bm25_tokens:
            try:
                from rank_bm25 import BM25Okapi

                self.bm25 = BM25Okapi(self.bm25_tokens, k1=_BM25_K1, b=_BM25_B)
            except Exception as exc:  # noqa: BLE001
                _EMBED_STATE.setdefault("bm25_error", f"{type(exc).__name__}: {exc}")
                self.bm25 = None

        # 向量通道：主题 + 正文前 512 字（模型自身会截断到 512 token）
        self.embeddings = _embed_texts(
            [f"{d.get('topic', '')} {d.get('content', '')[:512]}" for d in docs]
        )

    @property
    def channels(self) -> List[str]:
        return ["lexical", "bm25"] + (["vector"] if self.embeddings else [])

    def channel_scores(self, query: str) -> Dict[str, List[float]]:
        """三路原始分：词法覆盖率 0~1、BM25 >=0、余弦 0~1。"""
        q_lex = set(_tokenize(query))
        lexical = [
            (len(q_lex & s) / len(q_lex)) if q_lex and s else 0.0 for s in self.lex_sets
        ]

        bm25 = [0.0] * self.n
        if self.bm25 is not None:
            try:
                bm25 = [max(float(x), 0.0) for x in self.bm25.get_scores(_jieba_tokenize(query))]
            except Exception:  # noqa: BLE001
                bm25 = [0.0] * self.n

        vector = [0.0] * self.n
        if self.embeddings:
            q_emb = _embed_texts([query])
            if q_emb:
                qv = q_emb[0]
                vector = [max(sum(a * b for a, b in zip(qv, dv)), 0.0) for dv in self.embeddings]
        return {"lexical": lexical, "bm25": bm25, "vector": vector}


def _get_index(docs: List[Dict[str, str]]) -> _DocIndex:
    """按「向量模型状态 + 文档数 + 总字数」缓存，避免每次请求重建索引。"""
    sig = f"{len(docs)}:{sum(len(d.get('content', '')) for d in docs)}"
    now = time.time()
    hit = _INDEX_CACHE.get(f"{_EMBED_STATE['status']}:{sig}")
    if hit and now - hit[0] < _INDEX_TTL_SECONDS:
        return hit[1]

    idx = _DocIndex(docs)
    # 模型可能在构建期间才从 uninitialized 变成 ready，缓存 key 要用最终状态，
    # 否则下一次请求会因状态变化再重建一遍（多付一次 4s 建索引）
    key = f"{_EMBED_STATE['status']}:{sig}"
    _INDEX_CACHE[key] = (now, idx)
    # 最多留 8 份，防止数据集频繁变动导致缓存膨胀
    if len(_INDEX_CACHE) > 8:
        for k in sorted(_INDEX_CACHE, key=lambda x: _INDEX_CACHE[x][0])[: len(_INDEX_CACHE) - 8]:
            _INDEX_CACHE.pop(k, None)
    return idx


_COMPARE_SPLIT = re.compile(r"(?:和|与|跟|及|vs\.?|还是|、|以及|对比|比较)", re.I)
_COMPARE_HINT = re.compile(r"区别|差异|不同|对比|比较|哪个|哪一个|还是|分别|各自|优势|劣势|关系")
_Q_TAIL = re.compile(
    r"(有什么区别|区别是什么|有什么不同|哪里不同|的关系|关系|的区别|区别|的差异|差异|"
    r"分别是什么|分别是多少|是多少|分别|各自|哪个更.*|哪一个.*|哪个.*|相比.*|"
    r"更适合.*|比较适合.*|适合.*)$"
)
# 共享属性里出现这些词，说明是「更/最 + 形容词」的比较语气，不是真正要比较的属性
_ATTR_REJECT = re.compile(r"[更最]|适合|值得|应该|可以|能")


def _shared_attribute(text: str) -> str:
    """从「哪个X」里抽被比较的共享属性，例如「涨跌幅」。

    只接受短的属性词；「更能反映估值」这类比较性短语一律丢弃，
    否则会被当成属性拼进每个子查询，反而引入噪声。
    """
    m = re.search(r"哪(?:个|一个|种)?(.+)$", text)
    if not m:
        return ""
    attr = re.sub(r"[大高好低多小差强优]$", "", m.group(1)).strip()
    if not attr or len(attr) > 8 or _ATTR_REJECT.search(attr):
        return ""
    return attr


def _decompose(query: str) -> Dict[str, Any]:
    """比较型问题拆解：返回子查询与待覆盖概念。

    只做确定性的连接词切分（和/与/跟/还是/、）+ 疑问尾巴剥离，
    不额外调用 LLM，保证 RAG 检索的延迟与成本可控。
    """
    q = query.strip()
    single = {"is_comparison": False, "sub_queries": [q], "concepts": [], "attribute": ""}
    if not _COMPARE_HINT.search(q) or not _COMPARE_SPLIT.search(q):
        return single

    parts = [p.strip(" ，,。?？;；") for p in _COMPARE_SPLIT.split(q)]
    parts = [p for p in parts if p]
    concepts: List[str] = []
    attribute = ""
    for i, p in enumerate(parts):
        # 首段可能带「哪个更适合…，」这种提问前缀
        if "哪" in p and ("，" in p or "," in p):
            p = re.split(r"[，,]", p)[-1]
        # 属性必须在剥尾巴之前抽：_Q_TAIL 里的「哪个.*」会把属性一起吃掉
        if i == len(parts) - 1:
            attribute = _shared_attribute(p)
        p = _Q_TAIL.sub("", p).strip()
        if i == len(parts) - 1 and attribute:
            p = re.sub(r"哪(?:个|一个|种)?.*$", "", p).strip()
        # 「科创板的涨跌幅」这类把属性黏在概念上的，取「的」前面的主体
        if "的" in p:
            p = p.split("的")[0].strip()
        if p:
            concepts.append(p)

    if len(concepts) < 2:
        return single
    sub_queries = [f"{c} {attribute}".strip() for c in concepts]
    return {
        "is_comparison": True,
        "sub_queries": sub_queries,
        "concepts": concepts,
        "attribute": attribute,
    }


def _rrf_fuse(score_lists: List[List[float]], k: int, n: int) -> List[float]:
    """Reciprocal Rank Fusion：每个通道按自身分数排名，累加 1/(k+rank)。

    只统计正分文档（0 分视为该通道未召回），返回未归一化的融合分。
    """
    fused = [0.0] * n
    for scores in score_lists:
        rank = 0
        for i in sorted(range(n), key=lambda j: scores[j], reverse=True):
            if scores[i] <= 0:
                break
            rank += 1
            fused[i] += 1.0 / (k + rank)
    return fused


def _normalize(values: List[float]) -> List[float]:
    """min-max 归一化到 0~1。

    RRF 原始分只差在名次，除以最大值会让所有候选挤在 0.9~1.0，
    min_score 就失去过滤意义；min-max 能把「最弱候选」拉到 0，
    门槛才真正表示「与最优候选的相对接近度」。
    只有一个候选时直接给 1.0（能过候选门槛说明确有词面重合）。
    """
    pos = [v for v in values if v > 0]
    if not pos:
        return [0.0] * len(values)
    lo, hi = min(pos), max(pos)
    if hi - lo <= 1e-12:
        return [1.0 if v > 0 else 0.0 for v in values]
    return [(v - lo) / (hi - lo) if v > 0 else 0.0 for v in values]


def _rank_single(idx: _DocIndex, query: str, rrf_k: int = 60) -> List[Dict[str, Any]]:
    """单查询：三路（词法 / BM25 / 向量）各自排名 → RRF 融合 → 归一化。

    候选门槛：词法或 BM25 必须有词面重合，纯向量相似不单独入选，
    避免语义漂移把无关长文拉进上下文。
    """
    scores = idx.channel_scores(query)
    fused = _rrf_fuse([scores[c] for c in idx.channels], rrf_k, idx.n)
    fused = [
        f if (scores["lexical"][i] > 0 or scores["bm25"][i] > 0) else 0.0
        for i, f in enumerate(fused)
    ]
    norm = _normalize(fused)

    rows = []
    for i, d in enumerate(idx.docs):
        if norm[i] <= 0:
            continue
        rows.append({
            "doc": d,
            "score": norm[i],
            "lexical": scores["lexical"][i],
            "bm25": scores["bm25"][i],
            "vector": scores["vector"][i] if "vector" in idx.channels else None,
            "coverage": None,
        })
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows


_CONCEPT_SUFFIX = ("限制", "规则", "制度", "标准", "机制", "规定")


def _aliases(concept: str) -> List[str]:
    """概念别名：去掉「限制/规则/制度」这类后缀，避免「涨跌停限制」匹配不到「涨跌停」。"""
    c = concept.lower()
    out = [c]
    for suf in _CONCEPT_SUFFIX:
        if c.endswith(suf) and len(c) > len(suf) + 1:
            out.append(c[: -len(suf)])
    return out


def _multi_query_rank(
    idx: _DocIndex, plan: Dict[str, Any], rrf_k: int = 60
) -> List[Dict[str, Any]]:
    """比较型：子查询的三路分数列表投入同一个 RRF 池，再按概念覆盖度重排。"""
    lists: List[List[float]] = []
    lexical_hit = [0] * idx.n
    for sub in plan["sub_queries"]:
        scores = idx.channel_scores(sub)
        for c in idx.channels:
            lists.append(scores[c])
        for i in range(idx.n):
            if scores["lexical"][i] > 0 or scores["bm25"][i] > 0:
                lexical_hit[i] += 1

    fused = _rrf_fuse(lists, rrf_k, idx.n)
    fused = [f if lexical_hit[i] > 0 else 0.0 for i, f in enumerate(fused)]
    norm = _normalize(fused)

    concepts = [c for c in plan.get("concepts", []) if c]
    alias_groups = [[a for a in _aliases(c) if a] for c in concepts]
    rows = []
    for i, d in enumerate(idx.docs):
        if norm[i] <= 0:
            continue
        # 只在前 300 字里数概念覆盖，避免长文档靠篇幅“刷”覆盖度
        text = f"{d.get('topic', '')} {d.get('content', '')[:300]}".lower()
        hit = sum(1 for group in alias_groups if any(a in text for a in group))
        coverage = hit / len(alias_groups) if alias_groups else 0.0
        rows.append({
            "doc": d,
            "score": 0.7 * norm[i] + 0.3 * coverage,
            "lexical": None,
            "bm25": None,
            "vector": None,
            "coverage": coverage,
        })
    # rerank：先保证比较对象两侧都有覆盖，再比融合分
    rows.sort(key=lambda x: (x["coverage"], x["score"]), reverse=True)
    return rows


async def retrieve(
    query: str,
    top_k: int = 5,
    min_score: float = 0.25,
    default_quota: int = 3,
    user_quota: int = 2,
    rrf_k: Optional[int] = None,
) -> Dict[str, Any]:
    """三路混合召回：词法(bigram) + BM25(jieba) + 向量(bge-small-zh)，RRF 融合。

    打分在**合并语料**上统一归一（RRF 归一化后 0~1），配额只决定「每路取几条」，不参与打分。
    向量模型不可用时自动退回两路，并在 fusion.degraded / embedding.status 标注。

    min_score: 融合分门槛（归一化后的相对序位分，不是绝对相关度）
    rrf_k: RRF 平滑常数，默认取 settings.RAG_RRF_K
    """
    default_docs = _load_knowledge()
    user_docs = await _load_user_published_knowledge()
    all_docs = user_docs + default_docs

    idx = await asyncio.to_thread(_get_index, all_docs)
    k = int(rrf_k if rrf_k is not None else settings.RAG_RRF_K)
    plan = _decompose(query)

    def _rank_all() -> List[Dict[str, Any]]:
        # 查询侧还要跑一次向量推理，一并放到线程里，别阻塞事件循环
        if plan["is_comparison"]:
            return _multi_query_rank(idx, plan, k)
        return _rank_single(idx, query, k)

    ranked = await asyncio.to_thread(_rank_all)
    ranked = [x for x in ranked if x["score"] >= min_score]

    def _is_user(d: Dict[str, str]) -> bool:
        return d.get("source") == "user_dataset"

    picked = [x for x in ranked if _is_user(x["doc"])][:max(0, user_quota)]
    picked += [x for x in ranked if not _is_user(x["doc"])][:max(0, default_quota)]

    # 某一路命中不足时，用另一路按融合分补满 top_k
    if len(picked) < top_k:
        seen = {id(x["doc"]) for x in picked}
        for x in ranked:
            if len(picked) >= top_k:
                break
            if id(x["doc"]) not in seen:
                picked.append(x)
                seen.add(id(x["doc"]))

    # 比较型保持「先覆盖度、再融合分」的重排顺序，单查询仍按融合分
    if plan["is_comparison"]:
        picked = sorted(picked, key=lambda x: (x["coverage"] or 0.0, x["score"]), reverse=True)[:top_k]
    else:
        picked = sorted(picked, key=lambda x: x["score"], reverse=True)[:top_k]

    sources = []
    for x in picked:
        d = x["doc"]
        item = {
            "id": d.get("id"),
            "topic": d.get("topic"),
            "score": round(x["score"], 2),
            "content": d.get("content", "")[:200],
            "source": d.get("source", "default"),
        }
        if x["coverage"] is not None:
            item["coverage"] = round(x["coverage"], 2)
        else:
            item["lexical"] = round(x["lexical"], 2)
            item["bm25"] = round(x["bm25"], 2)
            item["vector"] = round(x["vector"], 3) if x["vector"] is not None else None
        sources.append(item)

    context = "\n\n".join(f"[{i+1}] {x['doc']['topic']}：{x['doc']['content']}" for i, x in enumerate(picked)) if picked else ""
    channels = idx.channels
    degraded = "vector" not in channels
    return {
        "sources": sources,
        "context": context,
        "user_knowledge_count": len(user_docs),
        "default_knowledge_count": len(default_docs),
        "quotas": {"default": default_quota, "user_dataset": user_quota},
        "fusion": {
            "method": "+".join(channels) + "_rrf" + ("+coverage" if plan["is_comparison"] else ""),
            "channels": channels,
            "rrf_k": k,
            "min_score": min_score,
            "degraded": degraded,
            "degrade_reason": _EMBED_STATE["error"] if degraded else "",
        },
        "embedding": {
            "model": os.path.basename(settings.RAG_EMBED_MODEL_PATH.rstrip("/\\")) or "unknown",
            "dim": _EMBED_STATE["dim"],
            "status": _EMBED_STATE["status"],
        },
        "query_plan": plan,
    }
