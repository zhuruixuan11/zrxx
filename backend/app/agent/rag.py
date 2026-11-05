"""简单 RAG：本地 knowledge.json + 数据库里已发布的数据集，两路合并检索。

检索优先级：用户发布的数据集样本 > knowledge.json 默认库。
打分用 TF-IDF 风格关键词 + bigram 匹配（sentence-transformers 未装时）。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List


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


def keyword_score(query: str, doc: Dict[str, str]) -> float:
    """TF-IDF 风格简单打分 + bigram 字符匹配（中文友好）。"""
    q = query.lower()
    content = (doc.get("topic", "") + " " + doc.get("content", "")).lower()
    terms = [t for t in q.split() if len(t) > 1]
    score = 0.0
    for t in terms:
        if t in content:
            score += 1.0
            if t in doc.get("topic", "").lower():
                score += 2.0
    # bigram 字符匹配（无空格中文必备）
    for i in range(len(q) - 1):
        bigram = q[i:i + 2]
        if bigram in content:
            score += 0.5
    # 内容长度奖励（长一点的答案更有价值，上限 10）
    score += min(len(content) / 200, 10) * 0.1
    return score


async def retrieve(query: str, top_k: int = 5, min_score: float = 0.3) -> Dict[str, Any]:
    """检索知识库：默认 knowledge.json + 用户发布数据集。"""
    default_docs = _load_knowledge()
    user_docs = await _load_user_published_knowledge()

    # 用户知识排前面（权重略高）
    all_docs = user_docs + default_docs

    scored = [(d, keyword_score(query, d)) for d in all_docs]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [d for d, s in scored if s > min_score][:top_k]

    sources = [
        {
            "id": d.get("id"),
            "topic": d.get("topic"),
            "score": round(keyword_score(query, d), 2),
            "content": d.get("content", "")[:200],
            "source": d.get("source", "default"),
        }
        for d in top
    ]
    context = "\n\n".join(f"[{i+1}] {d['topic']}：{d['content']}" for i, d in enumerate(top)) if top else ""
    return {
        "sources": sources,
        "context": context,
        "user_knowledge_count": len(user_docs),
        "default_knowledge_count": len(default_docs),
    }
