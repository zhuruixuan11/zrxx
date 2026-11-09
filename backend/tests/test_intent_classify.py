"""意图分类：8 类意图各一条 + 澄清分支（低置信度）。

全部走关键词兜底路径（Mock LLM），不发起任何网络/模型调用。
"""
import asyncio

import pytest

from app.agent import nodes
from app.agent.router import _keyword_classify, pick_clarification_question
from app.agent.state import INTENT_LIST, IntentType


class _MockLLM:
    """只实现意图识别用到的 is_mock()，让 classify_intent_llm 直接走关键词。"""

    def is_mock(self) -> bool:
        return True


@pytest.mark.parametrize("message,intent,min_conf", [
    # screener：此前零覆盖
    ("帮我筛选出金叉的股票", IntentType.SCREENER, 0.85),
    ("找几只超卖的股票", IntentType.SCREENER, 0.85),
    ("我的持仓现在赚多少", IntentType.PORTFOLIO_QUERY, 0.85),
    ("贵州茅台今天行情怎么样", IntentType.MARKET_QUERY, 0.85),
    # investment_analysis：此前零覆盖
    ("帮我分析一下 600519 值不值得买", IntentType.INVESTMENT_ANALYSIS, 0.90),
    ("降息对市场环境有什么影响", IntentType.INVESTMENT_ANALYSIS, 0.85),
    ("什么是市盈率", IntentType.KNOWLEDGE_QUERY, 0.85),
    ("这个组合的回撤风险大吗", IntentType.RISK_ASSESSMENT, 0.85),
    ("600519 跌到 1500 提醒我", IntentType.ALERT_CONFIG, 0.85),
    ("你好", IntentType.CHITCHAT, 0.85),
])
def test_each_intent_is_recognised(message, intent, min_conf):
    got, conf = _keyword_classify(message)
    assert got == intent
    assert conf >= min_conf


def test_all_eight_intents_are_covered():
    covered = {
        IntentType.SCREENER, IntentType.PORTFOLIO_QUERY, IntentType.MARKET_QUERY,
        IntentType.INVESTMENT_ANALYSIS, IntentType.KNOWLEDGE_QUERY,
        IntentType.RISK_ASSESSMENT, IntentType.ALERT_CONFIG, IntentType.CHITCHAT,
    }
    assert covered == set(INTENT_LIST)


def test_vague_message_has_confidence_below_clarification_threshold():
    intent, conf = _keyword_classify("随便说点什么")
    assert intent == IntentType.CHITCHAT
    assert conf == 0.6
    assert conf < 0.8  # node_classify 的澄清阈值


def test_node_classify_asks_for_clarification_on_low_confidence(monkeypatch):
    monkeypatch.setattr(nodes, "get_llm", lambda: _MockLLM())
    state = asyncio.run(nodes.node_classify({"message": "随便说点什么"}))
    assert state["intent_confidence"] == 0.6
    assert state["needs_clarification"] is True
    assert "再说清楚" in state["clarification_question"]
    # 澄清标志优先于意图，路由必须进 clarification 节点而不是 chitchat
    assert nodes.route_by_intent(state) == "clarification"


def test_node_classify_routes_directly_on_clear_intent(monkeypatch):
    monkeypatch.setattr(nodes, "get_llm", lambda: _MockLLM())
    state = asyncio.run(nodes.node_classify({"message": "帮我筛选出金叉的股票"}))
    assert state["intent"] == IntentType.SCREENER
    assert state["needs_clarification"] is False
    assert nodes.route_by_intent(state) == "screener"


def test_clarification_question_names_both_candidate_intents():
    q = pick_clarification_question(IntentType.SCREENER, IntentType.PORTFOLIO_QUERY)
    assert "全市场选股" in q
    assert "投资组合" in q
