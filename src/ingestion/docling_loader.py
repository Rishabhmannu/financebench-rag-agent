"""Load and convert PDFs.

Returns per-page text (from pypdf) AND the full Docling `DoclingDocument`
object when Docling is available, so the chunker can pick its preferred path:

  - `docling_doc` present → chunker uses Docling `HybridChunker` (table- and
    section-aware, carries structured metadata like section_header + page)
  - `docling_doc` missing (SKIP_DOCLING=1 or Docling failed) → chunker falls
    back to per-page pypdf chunking

Set env var SKIP_DOCLING=1 to bypass Docling entirely (saves ~2.5 min per
large PDF but gives up the structured-chunking gains).
"""

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_pdf(file_path: Path) -> dict:
    """Convert a PDF to text, preserving page boundaries.

    Returns a dict with:
      - text:        full document text (Docling markdown if available, else pypdf joined)
      - pages:       list of {"page_number": int, "text": str} — pypdf-based fallback
      - docling_doc: the DoclingDocument object when Docling parsed successfully,
                     else None. The chunker uses this to invoke Docling HybridChunker.
      - source_file: filename
      - num_pages:   total page count
    """
    pages = _extract_pages_pypdf(file_path)

    docling_doc: Any | None = None
    full_text = ""

    if os.environ.get("SKIP_DOCLING") == "1":
        full_text = "\n\n".join(p["text"] for p in pages)
    else:
        docling_doc = _parse_with_docling(file_path)
        if docling_doc is not None:
            try:
                full_text = docling_doc.export_to_markdown()
            except Exception as e:
                logger.warning(f"Docling parsed {file_path.name} but export_to_markdown failed: {e}")
                full_text = ""
        if not full_text:
            full_text = "\n\n".join(p["text"] for p in pages)

    return {
        "text": full_text,
        "pages": pages,
        "docling_doc": docling_doc,
        "tables": [],
        "source_file": file_path.name,
        "num_pages": len(pages),
    }


def _parse_with_docling(file_path: Path):
    """Parse the PDF with Docling. Returns the DoclingDocument or None on failure."""
    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        logger.info("docling not installed; falling back to pypdf")
        return None

    try:
        converter = DocumentConverter()
        result = converter.convert(str(file_path))
        return result.document
    except Exception as e:
        logger.info(f"Docling failed on {file_path.name} ({type(e).__name__}: {e}); falling back to pypdf")
        return None


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
