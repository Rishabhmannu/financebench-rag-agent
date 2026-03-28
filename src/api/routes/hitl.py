from fastapi import APIRouter

router = APIRouter(prefix="/hitl", tags=["human-in-the-loop"])


@router.post("/approve")
async def approve_response(thread_id: str):
    """Approve a HITL-paused response."""
    # TODO (Sprint 3): Implement with LangGraph interrupt resume
    return {"status": "not_implemented", "thread_id": thread_id}


@router.post("/reject")
async def reject_response(thread_id: str):
    """Reject a HITL-paused response."""
    # TODO (Sprint 3): Implement with LangGraph interrupt resume
    return {"status": "not_implemented", "thread_id": thread_id}
