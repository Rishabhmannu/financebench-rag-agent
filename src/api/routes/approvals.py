"""Approvals inbox — Phase 3.5 multi-party HITL.

GET /approvals — list pending HITL interrupts the caller is authorized to
                 approve (caller's role can_approve_for requester's role).
GET /approvals/{thread_id} — full review payload: query + draft answer +
                             requester + amount + threshold + created_at.

These endpoints are READ-ONLY. Decision actions still go through
POST /hitl/approve and POST /hitl/reject (now auth-gated on the same
can_approve hierarchy).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.api.dependencies import get_current_user
from src.config.rbac_config import can_approve
from src.models.auth import User
from src.services.thread_service import (
    count_hitl_decisions_on_thread,
    list_all_threads,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _pool_or_503(http_request: Request):
    pool = getattr(http_request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Checkpoint store not initialized — approvals disabled",
        )
    return pool


def _is_interrupted(graph_state) -> tuple[bool, dict | None]:
    for task in (graph_state.tasks or []):
        if getattr(task, "interrupts", None):
            v = task.interrupts[0].value
            return True, v if isinstance(v, dict) else {"value": str(v)}
    return False, None


@router.get("")
async def list_approvals(
    http_request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """List pending HITL interrupts the caller can approve."""
    pool = _pool_or_503(http_request)

    rows = await list_all_threads(pool)

    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        from src.api.routes.chat import _get_graph
        graph = _get_graph(checkpointer=http_request.app.state.checkpointer)

    pending: list[dict[str, Any]] = []
    for r in rows:
        thread_id = r["thread_id"]
        requester_user_id = r.get("user_id") or "?"
        requester_role = r.get("role") or "?"

        if not can_approve(user.role, requester_role):
            continue

        cfg = {"configurable": {"thread_id": thread_id}}
        try:
            gs = await graph.aget_state(cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("aget_state failed for thread %s: %s", thread_id, exc)
            continue
        interrupted, payload = _is_interrupted(gs)
        if not interrupted:
            continue

        query = ""
        sources_count = 0
        confidence = None
        if gs and gs.values:
            query = (gs.values.get("original_query") or gs.values.get("sanitized_query") or "").strip()
            meta = gs.values.get("response_metadata") or {}
            sources_count = len(meta.get("sources") or [])
            confidence = gs.values.get("hallucination_score")

        pending.append({
            "thread_id": thread_id,
            "requester_user_id": requester_user_id,
            "requester_name": r.get("name"),
            "requester_department": r.get("department"),
            "requester_role": requester_role,
            "query": query[:200],
            "reason": (payload or {}).get("reason"),
            "max_amount": (payload or {}).get("max_amount"),
            "threshold": (payload or {}).get("threshold"),
            "submitted_at": (payload or {}).get("submitted_at"),
            "sources_count": sources_count,
            "confidence": confidence,
        })

    return {"approvals": pending, "count": len(pending)}


@router.get("/{thread_id}")
async def show_approval(
    thread_id: str,
    http_request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Full review payload for a pending interrupt — including the draft answer
    that was suppressed from the requester's terminal."""
    pool = _pool_or_503(http_request)

    from src.services.thread_service import get_thread_owner_role
    owner, requester_role, requester_name, requester_dept = await get_thread_owner_role(pool, thread_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    if not can_approve(user.role, requester_role or ""):
        raise HTTPException(
            status_code=403,
            detail=f"Your role '{user.role}' cannot approve requests from '{requester_role}'.",
        )

    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        from src.api.routes.chat import _get_graph
        graph = _get_graph(checkpointer=http_request.app.state.checkpointer)

    cfg = {"configurable": {"thread_id": thread_id}}
    gs = await graph.aget_state(cfg)
    if gs is None:
        raise HTTPException(status_code=404, detail="Thread state unavailable")
    interrupted, payload = _is_interrupted(gs)
    if not interrupted:
        raise HTTPException(status_code=409, detail="Thread is not awaiting approval")

    state_values = gs.values or {}
    draft = state_values.get("generated_answer") or state_values.get("final_response") or ""
    query = state_values.get("original_query") or state_values.get("sanitized_query") or ""
    meta = state_values.get("response_metadata") or {}
    sources = meta.get("sources") or []
    source_files = sorted({s.get("file") for s in sources if isinstance(s, dict) and s.get("file")})

    # Track 2 enrichment — context fields the approver wants at decision time:
    #   submitted_at_age_seconds: pre-computed age so the CLI doesn't have to
    #     do timezone math. None if submitted_at is missing or malformed.
    #   prior_decisions_on_thread: catches re-submissions / bouncing patterns
    #     (e.g. requester re-asks a question that was already rejected).
    submitted_at_str = (payload or {}).get("submitted_at")
    submitted_at_age_s: float | None = None
    if submitted_at_str:
        try:
            from datetime import datetime, timezone
            submitted_dt = datetime.fromisoformat(submitted_at_str)
            if submitted_dt.tzinfo is None:
                submitted_dt = submitted_dt.replace(tzinfo=timezone.utc)
            submitted_at_age_s = (datetime.now(timezone.utc) - submitted_dt).total_seconds()
        except (ValueError, TypeError):
            pass
    prior_decisions = await count_hitl_decisions_on_thread(pool, thread_id)

    return {
        "thread_id": thread_id,
        "requester_user_id": owner,
        "requester_name": requester_name,
        "requester_department": requester_dept,
        "requester_role": requester_role,
        "query": query,
        "draft_answer": draft,
        "reason": (payload or {}).get("reason"),
        "max_amount": (payload or {}).get("max_amount"),
        "threshold": (payload or {}).get("threshold"),
        "submitted_at": submitted_at_str,
        "submitted_at_age_seconds": submitted_at_age_s,
        "prior_decisions_on_thread": prior_decisions,
        "sources_count": len(sources),
        "source_files": source_files,
        "confidence": state_values.get("hallucination_score"),
        "retrieval_fallback_used": bool(state_values.get("retrieval_fallback_used")),
    }
