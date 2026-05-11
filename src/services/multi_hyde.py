"""Multi-HyDE: generate N hypothetical 10-K-style answers per query.

The retrieval pipeline embeds each hypothetical alongside the original query
and runs hybrid_search with all paths, then RRF-fuses + dedupes the results.
The hypotheticals shift the query into the documents' vocabulary, narrowing
the question-phrasing vs disclosure-phrasing gap that dense embeddings
otherwise miss.

Failure mode: any exception (LLM rate limit, malformed output, etc.) returns
an empty list — the caller falls back to single-query retrieval. Multi-HyDE
is a recall booster, not a load-bearing dependency.

Reference: arXiv 2509.16369 (Multi-HyDE on financial QA: +11.2% accuracy,
-15% hallucinations vs single-HyDE).
"""
from __future__ import annotations

import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from src.config.prompts import MULTI_HYDE_PROMPT
from src.config.settings import settings
from src.services.llm_factory import _llm_for_task
from src.services.result_cache import get_or_compute

logger = logging.getLogger(__name__)


def _parse_hypotheticals(text: str, n: int) -> list[str]:
    """Split the LLM output into N paragraphs.

    The prompt asks for blank-line separated passages. We tolerate occasional
    numbered prefixes ("1.", "2)", etc.) and surrounding whitespace.
    """
    if not text:
        return []
    chunks = re.split(r"\n\s*\n+", text.strip())
    cleaned: list[str] = []
    for c in chunks:
        c = re.sub(r"^\s*(\d+[.)]\s+|[-*]\s+)", "", c).strip()
        if c:
            cleaned.append(c)
    return cleaned[:n]


def _generate_uncached(
    query: str,
    target_company: str | None,
    target_fiscal_year: int | None,
    n: int,
) -> list[str]:
    llm = _llm_for_task(
        settings.MULTI_HYDE_MODEL,
        temperature=settings.MULTI_HYDE_TEMPERATURE,
        max_tokens=1024,
    )
    prompt = MULTI_HYDE_PROMPT.format(
        n=n,
        target_company=target_company or "(not specified)",
        target_fiscal_year=target_fiscal_year or "(not specified)",
        query=query,
    )
    try:
        result = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=f"Generate {n} hypothetical passages now."),
        ])
        raw = result.content if isinstance(result.content, str) else str(result.content)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Multi-HyDE generation failed ({type(exc).__name__}): {exc}")
        return []

    hypotheticals = _parse_hypotheticals(raw, n)
    if len(hypotheticals) < n:
        logger.warning(
            f"Multi-HyDE: parsed {len(hypotheticals)}/{n} passages from output. "
            f"Proceeding with what we got."
        )
    return hypotheticals


def generate_hypotheticals(
    query: str,
    target_company: str | None = None,
    target_fiscal_year: int | None = None,
    n: int | None = None,
) -> list[str]:
    """Generate N hypothetical 10-K-style answers for the query.

    Returns up to N passages; empty list on failure (caller should fall back).
    Cached by (model, temperature, n, query, company, fiscal_year) so eval
    re-runs and retrieval retries don't pay the LLM cost twice.
    """
    if n is None:
        n = settings.MULTI_HYDE_N
    if not query or n <= 0:
        return []

    return get_or_compute(
        "multi-hyde",
        (
            settings.MULTI_HYDE_MODEL,
            f"{settings.MULTI_HYDE_TEMPERATURE:.3f}",
            str(n),
            query,
            target_company or "",
            str(target_fiscal_year) if target_fiscal_year else "",
        ),
        lambda: _generate_uncached(query, target_company, target_fiscal_year, n),
    )
