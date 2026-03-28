from fastapi import APIRouter, Depends, HTTPException, status

from src.api.dependencies import get_current_user
from src.models.auth import User

router = APIRouter(prefix="/ingest", tags=["ingestion"])


@router.post("")
async def ingest_documents(user: User = Depends(get_current_user)):
    """Trigger document ingestion. Admin only."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    # TODO (Sprint 1): Implement ingestion pipeline trigger
    return {"status": "not_implemented", "message": "Ingestion pipeline coming in Sprint 1"}
