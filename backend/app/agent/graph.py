"""LangGraph Agent 图构建。

意图路由（classify_intent → conditional_edges）：
    ├─ 低置信度 → clarification → END
    ├─ portfolio_query → portfolio → END
    ├─ market_query    → market    → END
    ├─ investment_analysis → analysis → END  (内部 fan-out 并行：组合 + 行情 + RAG + 画像)
    ├─ knowledge_query → knowledge → END
    ├─ risk_assessment → risk → END
    ├─ alert_config    → alert → END
    └─ chitchat        → chitchat → END

设计要点：
- 意图模糊时走澄清节点反问用户，不盲目路由
- 投资分析节点内部并行 fan-out，缩短首字延迟
- 每个节点返回 dict（不可变风格），避免污染 LangGraph 全局 state
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from ..config import get_settings
from .memory import format_for_prompt, get_recent_history
from .nodes import (
    node_alert,
    node_analysis,
    node_chitchat,
    node_classify,
    node_clarification,
    node_knowledge,
    node_market,
    node_portfolio,
    node_risk,
    route_by_intent,
)
from .state import AgentState


# ===== LangGraph 1.x 版本：编译后的 graph 是 Runnable =====
def build_graph():
    graph = StateGraph(AgentState)

    # 入口：注入历史上下文 + 意图识别
    graph.add_node("classify", node_classify)
    graph.add_node("clarification", node_clarification)
    graph.add_node("portfolio", node_portfolio)
    graph.add_node("market", node_market)
    graph.add_node("analysis", node_analysis)
    graph.add_node("knowledge", node_knowledge)
    graph.add_node("chitchat", node_chitchat)
    graph.add_node("risk", node_risk)
    graph.add_node("alert", node_alert)

    graph.set_entry_point("classify")
    graph.add_conditional_edges("classify", route_by_intent)

    # 所有叶子节点 → END
    for n in ("clarification", "portfolio", "market", "analysis",
              "knowledge", "chitchat", "risk", "alert"):
        graph.add_edge(n, END)

    return graph.compile()


_agent = None


def get_agent():
    global _agent
    if _agent is None:
        _agent = build_graph()
    return _agent


async def run_agent(user_id: str, message: str, session=None, **extra) -> dict:
    """调用 Agent 图运行一次。

    Args:
        user_id: 用户 ID
        message: 用户输入文本
        session: SQLAlchemy async session（用于查历史；None 时跳过历史注入）
    Returns:
        dict 包含 response / response_type / intent / ...
    """
    from loguru import logger
    from ..config import get_settings

    settings = get_settings()

    # 1) 注入历史上下文
    history_ctx = ""
    if session is not None:
        try:
            history = await get_recent_history(session, user_id, limit=6)
            history_ctx = format_for_prompt(history)
        except Exception as e:
            logger.warning("历史注入失败：%s", e)

    initial = AgentState(
        user_id=user_id,
        message=message,
        intent=None,
        intent_confidence=0.0,
        history_context=history_ctx,
        needs_clarification=False,
        clarification_question="",
        portfolio_data=None,
        market_data=None,
        rag_sources=[],
        rag_context="",
        response="",
        response_type="text",
        error=None,
        disclaimer=settings.AGENT_DISCLAIMER,
    )

    result = await get_agent().ainvoke(initial)
    return result
