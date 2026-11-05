"""意图识别关键词兜底单测（不依赖 LLM）。"""
from app.agent.router import _keyword_classify
from app.agent.state import IntentType


def test_portfolio_keyword_wins_over_generic_question_word():
    intent, conf = _keyword_classify("我的持仓怎么样")
    assert intent == IntentType.PORTFOLIO_QUERY
    assert conf >= 0.85


def test_alert_keyword():
    intent, _ = _keyword_classify("600519 跌到 1500 提醒我")
    assert intent == IntentType.ALERT_CONFIG


def test_market_keyword():
    intent, _ = _keyword_classify("茅台今天行情如何")
    assert intent == IntentType.MARKET_QUERY


def test_unknown_text_falls_back_to_chitchat_with_low_confidence():
    intent, conf = _keyword_classify("随便说点什么")
    assert intent == IntentType.CHITCHAT
    assert conf == 0.6
