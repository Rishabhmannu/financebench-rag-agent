"""Constructs and compiles the RAG agent StateGraph."""

from langgraph.graph import END, START, StateGraph

from src.graph.edges import (
    route_after_grading,
    route_after_guardrails,
    route_after_hallucination,
    route_after_hitl,
    route_after_router,
)
from src.graph.nodes.entity_extractor import entity_extractor_node
from src.graph.nodes.generator import generator_node
from src.graph.nodes.grader import grader_node
from src.graph.nodes.guardrails import guardrails_node
from src.graph.nodes.hallucination import hallucination_checker_node
from src.graph.nodes.hitl_gate import hitl_gate_node
from src.graph.nodes.query_rewriter import query_rewriter_node
from src.graph.nodes.rbac_gate import rbac_gate
from src.graph.nodes.reranker import reranker_node
from src.graph.nodes.response_formatter import response_formatter_node
from src.graph.nodes.retrieval import retrieval_node
from src.graph.nodes.router import router_node
from src.graph.nodes.terminal_nodes import blocked_response_node, clarification_node, no_info_node, out_of_scope_node
from src.models.state import RAGState


def build_graph(checkpointer=None) -> StateGraph:
    """Build the full RAG agent graph. Pass a checkpointer for HITL persistence."""

    graph = StateGraph(RAGState)

    # --- Add all nodes ---
    graph.add_node("rbac_gate", rbac_gate)
    graph.add_node("guardrails", guardrails_node)
    graph.add_node("blocked_response", blocked_response_node)
    graph.add_node("entity_extractor", entity_extractor_node)
    graph.add_node("router", router_node)
    graph.add_node("out_of_scope_response", out_of_scope_node)
    graph.add_node("clarification_response", clarification_node)
    graph.add_node("retrieval", retrieval_node)
    graph.add_node("reranker", reranker_node)
    graph.add_node("grader", grader_node)
    graph.add_node("query_rewriter", query_rewriter_node)
    graph.add_node("no_info_response", no_info_node)
    graph.add_node("generator", generator_node)
    graph.add_node("hallucination_checker", hallucination_checker_node)
    graph.add_node("hitl_gate", hitl_gate_node)
    graph.add_node("response_formatter", response_formatter_node)

    # --- Edges ---
    graph.add_edge(START, "rbac_gate")
    graph.add_edge("rbac_gate", "guardrails")

    graph.add_conditional_edges(
        "guardrails",
        route_after_guardrails,
        {"clean": "entity_extractor", "blocked": "blocked_response"},
    )
    graph.add_edge("entity_extractor", "router")
    graph.add_edge("blocked_response", END)

    graph.add_conditional_edges(
        "router",
        route_after_router,
        {"retrieval": "retrieval", "clarification": "clarification_response", "out_of_scope": "out_of_scope_response"},
    )
    graph.add_edge("out_of_scope_response", END)
    graph.add_edge("clarification_response", END)

    graph.add_edge("retrieval", "reranker")
    graph.add_edge("reranker", "grader")

    graph.add_conditional_edges(
        "grader",
        route_after_grading,
        {"sufficient": "generator", "retry": "query_rewriter", "no_info": "no_info_response"},
    )
    graph.add_edge("query_rewriter", "retrieval")
    graph.add_edge("no_info_response", END)

    graph.add_edge("generator", "hallucination_checker")

    graph.add_conditional_edges(
        "hallucination_checker",
        route_after_hallucination,
        {"grounded": "hitl_gate", "retry": "generator", "disclaimer": "hitl_gate"},
    )

    graph.add_conditional_edges(
        "hitl_gate",
        route_after_hitl,
        {"no_approval_needed": "response_formatter", "approved": "response_formatter", "rejected": "blocked_response"},
    )

    graph.add_edge("response_formatter", END)

    # --- Compile ---
    return graph.compile(checkpointer=checkpointer)
