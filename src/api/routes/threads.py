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
    list_threads_for_user,
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
    user: User = Depends(get_current_user),
):
    """List the current user's conversation threads, newest first.

    For each thread we cheaply read row stats from the checkpoints table
    (thread_id, counts, latest checkpoint_id as a recency proxy). Titles
    require deserializing the checkpoint blob, so they're populated by a
    second pass through LangGraph's ``aget_state`` only for the rows we
    actually return in this page.
    """
    pool = _pool_or_503(http_request)
    rows, total = await list_threads_for_user(pool, user.user_id, limit=limit, offset=offset)

    # Per-thread title + interrupt state require the deserialized state,
    # so we hit LangGraph's API once per row. Cheap at the typical page
    # size (≤50) and avoids reimplementing the deserializer.
    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        # Fallback: the chat route lazily builds & caches the graph; if
        # that hasn't happened yet we build it here so /threads still works
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
        })

    return {"threads": threads, "total": total, "limit": limit, "offset": offset}


@router.get("/{thread_id}")
async def get_thread(
    thread_id: str,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Load the messages + interrupt state for a single thread.

    Ownership: the thread's metadata.user_id must match the caller. 403
    on mismatch (intentionally not 404 — we don't leak existence to a
    different user any more than 403 already does).
    """
    pool = _pool_or_503(http_request)
    owner = await get_thread_owner(pool, thread_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    if owner != user.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Thread belongs to a different user")

    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        from src.api.routes.chat import _get_graph
        graph = _get_graph(checkpointer=http_request.app.state.checkpointer)

    cfg = {"configurable": {"thread_id": thread_id}}
    gs = await graph.aget_state(cfg)
    if gs is None or not gs.values:
        return {"thread_id": thread_id, "messages": [], "is_interrupted": False, "interrupt_payload": None}

    interrupted, payload = _is_interrupted(gs)
    return {
        "thread_id": thread_id,
        "messages": _messages_from_state(gs.values),
        "is_interrupted": interrupted,
        "interrupt_payload": payload,
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
