import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph.types import Command
from pydantic import BaseModel

from src.api.dependencies import get_current_user
from src.graph.builder import build_graph
from src.models.auth import User

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


@router.post("/approve")
async def approve_response(
    body: HITLDecisionRequest,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Resume a HITL-paused graph with approval."""
    checkpointer = getattr(http_request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="HITL not available (no checkpointer)")

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
    return {
        "status": "approved",
        "thread_id": body.thread_id,
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
    """Resume a HITL-paused graph with rejection."""
    checkpointer = getattr(http_request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="HITL not available (no checkpointer)")

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

    return {
        "status": "rejected",
        "thread_id": body.thread_id,
        "response": result.get("final_response", ""),
    }
