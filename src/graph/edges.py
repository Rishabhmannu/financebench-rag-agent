"""Conditional edge routing functions for the RAG agent graph."""

from src.config.settings import settings
from src.models.state import RAGState


def route_after_guardrails(state: RAGState) -> str:
    """Route based on guardrail status."""
    status = state.get("guardrail_status", "clean")
    if status == "clean":
        return "clean"
    return "blocked"


def route_after_router(state: RAGState) -> str:
    """Route based on query intent classification."""
    intent = state.get("query_intent", "retrieval")
    if intent == "retrieval":
        return "retrieval"
    elif intent == "out_of_scope":
        return "out_of_scope"
    return "clarification"


def route_after_grading(state: RAGState) -> str:
    """Route based on grading results: sufficient chunks, retry, or no info."""
    relevant_chunks = state.get("relevant_chunks", [])
    retry_count = state.get("retrieval_retry_count", 0)

    if len(relevant_chunks) >= settings.GRADING_MIN_RELEVANT_CHUNKS:
        return "sufficient"
    elif retry_count < settings.MAX_RETRIEVAL_RETRIES:
        return "retry"
    return "no_info"


def route_after_retrieval_evaluator(state: RAGState) -> str:
    """Route after optional selective retrieval evaluation."""
    decision = state.get("retrieval_evaluator_decision", "accept")
    if decision == "retry":
        retry_count = state.get("retrieval_retry_count", 0)
        if retry_count < settings.MAX_RETRIEVAL_RETRIES:
            return "retry"
    return "accept"


def route_after_hallucination(state: RAGState) -> str:
    """Route based on hallucination check results."""
    status = state.get("hallucination_status", "grounded")
    score = state.get("hallucination_score", 1.0)
    retry_count = state.get("generation_retry_count", 0)

    if status == "grounded" and score >= settings.HALLUCINATION_THRESHOLD:
        return "grounded"
    elif retry_count < settings.MAX_GENERATION_RETRIES:
        return "retry"
    # Exhausted retries — pass through with disclaimer
    return "disclaimer"


def route_after_hitl(state: RAGState) -> str:
    """Route based on HITL decision."""
    requires_approval = state.get("requires_human_approval", False)
    decision = state.get("human_decision")

    if not requires_approval:
        return "no_approval_needed"
    elif decision == "approved":
        return "approved"
    elif decision == "rejected":
        return "rejected"
    return "no_approval_needed"
