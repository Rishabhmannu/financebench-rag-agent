"""RBAC-checked PDF document serving for the frontend citation clickthrough.

Frontend renders source chips on each answer (`{source_file, page}`); clicking
a chip opens the PDF in an in-browser viewer. That viewer needs to fetch the
PDF over HTTP — which means we expose the file but must NOT bypass RBAC.

Design:
  1. Resolve the requested filename to a Qdrant chunk and read its
     `doc_type` + `confidentiality` payload.
  2. Compare against the caller's role permissions; reject if not allowed.
  3. Validate the filename against path traversal (no ``../``, no absolute
     paths, no resolving outside DOCUMENTS_ROOT).
  4. Stream the file with `Content-Disposition: inline` so browsers render
     in-window rather than triggering a download.

If a filename has been ingested but its underlying file is missing on disk
we surface a 404 — Qdrant being the source of truth for "what's indexed"
means RBAC still applies even when the file is gone, but we don't fabricate
a fake response.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from qdrant_client.models import FieldCondition, Filter, MatchValue

from src.api.dependencies import get_current_user
from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.auth import User
from src.services.vector_store import get_qdrant_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


def _lookup_document_meta(filename: str) -> dict | None:
    """Fetch a single chunk for this filename to read doc_type + confidentiality.

    Qdrant `scroll` with a filter is the cheapest way — we only need one
    chunk to read the doc-level metadata (all chunks of one document share
    the same `doc_type` and `confidentiality`).
    """
    client = get_qdrant_client()
    try:
        points, _ = client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            scroll_filter=Filter(
                must=[FieldCondition(key="source_file", match=MatchValue(value=filename))]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        logger.warning("Qdrant scroll for %s failed: %s", filename, e)
        return None
    if not points:
        return None
    payload = points[0].payload or {}
    return {
        "doc_type": payload.get("doc_type"),
        "confidentiality": payload.get("confidentiality"),
    }


def _is_allowed(perms: dict, meta: dict) -> bool:
    """Apply the standard RBAC check: doc_type + confidentiality must be in
    the caller's allowed sets. Admin's `["*"]` wildcard short-circuits.
    """
    doc_type = meta.get("doc_type")
    conf = meta.get("confidentiality")
    allowed_types = perms.get("allowed_doc_types") or []
    allowed_conf = perms.get("allowed_confidentiality") or []
    type_ok = "*" in allowed_types or doc_type in allowed_types
    conf_ok = "*" in allowed_conf or conf in allowed_conf
    return type_ok and conf_ok


@router.get("/{filename}")
async def get_document(filename: str, user: User = Depends(get_current_user)):
    # Path traversal guard — the filename must be a plain basename living
    # under DOCUMENTS_ROOT. We reject anything with separators, parent refs,
    # or non-PDF extensions before touching the filesystem.
    if "/" in filename or "\\" in filename or ".." in filename or filename.startswith("."):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are served")

    root = Path(settings.DOCUMENTS_ROOT).resolve()
    target = (root / filename).resolve()
    # Defense-in-depth: even if the filename slipped past the basename check,
    # require the resolved path is inside DOCUMENTS_ROOT.
    if not str(target).startswith(str(root) + "/") and target != root:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename")

    # RBAC: read doc metadata from Qdrant + compare to caller's role perms.
    # We do this BEFORE checking the file exists so we don't leak "this
    # filename is indexed" to unauthorized callers via a 404 vs 403.
    meta = _lookup_document_meta(filename)
    if meta is None:
        # Not in the index → file isn't a known document, treat as not found
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    perms = get_permissions(user.role)
    if not _is_allowed(perms, meta):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access to this document is not permitted for your role")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document file not on disk")

    return FileResponse(
        path=str(target),
        media_type="application/pdf",
        filename=filename,
        # `inline` so PDF viewer embeds in the page; not as an attachment
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
