"""Load and convert PDFs using Docling."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_pdf(file_path: Path) -> dict:
    """Convert a PDF to a Docling document. Returns a dict with content and structure."""
    try:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        result = converter.convert(str(file_path))
        doc = result.document

        return {
            "text": doc.export_to_markdown(),
            "tables": [],  # TODO: extract tables separately for better chunking
            "source_file": file_path.name,
            "num_pages": len(doc.pages) if hasattr(doc, "pages") else 0,
        }
    except Exception as e:
        logger.error(f"Docling conversion failed for {file_path}: {e}")
        # Fallback: basic text extraction
        logger.info("Falling back to basic PDF text extraction")
        return _fallback_load(file_path)


def _fallback_load(file_path: Path) -> dict:
    """Fallback PDF loading without Docling."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        return {
            "text": text,
            "tables": [],
            "source_file": file_path.name,
            "num_pages": len(reader.pages),
        }
    except Exception as e:
        logger.error(f"Fallback PDF loading also failed: {e}")
        return {"text": "", "tables": [], "source_file": file_path.name, "num_pages": 0}
