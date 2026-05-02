"""Canonical company registry and alias utilities.

Supports both the original 3-company corpus and FinanceBench-scale corpora.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

DEFAULT_ALIASES: dict[str, set[str]] = {
    "apple": {"apple", "apple inc", "aapl"},
    "microsoft": {"microsoft", "microsoft corp", "microsoft corporation", "msft"},
    "tesla": {"tesla", "tesla inc", "tesla motors", "tsla"},
    "amazon": {"amazon", "amazon.com", "amzn"},
    "google": {"google", "alphabet", "goog", "googl"},
    "meta": {"meta", "meta platforms", "facebook", "fb"},
}


def _normalize(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^\w\s&.-]", " ", text)
    text = text.replace(".", " ")
    return re.sub(r"\s+", " ", text).strip()


def canonical_company_slug(company_name: str | None) -> str | None:
    """Canonicalize company names to lowercase slug."""
    if not company_name:
        return None
    normalized = _normalize(company_name)
    if not normalized:
        return None

    # Exact alias hit first
    for slug, aliases in build_company_alias_map().items():
        if normalized in aliases:
            return slug

    # Conservative fallback for short, explicit company-like names only.
    if len(normalized) <= 64 and " " in normalized:
        slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
        return slug or None
    return None


@lru_cache(maxsize=1)
def build_company_alias_map() -> dict[str, set[str]]:
    """Load alias map from defaults + FinanceBench metadata if available."""
    aliases = {slug: set(vals) for slug, vals in DEFAULT_ALIASES.items()}

    fb_path = Path("data/raw/financebench/financebench_document_information.jsonl")
    if fb_path.exists():
        for line in fb_path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            company = rec.get("company")
            if not company:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", _normalize(company)).strip("_")
            if not slug:
                continue
            aliases.setdefault(slug, set()).add(_normalize(company))
            # Include frequent short aliases (ticker-like tokens in doc_name)
            doc_name = _normalize(str(rec.get("doc_name", "")))
            parts = [p for p in re.split(r"[_\s-]+", doc_name) if p]
            if parts:
                aliases[slug].add(parts[0])

    return aliases


def all_company_slugs() -> list[str]:
    return sorted(build_company_alias_map().keys())

