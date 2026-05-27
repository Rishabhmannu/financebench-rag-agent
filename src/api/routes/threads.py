"""Thread (conversation) endpoints for the Sprint 9 frontend sidebar.

The sidebar lists prior conversations and lets users resume them. Three
endpoints back that:

  GET    /threads                  — list current user's threads
  GET    /threads/{thread_id}      — load messages + interrupt state
  DELETE /threads/{thread_id}      — delete a conversation

Ownership is enforced by reading the ``user_id`` we wrote into the
LangGraph metadata at chat-route time. Cross-user access returns 403,
not 404 — we want to be honest that the thread exists but isn't yours.

LangGraph's AsyncPostgresSaver doesn't expose a public "list by metadata"
API, so thread enumeration drops to raw SQL via ``thread_service``.
Per-thread *contents* go through the public ``aget_state`` API so we
don't reimplement the checkpoint deserializer.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from src.api.dependencies import get_current_user
from src.models.auth import User
from src.services.thread_service import (
    delete_thread,
    get_thread_owner,
    list_all_threads_paged,
    list_threads_for_user,
    ts_from_checkpoint_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/threads", tags=["threads"])


def _pool_or_503(http_request: Request):
    pool = getattr(http_request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Checkpoint store not initialized — HITL/threads disabled",
        )
    return pool


def _make_title(state_values: dict[str, Any] | None) -> str:
    """Pick a sidebar title for a thread.

    Preference order:
      1. The graph state's `original_query` (set by the chat route)
      2. The graph state's `sanitized_query` (after guardrails)
      3. Empty string fallback
    Truncated to 80 chars so the sidebar stays compact.
    """
    if not state_values:
        return ""
    q = state_values.get("original_query") or state_values.get("sanitized_query") or ""
    q = q.strip().replace("\n", " ")
    return q[:80] + ("…" if len(q) > 80 else "")


def _is_interrupted(graph_state) -> tuple[bool, dict | None]:
    """Detect a pending HITL interrupt on the latest checkpoint.

    Mirrors the inspection in `src/api/routes/chat.py` — when an interrupt
    fires, `graph_state.tasks` carries the pending interrupt payload.
    """
    interrupted = False
    payload: dict | None = None
    for task in (graph_state.tasks or []):
        if getattr(task, "interrupts", None):
            interrupt_value = task.interrupts[0].value
            interrupted = True
            payload = interrupt_value if isinstance(interrupt_value, dict) else {"value": str(interrupt_value)}
            break
    return interrupted, payload


def _messages_from_state(state_values: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Reconstruct a {role, content} message list from the graph state.

    Our RAGState (see `src/models/state.py`) keeps the user message in
    `original_query` and the final answer in `final_response`. Anything
    in between (intermediate node outputs) isn't user-visible.
    """
    if not state_values:
        return []
    msgs: list[dict[str, Any]] = []
    user_q = state_values.get("original_query")
    if user_q:
        msgs.append({"role": "user", "content": user_q})
    answer = state_values.get("final_response")
    if answer:
        meta = state_values.get("response_metadata") or {}
        msgs.append({
            "role": "assistant",
            "content": answer,
            "sources": meta.get("sources", []),
            "confidence": meta.get("confidence"),
        })
    return msgs


@router.get("")
async def list_threads(
    http_request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    all_users: bool = Query(
        False,
        alias="all",
        description="Admin only: enumerate every user's threads (default false). Ignored for non-admin roles.",
    ),
    user: User = Depends(get_current_user),
):
    """List conversation threads, newest first.

    Default: caller's own threads. Admin + `?all=true`: every user's threads
    (Bug A fix). Each row carries owner identity (user_id, name, role, dept),
    decoded created_at / last_activity_at timestamps, checkpoint_count, plus
    the title + is_interrupted flag derived from a per-row aget_state call.
    """
    pool = _pool_or_503(http_request)

    # Bug A (audit): admin gets the cross-user listing when ?all=true.
    # Non-admin role silently falls back to own-user listing — we don't 403
    # to avoid breaking older CLIs that may set ?all=true unconditionally.
    if all_users and user.role == "admin":
        rows, total = await list_all_threads_paged(pool, limit=limit, offset=offset)
        scope = "all"
    else:
        rows, total = await list_threads_for_user(pool, user.user_id, limit=limit, offset=offset)
        scope = "self"

    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        from src.api.routes.chat import _get_graph
        graph = _get_graph(checkpointer=http_request.app.state.checkpointer)

    threads = []
    for r in rows:
        cfg = {"configurable": {"thread_id": r["thread_id"]}}
        title = ""
        interrupted = False
        try:
            gs = await graph.aget_state(cfg)
            title = _make_title(gs.values if gs else None)
            interrupted, _ = _is_interrupted(gs)
        except Exception as e:
            logger.warning("aget_state failed for thread %s: %s", r["thread_id"], e)
        threads.append({
            "thread_id": r["thread_id"],
            "title": title,
            "checkpoint_count": r["checkpoint_count"],
            "is_interrupted": interrupted,
            # Track 2 enrichment: owner identity + decoded timestamps. The
            # CLI uses these to render columns like "Owner | Role | Last
            # activity" so admin can scan whose request is whose at a glance.
            "owner": {
                "user_id": r["user_id"],
                "name": r["name"],
                "role": r["role"],
                "department": r["department"],
            },
            "created_at": r["created_at"],
            "last_activity_at": r["last_activity_at"],
        })

    return {
        "threads": threads,
        "total": total,
        "limit": limit,
        "offset": offset,
        "scope": scope,
        "viewer_role": user.role,
    }


@router.get("/{thread_id}")
async def get_thread(
    thread_id: str,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Load the messages + interrupt state + HITL audit for a single thread.

    Ownership: the thread's metadata.user_id must match the caller. Admin
    bypass per docs/cli.md spec (Bug A fix — get_thread had no bypass while
    delete_thread did, an inconsistency we're now closing).
    """
    pool = _pool_or_503(http_request)

    # Bug A (audit) + Track 2: pull owner identity (4-tuple) instead of just
    # user_id so the response can carry the owner block. Same call cost.
    from src.services.thread_service import get_thread_owner_role
    owner_user_id, owner_role, owner_name, owner_dept = await get_thread_owner_role(pool, thread_id)
    if owner_user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    if owner_user_id != user.user_id and user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Thread belongs to a different user")

    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        from src.api.routes.chat import _get_graph
        graph = _get_graph(checkpointer=http_request.app.state.checkpointer)

    cfg = {"configurable": {"thread_id": thread_id}}
    gs = await graph.aget_state(cfg)
    owner_block = {
        "user_id": owner_user_id,
        "name": owner_name,
        "role": owner_role,
        "department": owner_dept,
    }
    if gs is None or not gs.values:
        return {
            "thread_id": thread_id,
            "messages": [],
            "is_interrupted": False,
            "interrupt_payload": None,
            "owner": owner_block,
            "turn_count": 0,
            "audit": None,
            "last_activity_at": None,
        }

    interrupted, payload = _is_interrupted(gs)
    values = gs.values or {}

    # Turn count = number of HumanMessage entries in the state. Cheap (already
    # have the deserialized state from aget_state above).
    try:
        from langchain_core.messages import HumanMessage
        turn_count = sum(1 for m in (values.get("messages") or []) if isinstance(m, HumanMessage))
    except Exception:
        turn_count = 0

    # HITL audit block (Track 2): present iff this thread went through hitl_gate
    # and had a decision recorded. All None if the thread never triggered HITL.
    audit = None
    if values.get("hitl_submitted_at") or values.get("human_decision_at"):
        audit = {
            "hitl_submitted_at": values.get("hitl_submitted_at"),
            "decided_at": values.get("human_decision_at"),
            "decided_by": values.get("human_decision_by"),
            "decided_by_role": values.get("human_decision_by_role"),
            "decision": values.get("human_decision"),
            "reason": values.get("human_decision_reason"),
        }

    # Last activity = decoded ts of the most-recent checkpoint id. Cheaper
    # than another SQL roundtrip — gs.config carries the checkpoint_id used.
    last_activity_at = ts_from_checkpoint_id(gs.config.get("configurable", {}).get("checkpoint_id"))

    return {
        "thread_id": thread_id,
        "messages": _messages_from_state(gs.values),
        "is_interrupted": interrupted,
        "interrupt_payload": payload,
        "owner": owner_block,
        "turn_count": turn_count,
        "audit": audit,
        "last_activity_at": last_activity_at,
    }


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread_endpoint(
    thread_id: str,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Delete a conversation. Ownership-gated; admin can delete any."""
    pool = _pool_or_503(http_request)
    owner = await get_thread_owner(pool, thread_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    if owner != user.user_id and user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Thread belongs to a different user")

    n = await delete_thread(pool, thread_id)
    logger.info("Deleted thread %s (%d checkpoint rows) for user %s", thread_id, n, user.user_id)
    return None
