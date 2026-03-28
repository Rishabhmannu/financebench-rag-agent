"""Terminal nodes for non-retrieval paths (blocked, out-of-scope, no-info, clarification)."""

from src.config.prompts import BLOCKED_RESPONSE, CLARIFICATION_RESPONSE, NO_INFO_RESPONSE, OUT_OF_SCOPE_RESPONSE
from src.models.state import RAGState


def blocked_response_node(state: RAGState) -> dict:
    return {"final_response": BLOCKED_RESPONSE, "response_metadata": {"reason": state.get("guardrail_status", "blocked")}}


def out_of_scope_node(state: RAGState) -> dict:
    return {"final_response": OUT_OF_SCOPE_RESPONSE, "response_metadata": {"reason": "out_of_scope"}}


def clarification_node(state: RAGState) -> dict:
    return {"final_response": CLARIFICATION_RESPONSE, "response_metadata": {"reason": "clarification"}}


def no_info_node(state: RAGState) -> dict:
    return {"final_response": NO_INFO_RESPONSE, "response_metadata": {"reason": "no_relevant_info"}}
