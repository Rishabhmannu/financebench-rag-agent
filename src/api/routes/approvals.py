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
from src.services.thread_service import list_all_threads

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
        if gs and gs.values:
            query = (gs.values.get("original_query") or gs.values.get("sanitized_query") or "").strip()

        pending.append({
            "thread_id": thread_id,
            "requester_user_id": requester_user_id,
            "requester_role": requester_role,
            "query": query[:200],
            "reason": (payload or {}).get("reason"),
            "max_amount": (payload or {}).get("max_amount"),
            "threshold": (payload or {}).get("threshold"),
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
    owner, requester_role = await get_thread_owner_role(pool, thread_id)
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

    return {
        "thread_id": thread_id,
        "requester_user_id": owner,
        "requester_role": requester_role,
        "query": query,
        "draft_answer": draft,
        "reason": (payload or {}).get("reason"),
        "max_amount": (payload or {}).get("max_amount"),
        "threshold": (payload or {}).get("threshold"),
    }
