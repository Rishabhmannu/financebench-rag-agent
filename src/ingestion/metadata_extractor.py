"""Extract metadata from documents for RBAC filtering and source attribution."""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Heuristic patterns for document type detection
DOC_TYPE_PATTERNS = {
    "10k": [r"10-K", r"annual report", r"form 10-K", r"fiscal year ended"],
    "invoice": [r"invoice", r"bill to", r"amount due", r"payment terms"],
    "expense_policy": [r"expense policy", r"reimbursement", r"travel policy", r"per diem"],
}


def extract_metadata(file_path: Path, document: dict, doc_type_override: str | None = None) -> dict:
    """Extract metadata from a document file."""
    filename = file_path.stem.lower()
    text_preview = document.get("text", "")[:2000].lower()

    # Determine doc_type
    if doc_type_override:
        doc_type = doc_type_override
    else:
        doc_type = _detect_doc_type(filename, text_preview)

    # Determine confidentiality (default to public for now)
    confidentiality = "public"
    if "confidential" in text_preview or "internal use only" in text_preview:
        confidentiality = "internal"

    # Try to extract company name from filename or content
    company = _extract_company(filename, text_preview)

    return {
        "doc_type": doc_type,
        "company": company,
        "confidentiality": confidentiality,
        "source_file": file_path.name,
        "num_pages": document.get("num_pages", 0),
    }


def _detect_doc_type(filename: str, text_preview: str) -> str:
    """Detect document type from filename and content."""
    combined = filename + " " + text_preview
    for doc_type, patterns in DOC_TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, combined, re.IGNORECASE):
                return doc_type
    return "unknown"


def _extract_company(filename: str, text_preview: str) -> str:
    """Try to extract company name. Returns 'Unknown' if not found."""
    known_companies = ["apple", "microsoft", "tesla", "google", "amazon", "meta"]
    combined = filename + " " + text_preview
    for company in known_companies:
        if company in combined:
            return company.title() + " Inc."
    return "Unknown"
