import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph.types import Command
from pydantic import BaseModel

from src.api.dependencies import get_current_user
from src.config.rbac_config import can_approve
from src.graph.builder import build_graph
from src.models.auth import User
from src.services.thread_service import get_thread_owner_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hitl", tags=["human-in-the-loop"])

# Reuse the same graph cache from the chat module
_graphs: dict = {}


def _get_graph(checkpointer=None):
    key = id(checkpointer)
    if key not in _graphs:
        _graphs[key] = build_graph(checkpointer=checkpointer)
    return _graphs[key]


class HITLDecisionRequest(BaseModel):
    thread_id: str


async def _authorize_approver(http_request: Request, thread_id: str, approver: User) -> str:
    """Phase 3.5: enforce can_approve(approver_role, requester_role). Returns
    the requester_role on success; raises 403 (or 404 if thread is missing)."""
    pool = getattr(http_request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Approvals unavailable (no DB)")
    owner, requester_role = await get_thread_owner_role(pool, thread_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    if not can_approve(approver.role, requester_role or ""):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your role '{approver.role}' cannot approve requests from "
                f"role '{requester_role}'. Self-approval is not allowed."
            ),
        )
    return requester_role or ""


@router.post("/approve")
async def approve_response(
    body: HITLDecisionRequest,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Resume a HITL-paused graph with approval. Phase 3.5: caller's role must
    can_approve_for the requester's role (no self-approval)."""
    checkpointer = getattr(http_request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="HITL not available (no checkpointer)")

    requester_role = await _authorize_approver(http_request, body.thread_id, user)

    graph = _get_graph(checkpointer=checkpointer)
    config = {
        "configurable": {"thread_id": body.thread_id},
        "metadata": {"hitl_enabled": True},
    }

    try:
        result = await graph.ainvoke(Command(resume="approved"), config=config)
    except Exception as e:
        logger.error(f"HITL approve failed for thread {body.thread_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resume graph")

    metadata = result.get("response_metadata", {})
    logger.info(
        f"HITL approved: thread={body.thread_id} approver={user.user_id}/{user.role} "
        f"requester_role={requester_role}"
    )
    return {
        "status": "approved",
        "thread_id": body.thread_id,
        "approver_user_id": user.user_id,
        "approver_role": user.role,
        "response": result.get("final_response", ""),
        "sources": metadata.get("sources", []),
        "confidence": metadata.get("confidence"),
    }


@router.post("/reject")
async def reject_response(
    body: HITLDecisionRequest,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Resume a HITL-paused graph with rejection. Phase 3.5: caller's role must
    can_approve_for the requester's role."""
    checkpointer = getattr(http_request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="HITL not available (no checkpointer)")

    requester_role = await _authorize_approver(http_request, body.thread_id, user)

    graph = _get_graph(checkpointer=checkpointer)
    config = {
        "configurable": {"thread_id": body.thread_id},
        "metadata": {"hitl_enabled": True},
    }

    try:
        result = await graph.ainvoke(Command(resume="rejected"), config=config)
    except Exception as e:
        logger.error(f"HITL reject failed for thread {body.thread_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resume graph")

    logger.info(
        f"HITL rejected: thread={body.thread_id} approver={user.user_id}/{user.role} "
        f"requester_role={requester_role}"
    )
    return {
        "status": "rejected",
        "thread_id": body.thread_id,
        "approver_user_id": user.user_id,
        "approver_role": user.role,
        "response": result.get("final_response", ""),
    }
