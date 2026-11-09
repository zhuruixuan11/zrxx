"""Agent 图结构快照：节点、边、意图→节点映射。改动编排而没同步更新这里，测试会挂。"""
import pytest

from app.agent.graph import build_graph
from app.agent.nodes import route_by_intent

# 10 个业务节点（不含 LangGraph 内置的 __start__ / __end__）
EXPECTED_NODES = {
    "classify", "clarification", "portfolio", "market", "analysis",
    "knowledge", "chitchat", "risk", "alert", "screener",
}

# 所有叶子节点直连 END
LEAF_NODES = {
    "clarification", "portfolio", "market", "analysis",
    "knowledge", "chitchat", "risk", "alert", "screener",
}

# 8 类意图 → 目标节点
INTENT_TO_NODE = {
    "portfolio_query": "portfolio",
    "market_query": "market",
    "investment_analysis": "analysis",
    "knowledge_query": "knowledge",
    "chitchat": "chitchat",
    "risk_assessment": "risk",
    "alert_config": "alert",
    "screener": "screener",
}


def test_graph_nodes_and_edges_snapshot():
    compiled = build_graph()
    builder = compiled.builder

    assert set(builder.nodes) == EXPECTED_NODES
    assert ("__start__", "classify") in builder.edges
    assert {(leaf, "__end__") for leaf in LEAF_NODES} <= set(builder.edges)
    # 只有 classify 是条件边，且分支函数名固定
    assert set(builder.branches) == {"classify"}
    assert set(builder.branches["classify"]) == {"route_by_intent"}


def test_entry_point_is_classify():
    edges = set(build_graph().builder.edges)
    starts = {src for src, _ in edges if src == "__start__"}
    assert starts == {"__start__"}
    assert ("__start__", "classify") in edges


@pytest.mark.parametrize("intent,node", sorted(INTENT_TO_NODE.items()))
def test_route_by_intent_snapshot(intent, node):
    assert route_by_intent({"intent": intent}) == node
    # 映射目标必须是图里真实存在的节点，防止改名字后路由指向空节点
    assert node in EXPECTED_NODES


def test_all_declared_intents_are_routable():
    from app.agent.state import INTENT_LIST

    assert set(INTENT_LIST) == set(INTENT_TO_NODE)


def test_clarification_flag_wins_over_intent():
    assert route_by_intent({"needs_clarification": True, "intent": "portfolio_query"}) == "clarification"


def test_unknown_intent_falls_back_to_chitchat():
    assert route_by_intent({"intent": "no_such_intent"}) == "chitchat"
