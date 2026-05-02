"""Extract metadata from documents for RBAC filtering and source attribution.

The `company` field is stored as a lowercase slug ("apple", "microsoft", "tesla",
"unknown") to support deterministic Qdrant payload filtering. The full display
name lives in `company_name` ("Apple Inc.").
"""

import logging
import re
from pathlib import Path

from src.services.company_registry import build_company_alias_map, canonical_company_slug

logger = logging.getLogger(__name__)

# Heuristic patterns for document type detection
DOC_TYPE_PATTERNS = {
    "10k": [r"10-K", r"annual report", r"form 10-K", r"fiscal year ended"],
    "invoice": [r"invoice", r"bill to", r"amount due", r"payment terms"],
    "expense_policy": [r"expense policy", r"reimbursement", r"travel policy", r"per diem"],
}

# Slug -> display name. Slug is what lands in Qdrant's `company` payload field
# and what the entity_extractor node emits at query time.
KNOWN_COMPANIES: dict[str, str] = {
    "apple": "Apple Inc.",
    "microsoft": "Microsoft Inc.",
    "tesla": "Tesla Inc.",
    "google": "Google Inc.",
    "amazon": "Amazon Inc.",
    "meta": "Meta Platforms Inc.",
}

UNKNOWN_COMPANY_SLUG = "unknown"
UNKNOWN_COMPANY_NAME = "Unknown"


def _extract_fiscal_year(filename: str, text_preview: str) -> int | None:
    combined = f"{filename} {text_preview}"
    m = re.search(r"\b(20[0-3]\d)\b", combined)
    return int(m.group(1)) if m else None


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

    # Extract company as a lowercase slug for filtering; name for display
    company_slug = _extract_company_slug(filename, text_preview)
    company_name = KNOWN_COMPANIES.get(company_slug, UNKNOWN_COMPANY_NAME)
    fiscal_year = _extract_fiscal_year(filename, text_preview)

    return {
        "doc_type": doc_type,
        "company": company_slug,
        "company_name": company_name,
        "fiscal_year": fiscal_year,
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


def _extract_company_slug(filename: str, text_preview: str) -> str:
    """Return a lowercase slug for the company, or UNKNOWN_COMPANY_SLUG if not found."""
    combined = filename + " " + text_preview
    normalized = combined.lower()
    for slug, aliases in build_company_alias_map().items():
        for alias in aliases:
            if alias and alias in normalized:
                return slug
    inferred = canonical_company_slug(filename)
    if inferred:
        return inferred
    for slug in KNOWN_COMPANIES:
        if slug in combined:
            return slug
    return UNKNOWN_COMPANY_SLUG


# Legacy alias — some older callers imported `_extract_company` expecting the
# display name. Kept as a thin wrapper to avoid silent breakage.
def _extract_company(filename: str, text_preview: str) -> str:
    slug = _extract_company_slug(filename, text_preview)
    return KNOWN_COMPANIES.get(slug, UNKNOWN_COMPANY_NAME)
