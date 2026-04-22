"""Load and convert PDFs. Returns per-page text so chunks can be tagged with page_number."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_pdf(file_path: Path) -> dict:
    """Convert a PDF to text, preserving page boundaries.

    Returns a dict with:
      - text: full document text (from Docling if available, else pypdf)
      - pages: list of {"page_number": int, "text": str} — used for per-page chunking
      - source_file: filename
      - num_pages: total page count
    """
    # Always extract per-page text via pypdf — Docling's export_to_markdown concatenates
    # pages without markers, so we need a separate pass to map chunks -> page numbers.
    pages = _extract_pages_pypdf(file_path)

    # Prefer Docling for the primary text (better tables/structure), fall back to pypdf.
    full_text = _extract_text_docling(file_path)
    if not full_text:
        full_text = "\n\n".join(p["text"] for p in pages)

    return {
        "text": full_text,
        "pages": pages,
        "tables": [],
        "source_file": file_path.name,
        "num_pages": len(pages),
    }


def _extract_text_docling(file_path: Path) -> str:
    """Extract full-document markdown via Docling. Returns empty string on failure."""
    try:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        result = converter.convert(str(file_path))
        return result.document.export_to_markdown()
    except Exception as e:
        logger.info(f"Docling unavailable for {file_path.name} ({e}); using pypdf text")
        return ""


def _extract_pages_pypdf(file_path: Path) -> list[dict]:
    """Extract per-page text via pypdf. Returns [] on failure."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        pages = []
        for page_no, page in enumerate(reader.pages, start=1):
            pages.append({
                "page_number": page_no,
                "text": page.extract_text() or "",
            })
        return pages
    except Exception as e:
        logger.error(f"pypdf extraction failed for {file_path}: {e}")
        return []
