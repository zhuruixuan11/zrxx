"""Agent 状态定义（LangGraph TypedDict）。"""
from typing import Any, Dict, List, Optional, TypedDict


class IntentType(str):
    # 所有意图（补齐 PDF 里的全部 7 种，不再有未路由的类型）
    PORTFOLIO_QUERY = "portfolio_query"
    MARKET_QUERY = "market_query"
    INVESTMENT_ANALYSIS = "investment_analysis"
    KNOWLEDGE_QUERY = "knowledge_query"
    CHITCHAT = "chitchat"
    RISK_ASSESSMENT = "risk_assessment"
    ALERT_CONFIG = "alert_config"
    SCREENER = "screener"  # 按信号选股（全市场扫描）


INTENT_LIST = [
    IntentType.PORTFOLIO_QUERY, IntentType.MARKET_QUERY,
    IntentType.INVESTMENT_ANALYSIS, IntentType.KNOWLEDGE_QUERY,
    IntentType.CHITCHAT, IntentType.RISK_ASSESSMENT, IntentType.ALERT_CONFIG,
    IntentType.SCREENER,
]


class AgentState(TypedDict, total=False):
    """LangGraph 全局状态。

    注意：运行时只会出现 total=False 字段，初始化不需要全部填充。
    """
    user_id: str
    message: str
    intent: Optional[str]
    intent_confidence: float
    # 澄清分支专用
    needs_clarification: bool
    clarification_question: str
    clarified_intent: Optional[str]  # 澄清后用户确认的意图
    # 并行节点产出
    portfolio_data: Optional[Dict[str, Any]]
    market_data: Optional[Dict[str, Any]]
    rag_sources: List[Dict[str, Any]]
    rag_context: str
    # 最终输出
    response: str
    response_type: str  # text / markdown
    error: Optional[str]
    # 对话历史注入（由 memory 模块预填充）
    history_context: str
