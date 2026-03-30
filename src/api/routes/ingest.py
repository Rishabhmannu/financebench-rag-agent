import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.dependencies import get_current_user
from src.ingestion.pipeline import ingest_directory
from src.models.auth import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingestion"])


@router.post("")
async def ingest_documents(user: User = Depends(get_current_user)):
    """Trigger document ingestion from data/sample/. Admin only."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    sample_dir = Path("data/sample")
    if not sample_dir.exists():
        raise HTTPException(status_code=404, detail="No sample data directory found. Run scripts/download_sample_data.py first.")

    pdf_files = list(sample_dir.glob("*.pdf"))
    if not pdf_files:
        raise HTTPException(status_code=404, detail="No PDF files found in data/sample/")

    try:
        count = ingest_directory(sample_dir)
        logger.info(f"Ingested {count} chunks from {len(pdf_files)} PDF files")
        return {"status": "success", "chunks_ingested": count, "files_processed": len(pdf_files)}
    except Exception as e:
        logger.error(f"Ingestion failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")
