"""意图识别：LLM 主路径 + 关键词 fallback + 置信度判断。

置信度 < 0.8 → 走澄清节点反问用户（解决 PDF 里意图模糊时盲目路由的问题）。
"""
from __future__ import annotations

import re
from typing import Tuple

from loguru import logger

from .state import IntentType, INTENT_LIST


# ========== 关键词规则（轻量兜底，Mock/LLM 失败时用）========
# 顺序 = 优先级（越具体/匹配越精准的意图越靠前）
_KEYWORD_RULES: list[tuple[str, list[str]]] = [
    # 按信号选股（最具体 → 放最前）
    (IntentType.SCREENER,         ["选出", "筛选", "找几只", "找些", "选股", "扫描", "全市场", "金叉股票", "金叉的", "超卖股票", "超卖的", "放量的", "锤子线", "突破股票", "选出几只", "帮我筛"]),
    (IntentType.PORTFOLIO_QUERY,  ["投资组合", "我的组合", "组合", "持仓", "总资产", "我的资产", "盈亏", "收益", "赚多少", "亏多少", "portfolio", "holding"]),
    (IntentType.MARKET_QUERY,     ["行情", "股价", "涨跌", "走势", "K线", "最新价", "现在多少", "收盘价", "开盘价", "market", "quote"]),
    # 市场环境 / 宏观与资金面：这类问题不需要股票代码，走分析流程注入宏观数据
    (IntentType.INVESTMENT_ANALYSIS, ["宏观", "GDP", "CPI", "M2", "SHIBOR", "通胀", "货币政策", "降准", "降息", "加息", "流动性", "资金面", "龙虎榜", "主力资金", "大盘", "市场环境", "市场怎么", "大盘怎么"]),
    (IntentType.KNOWLEDGE_QUERY,  ["什么是", "什么叫", "怎么选", "知识", "策略", "术语", "入门", "解释", "是什么意思"]),
    (IntentType.ALERT_CONFIG,     ["提醒", "预警", "止盈", "止损", "监控", "设一个", "设置", "alert", "notify"]),
    (IntentType.RISK_ASSESSMENT,  ["风险", "回撤", "波动", "警示", "安全", "risk", "volatil"]),
    (IntentType.INVESTMENT_ANALYSIS, ["帮我看看", "值不值得", "要不要买", "值得买", "帮我分析", "分析一下", "评估一下", "好吗", "怎么样", "可以买", "建议"]),
    (IntentType.CHITCHAT,         ["你好", "hi", "hello", "在吗", "谢谢", "再见"]),
]


def _keyword_classify(text: str) -> Tuple[str, float]:
    """纯关键词匹配：收集所有命中的意图，取匹配词数最多的。

    注意：关键词匹配的置信度都设为 0.85 以上——因为能命中关键词本身说明意图明确，
    不需要走澄清分支（澄清是给 LLM 低置信度用的）。
    """
    t = text.lower()
    candidates: list[tuple[int, int, str]] = []
    for i, (intent, keywords) in enumerate(_KEYWORD_RULES):
        matched = [kw for kw in keywords if kw.lower() in t]
        if matched:
            candidates.append((len(matched), -i, intent))
    if not candidates:
        return IntentType.CHITCHAT, 0.6
    candidates.sort(reverse=True)
    best_n, _, best_intent = candidates[0]
    # 关键词匹配：至少 0.85（明确），匹配到越多词越高
    conf = min(0.99, 0.85 + best_n * 0.03)
    return best_intent, conf


async def classify_intent_llm(message: str, llm_client) -> Tuple[str, float]:
    """LLM 意图识别。Mock 模式下直接走纯关键词（省一次 Mock LLM 调用，且更准）。

    优化：只要命中任意关键词（意图已足够明确）就直接返回，
    省掉一次 LLM 往返（约 1-2s）；完全没命中时才交给 LLM 判断。
    """
    kw_intent, kw_conf = _keyword_classify(message)
    if kw_conf >= 0.88:
        return kw_intent, kw_conf

    if llm_client.is_mock():
        return kw_intent, kw_conf
    prompt = f"""你是一个意图识别引擎。根据用户的输入，从以下意图中选一个最合适的，并给出置信度（0.0-1.0）。

可选意图：
- screener: 按策略信号/技术指标从全市场选股（如选出金叉的、筛超卖的、找放量股票）
- portfolio_query: 查询投资组合/持仓/资产/盈亏
- market_query: 查询股票/基金行情、股价、走势
- investment_analysis: 对某只股票/基金做投资分析建议
- knowledge_query: 询问理财知识、术语解释、入门问题
- risk_assessment: 询问风险、回撤、波动、安全边际
- alert_config: 设置提醒/预警/止盈止损
- chitchat: 闲聊/打招呼/其他

严格按以下 JSON 格式返回，不要输出其他内容：
{{"intent": "xxx", "confidence": 0.xx}}

用户输入：{message}"""

    try:
        reply = await llm_client.achat([{"role": "user", "content": prompt}])
        # 从回复里提取 JSON
        import json
        match = re.search(r"\{[^{}]+\}", reply)
        if not match:
            return _keyword_classify(message)
        data = json.loads(match.group())
        intent = data.get("intent", IntentType.CHITCHAT)
        if intent not in INTENT_LIST:
            intent = IntentType.CHITCHAT
        conf = float(data.get("confidence", 0.5))
        return intent, min(max(conf, 0.1), 1.0)
    except Exception as e:
        logger.warning("LLM 意图识别失败，回退关键词：%s", e)
        return _keyword_classify(message)


def pick_clarification_question(intent_a: str, intent_b: str) -> str:
    """两种可能意图时生成澄清问题。"""
    label = {
        IntentType.SCREENER: "按策略信号从全市场选股",
        IntentType.PORTFOLIO_QUERY: "查看投资组合/持仓",
        IntentType.MARKET_QUERY: "查询股票行情",
        IntentType.INVESTMENT_ANALYSIS: "获取投资分析建议",
        IntentType.KNOWLEDGE_QUERY: "询问理财知识",
        IntentType.RISK_ASSESSMENT: "评估风险",
        IntentType.ALERT_CONFIG: "设置价格预警",
        IntentType.CHITCHAT: "闲聊",
    }
    return f"您是想{label.get(intent_a, intent_a)}，还是{label.get(intent_b, intent_b)}？请帮我确认一下😊"
