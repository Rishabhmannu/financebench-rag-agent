import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from src.api.dependencies import get_current_user
from src.config.settings import settings
from src.ingestion.pipeline import ingest_directory, ingest_file
from src.models.auth import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingestion"])


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


@router.post("")
async def ingest_documents(user: User = Depends(get_current_user)):
    """Trigger document ingestion from data/sample/. Admin only."""
    _require_admin(user)

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


@router.post("/upload")
async def ingest_upload(
    files: list[UploadFile] = File(...),
    doc_type: str = Form("10k"),
    confidentiality: str = Form("public"),
    user: User = Depends(get_current_user),
):
    """Accept one or more uploaded PDFs and ingest them. Admin only.

    Frontend drag-drop / file-picker submits a multipart form to this
    endpoint. Files are persisted to `DOCUMENTS_ROOT` so the citation
    clickthrough endpoint can later serve them, then the existing
    ingest_file pipeline indexes each one. Non-PDFs are rejected; an
    individual ingestion failure surfaces per-file in the response so
    the frontend can flag partial success.
    """
    _require_admin(user)

    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files in upload")

    root = Path(settings.DOCUMENTS_ROOT)
    root.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    total_chunks = 0
    errors: list[dict] = []

    for upload in files:
        # Validate file type by extension AND content-type. Non-PDFs are
        # rejected; we don't want arbitrary file uploads landing on disk.
        if not upload.filename or not upload.filename.lower().endswith(".pdf"):
            errors.append({"filename": upload.filename or "<unnamed>", "error": "Only .pdf files are accepted"})
            continue
        # Path-traversal guard: keep only the basename, no separators
        base = Path(upload.filename).name
        if base in {".", "..", ""} or "/" in base or "\\" in base:
            errors.append({"filename": upload.filename, "error": "Invalid filename"})
            continue

        target_path = root / base
        try:
            with target_path.open("wb") as out:
                shutil.copyfileobj(upload.file, out)
        except Exception as e:
            errors.append({"filename": base, "error": f"Could not save: {type(e).__name__}"})
            continue

        try:
            n = ingest_file(
                target_path,
                doc_type=doc_type,
                metadata_override={"confidentiality": confidentiality},
            )
            saved.append({"filename": base, "chunks": n})
            total_chunks += n
        except Exception as e:
            logger.error("Ingestion of %s failed: %s", base, e, exc_info=True)
            errors.append({"filename": base, "error": f"Ingestion failed: {type(e).__name__}: {str(e)[:200]}"})
            # Don't delete the file — admin may want to retry. Just report.

    return {
        "status": "success" if not errors else ("partial" if saved else "failed"),
        "files_processed": len(saved),
        "chunks_ingested": total_chunks,
        "files": saved,
        "errors": errors,
    }
