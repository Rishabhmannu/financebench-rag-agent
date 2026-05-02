"""Deterministic candidate validator for post-reranker filtering."""

from __future__ import annotations


def _year_from_chunk(chunk: dict) -> int | None:
    meta = chunk.get("metadata") or {}
    year = meta.get("fiscal_year")
    if isinstance(year, int):
        return year
    if isinstance(year, str) and year.isdigit():
        return int(year)
    fb_period = str(meta.get("fb_doc_period", "")).strip()
    return int(fb_period) if fb_period.isdigit() else None


def validate_candidates(
    *,
    query: str,
    candidates: list[dict],
    target_company: str | None,
    target_fiscal_year: int | None,
    min_keep: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Validate candidates with deterministic entity/year checks.

    Returns:
        (filtered_candidates, diagnostics)
    """
    diagnostics: list[dict] = []
    keep: list[dict] = []
    rejected: list[dict] = []

    for idx, chunk in enumerate(candidates):
        meta = chunk.get("metadata") or {}
        chunk_company = meta.get("company")
        chunk_year = _year_from_chunk(chunk)
        entity_match = target_company is None or chunk_company == target_company
        year_match = target_fiscal_year is None or chunk_year == target_fiscal_year
        valid = entity_match and year_match
        diagnostics.append(
            {
                "candidate_id": idx,
                "entity_match": entity_match,
                "year_match": year_match,
                "valid": valid,
                "chunk_company": chunk_company,
                "chunk_year": chunk_year,
            }
        )
        (keep if valid else rejected).append(chunk)

    # Fallback relaxation: if validator is too strict, keep top candidates.
    if len(keep) < min_keep:
        keep = candidates[: max(min_keep, len(keep))]
        diagnostics.append(
            {
                "relaxed": True,
                "reason": "min_keep_guard",
                "min_keep": min_keep,
                "kept_after_relaxation": len(keep),
            }
        )

    return keep, diagnostics

