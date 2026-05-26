"""Terminal nodes for non-retrieval paths (blocked, out-of-scope, no-info, clarification)."""

import logging

from src.config.prompts import BLOCKED_RESPONSE, CLARIFICATION_RESPONSE, NO_INFO_RESPONSE, OUT_OF_SCOPE_RESPONSE
from src.config.rbac_config import get_permissions
from src.models.state import RAGState
from src.services.embeddings import embed_text
from src.services.vector_store import get_qdrant_client, hybrid_search

logger = logging.getLogger(__name__)


def blocked_response_node(state: RAGState) -> dict:
    return {"final_response": BLOCKED_RESPONSE, "response_metadata": {"reason": state.get("guardrail_status", "blocked")}}


def out_of_scope_node(state: RAGState) -> dict:
    return {"final_response": OUT_OF_SCOPE_RESPONSE, "response_metadata": {"reason": "out_of_scope"}}


def clarification_node(state: RAGState) -> dict:
    return {"final_response": CLARIFICATION_RESPONSE, "response_metadata": {"reason": "clarification"}}


def _rbac_blocked_doc_types(state: RAGState) -> set[str]:
    """Probe the corpus without RBAC filter for the user's query. Returns the
    set of doc_types that match the query but the user's role can't read.
    Empty set means RBAC was not the limiter (the data just isn't in the corpus).
    """
    query = state.get("retrieval_query") or state.get("sanitized_query", "")
    user_role = state.get("user_role", "")
    if not query or not user_role:
        return set()

    perms = get_permissions(user_role)
    allowed_doc_types = perms.get("allowed_doc_types", [])
    if "*" in allowed_doc_types:
        return set()

    try:
        client = get_qdrant_client()
        unfiltered = hybrid_search(
            client=client,
            query_text=query,
            query_dense_vector=embed_text(query),
            rbac_filter=None,
            top_k=5,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"RBAC probe failed ({type(exc).__name__}): {exc}")
        return set()

    blocked: set[str] = set()
    for chunk in unfiltered:
        meta = chunk.get("metadata", {}) or {}
        dt = meta.get("doc_type")
        if dt and dt not in allowed_doc_types:
            blocked.add(dt)
    return blocked


def no_info_node(state: RAGState) -> dict:
    """Empty-relevant-chunks terminal. When the empty-set was driven by RBAC
    filtering, return an informative refusal that names the doc types the user
    can't access. Otherwise the generic "couldn't find relevant info" message.
    """
    blocked = _rbac_blocked_doc_types(state)
    if blocked:
        user_role = state.get("user_role", "")
        perms = get_permissions(user_role)
        allowed = perms.get("allowed_doc_types", [])
        allowed_str = ", ".join(allowed) if "*" not in allowed else "all document types"
        blocked_str = ", ".join(sorted(blocked))
        msg = (
            f"**Access restricted.** This question matches documents of type **{blocked_str}**, "
            f"which your role (**{user_role}**) cannot read. "
            f"You currently have access to: **{allowed_str}**.\n\n"
            f"Switch to a role with access via `/role <name>`. "
            f"Try `finance` for invoices and expense policies, or `clevel` for confidential reports."
        )
        return {"final_response": msg, "response_metadata": {"reason": "rbac_restricted", "blocked_doc_types": sorted(blocked)}}

    return {"final_response": NO_INFO_RESPONSE, "response_metadata": {"reason": "no_relevant_info"}}
