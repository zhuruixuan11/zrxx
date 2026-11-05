"""财经新闻情感分析（规则引擎版）。

用中文财经词表给标题/摘要打分，而不是逐条调用 LLM：
  - 资讯中心 60 秒刷新一次、单次几十条，逐条调 LLM 既慢又贵；
  - 中文财经标题表达高度模板化（涨停/减持/立案调查…），词表命中率很高；
  - 规则版完全确定、可解释（能告诉用户命中了哪些词），零额外成本。

打分口径：每个命中词按权重累加（强词如「涨停」权重 2，弱词如「增长」权重 1），
利好为正、利空为负；总分 ≥ 2 判利好，≤ -2 判利空，其余中性。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

# 利好词（词 → 权重；强信号给 2）
BULLISH_WORDS: Dict[str, int] = {
    "涨停": 2, "大涨": 2, "涨超": 1, "飙升": 2, "创新高": 2, "新高": 1,
    "业绩预增": 2, "净利增长": 2, "净利润增长": 2, "扭亏为盈": 2, "扭亏": 2,
    "超预期": 2, "超市场预期": 2, "好于预期": 2, "好于市场预期": 2, "同比增长": 1, "增长": 1, "盈利": 1, "营收增长": 1,
    "中标": 2, "签约": 1, "订单大增": 2, "大单": 2, "订单": 1, "扩产": 1,
    "涨价": 2, "提价": 1, "政策扶持": 2, "补贴": 1, "减税": 2, "降准": 2, "降息": 2,
    "获批": 1, "批准": 1, "增持": 2, "回购": 2, "分红": 1, "派息": 1, "派现": 1,
    "摘帽": 2, "撤销风险警示": 2, "撤销ST": 2,
    "上调目标价": 2, "目标价上调": 2, "上调评级": 2,
    "战略合作": 1, "并购": 1, "重组": 1, "国产替代": 1, "利好": 2,
    "突破": 1, "放量": 1, "上调": 1, "复苏": 1, "回暖": 1, "加码": 1,
}

# 利空词
BEARISH_WORDS: Dict[str, int] = {
    "跌停": 2, "大跌": 2, "暴跌": 2, "跌超": 1, "重挫": 2, "创新低": 2, "新低": 1,
    "业绩预减": 2, "净利下滑": 2, "净利润下滑": 2, "亏损": 2, "同比下降": 1,
    "下滑": 1, "下降": 1, "不及预期": 2, "低于预期": 2, "放缓": 1, "承压": 1,
    "减持": 2, "拟减持": 2, "违规减持": 2, "解禁": 1, "质押": 1,
    "处罚": 2, "罚款": 2, "立案": 2, "立案调查": 2, "问询": 1, "警示": 1,
    "退市": 2, "ST": 2, "风险警示": 2, "下调评级": 2, "下调": 1,
    "违约": 2, "暴雷": 2, "逾期": 1, "诉讼": 1, "停产": 2, "召回": 1,
    "终止": 1, "失败": 1, "利空": 2, "裁员": 1, "破产": 2, "停牌": 1,
}

VALID_SENTIMENTS = frozenset({"bullish", "bearish", "neutral"})

LABEL_CN = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}

# 否定前缀：紧挨在利空词前时，该利空不成立（如「不减持」「未违规」）
_NEG_PREFIXES = ("不", "未", "无", "非")

# 语义反转的固定说法：先归一化成确定极性，再打分
_REWRITES = {
    "撤销其他风险警示": "摘帽",
    "撤销风险警示": "摘帽",
    "解除风险警示": "摘帽",
    "取消风险警示": "摘帽",
}

# 「上调/下调 … 目标价/评级」中间常夹公司名，用正则兜底
_PATTERNS = [
    (re.compile(r"(上调|提高|调高|看多).{0,20}(目标价|评级)"), 2, "bullish", "上调目标价"),
    (re.compile(r"(下调|降低|调低|看空).{0,20}(目标价|评级)"), 2, "bearish", "下调目标价"),
    (re.compile(r"(目标价|评级).{0,20}(上调|提高|调高)"), 2, "bullish", "目标价上调"),
    (re.compile(r"(目标价|评级).{0,20}(下调|降低|调低)"), 2, "bearish", "目标价下调"),
]


def _preprocess(text: str) -> str:
    """处理否定与「反转」表述，避免把利好读成利空。

    例：「撤销其他风险警示」= 摘帽（利好），但原词表会命中「风险警示」。
    """
    for src, dst in _REWRITES.items():
        text = text.replace(src, dst)
    for prefix in _NEG_PREFIXES:
        for word in BEARISH_WORDS:
            # 词本身就带否定（如「不及预期」）时不做剥离
            if word.startswith(_NEG_PREFIXES):
                continue
            text = text.replace(f"{prefix}{word}", "")
    return text


def _hits(text: str, words: Dict[str, int]) -> tuple[int, List[str]]:
    """命中统计；长词优先，避免「立案调查」与「立案」重复计分。"""
    matched = [w for w in words if w in text]
    kept: List[str] = []
    for w in sorted(matched, key=len, reverse=True):
        if any(w in k for k in kept):
            continue
        kept.append(w)
    score = sum(words[w] for w in kept)
    return score, kept


def analyze(title: str, summary: str = "") -> Dict[str, Any]:
    """单条新闻打分：返回 {sentiment, score, keywords}。"""
    text = _preprocess(f"{title or ''} {summary or ''}")
    bull_score, bull_hits = _hits(text, BULLISH_WORDS)
    bear_score, bear_hits = _hits(text, BEARISH_WORDS)

    # 正则兜底：命中「上调目标价」时，去掉单词「上调」避免重复计分
    fired: set[str] = set()
    for regex, weight, polarity, keyword in _PATTERNS:
        if polarity in fired or not regex.search(text):
            continue
        fired.add(polarity)
        if polarity == "bullish":
            bull_score += weight
            bull_hits.append(keyword)
            for w in ("上调", "提高"):
                if w in bull_hits:
                    bull_hits.remove(w)
                    bull_score -= BULLISH_WORDS.get(w, 0)
        else:
            bear_score += weight
            bear_hits.append(keyword)
            for w in ("下调", "降低"):
                if w in bear_hits:
                    bear_hits.remove(w)
                    bear_score -= BEARISH_WORDS.get(w, 0)

    raw = bull_score - bear_score

    if raw >= 2:
        label = "bullish"
    elif raw <= -2:
        label = "bearish"
    else:
        label = "neutral"

    keywords = bull_hits if raw > 0 else bear_hits if raw < 0 else bull_hits + bear_hits
    return {
        "sentiment": label,
        "score": max(-1.0, min(1.0, raw / 4.0)),
        "keywords": keywords[:5],
    }


def label_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给每条新闻加上 sentiment / sentiment_score / sentiment_keywords 字段。"""
    out: List[Dict[str, Any]] = []
    for it in items:
        data = analyze(it.get("title", ""), it.get("summary", ""))
        out.append({**it, **{
            "sentiment": data["sentiment"],
            "sentiment_score": round(data["score"], 2),
            "sentiment_keywords": data["keywords"],
        }})
    return out


def aggregate(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总一批已标注新闻的情绪分布与整体倾向。"""
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    scores: List[float] = []
    for it in items:
        label = it.get("sentiment")
        if label not in VALID_SENTIMENTS:
            continue
        counts[label] += 1
        scores.append(float(it.get("sentiment_score") or 0))

    total = len(scores)
    if not total:
        return {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0,
                "score": 0.0, "sentiment": "neutral", "label": LABEL_CN["neutral"]}

    avg = sum(scores) / total
    if avg >= 0.15:
        label = "bullish"
    elif avg <= -0.15:
        label = "bearish"
    else:
        label = "neutral"

    return {
        **counts,
        "total": total,
        "score": round(avg, 2),
        "sentiment": label,
        "label": LABEL_CN[label],
    }
