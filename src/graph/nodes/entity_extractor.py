"""Entity extractor — pre-retrieval company + fiscal year detection.

Problem this solves (surfaced in Sprint 7a): hybrid retrieval (dense + BM25)
returns chunks from the wrong company because all 10-Ks share terminology.
Asking "Apple operating income" pulls Microsoft's chunks too.

Fix: extract the target entity from the query *before* retrieval, and pass it
as a Qdrant payload filter alongside RBAC. Retrieval then physically cannot
return cross-company chunks.

Strategy: two-tier for latency + cost control.
  1. Dictionary/regex pass — deterministic, zero LLM cost, handles 90% of cases
     (explicit mentions, common tickers).
  2. Groq LLM fallback — only when the dictionary pass is ambiguous (e.g.
     pronoun follow-ups "what about their R&D?", or multi-entity mentions).

Emits `target_company` (lowercase slug or None) and `target_fiscal_year`
(integer or None) into state.
"""

import logging
import re

from langchain_core.messages import AIMessage, HumanMessage

from src.config.prompts import ENTITY_EXTRACTOR_PROMPT
from src.models.schemas import EntityExtraction
from src.models.state import RAGState
from src.services.company_registry import all_company_slugs, build_company_alias_map, canonical_company_slug
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)

# Runtime-loaded alias map supports the base corpus + FinanceBench.
COMPANY_ALIASES = build_company_alias_map()

# Year patterns. FULL = 4-digit 20XX (lookarounds so "FY2022" matches — \b fails
# between letters and digits). SHORT = FY-prefixed 2-digit ("FY22" / "FY 22").
YEAR_PATTERN_FULL = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
YEAR_PATTERN_SHORT = re.compile(r"\bFY\s?(\d{2})\b", re.IGNORECASE)


def _dictionary_match(query: str) -> tuple[str | None, bool]:
    """Return (slug_or_None, is_ambiguous).

    - (slug, False): exactly one company matched — we're confident.
    - (None, False): no company mentioned — likely a scoped-to-filter-skip case.
    - (None, True): multiple companies mentioned OR pronoun-like hints found →
                    defer to LLM fallback.
    """
    q = query.lower()
    matches: set[str] = set()
    for slug, aliases in build_company_alias_map().items():
        for alias in aliases:
            # Word-boundary match — avoids "appleseed" matching "apple"
            if re.search(rf"\b{re.escape(alias)}\b", q):
                matches.add(slug)
                break

    if len(matches) == 1:
        return matches.pop(), False
    if len(matches) > 1:
        # Comparative query ("compare Apple and MSFT"). LLM may still want to
        # return None, but we ask it to confirm rather than deciding here.
        return None, True

    # No explicit match. Pronouns/references ("their", "that company", "what about…")
    # suggest the query depends on conversation context → LLM fallback.
    pronoun_hints = ("their ", "that company", "what about", "how about", "its ")
    if any(hint in q for hint in pronoun_hints):
        return None, True

    return None, False


def _extract_year(query: str) -> int | None:
    """Extract the target fiscal year from the query.

    For multi-year queries ("FY2021 and FY2022", "FY2018 - FY2020 average") the
    target is the *latest* year — that's the fiscal year of the source 10-K,
    which discusses all comparison years inside.
    """
    full = [int(y) for y in YEAR_PATTERN_FULL.findall(query)]
    short = [2000 + int(y) for y in YEAR_PATTERN_SHORT.findall(query)]
    years = full + short
    return max(years) if years else None


def _format_history(messages: list) -> str:
    """Compact prior conversation for the LLM fallback prompt."""
    prior = messages[:-1] if messages else []
    if not prior:
        return "(no prior conversation)"
    lines = []
    for msg in prior[-4:]:  # last 2 turns (human+ai pair x 2)
        if isinstance(msg, HumanMessage):
            lines.append(f"User: {msg.content[:150]}")
        elif isinstance(msg, AIMessage):
            lines.append(f"Assistant: {msg.content[:250]}")
    return "\n".join(lines) if lines else "(no prior conversation)"


def _llm_fallback(query: str, messages: list) -> EntityExtraction:
    """Call Groq (routing-tier LLM) to resolve ambiguous cases via structured output."""
    try:
        llm = LLMFactory.get_router_llm()
        structured = llm.with_structured_output(EntityExtraction)
        prompt = ENTITY_EXTRACTOR_PROMPT.format(
            history=_format_history(messages),
            query=query,
        )
        result: EntityExtraction = structured.invoke([HumanMessage(content=prompt)])
        if result.company:
            result.company = canonical_company_slug(result.company)
        return result
    except Exception as e:
        logger.warning(f"Entity LLM fallback failed, returning empty extraction: {e}")
        return EntityExtraction(company=None, fiscal_year=None)


def entity_extractor_node(state: RAGState) -> dict:
    """Resolve target_company and target_fiscal_year from the query + history.

    Runs between guardrails and router. The sanitized query (post-PII + post-
    contextualization) is what we extract from so pronouns are already resolved.
    """
    query = state.get("sanitized_query") or ""
    messages = state.get("messages", [])

    if not query:
        return {"target_company": None, "target_fiscal_year": None}

    # Tier 1: dictionary (zero LLM cost)
    slug, ambiguous = _dictionary_match(query)
    year = _extract_year(query)

    # Tier 2: LLM fallback only if dictionary was ambiguous (multiple companies,
    # pronoun, etc.). A clean single-company match skips the LLM entirely.
    if ambiguous:
        parsed = _llm_fallback(query, messages)
        slug = parsed.company
        # Prefer regex-extracted year (deterministic) if found; else LLM's
        year = year if year is not None else parsed.fiscal_year
        logger.info(f"Entity LLM fallback: company={slug}, year={year}")
    elif slug:
        logger.info(f"Entity dictionary match: company={slug}, year={year}")
    else:
        # No company and no pronouns — generic query. Skip filter (retrieval
        # will search across all companies, which is the intended behavior).
        logger.info(f"No target entity extracted for query: {query[:60]}...")

    if slug and slug not in all_company_slugs():
        # Accept unknown slug shape if it is normalized (future corpora), but
        # prevent accidental long-text outputs from structured parser failures.
        normalized = canonical_company_slug(slug)
        slug = normalized

    return {
        "target_company": slug,
        "target_fiscal_year": year,
    }
