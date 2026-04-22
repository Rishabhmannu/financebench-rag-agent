"""Unit tests for src/graph/nodes/entity_extractor.py.

Covers the deterministic dictionary-match tier and year extraction. The LLM
fallback tier is not tested here (integration-level — requires a live LLM).
"""

from src.graph.nodes.entity_extractor import (
    _dictionary_match,
    _extract_year,
    entity_extractor_node,
)


# ---------------------------------------------------------------------------
# Dictionary match — single-entity queries (fast path, no LLM)
# ---------------------------------------------------------------------------

def test_dict_match_apple_by_name():
    slug, ambiguous = _dictionary_match("What was Apple's revenue in 2023?")
    assert slug == "apple"
    assert ambiguous is False


def test_dict_match_microsoft_by_ticker():
    slug, ambiguous = _dictionary_match("How much did MSFT spend on R&D?")
    assert slug == "microsoft"
    assert ambiguous is False


def test_dict_match_tesla_by_ticker():
    slug, ambiguous = _dictionary_match("TSLA operating margin?")
    assert slug == "tesla"
    assert ambiguous is False


def test_dict_match_case_insensitive():
    assert _dictionary_match("APPLE revenue")[0] == "apple"
    assert _dictionary_match("aapl income")[0] == "apple"


def test_dict_match_word_boundary_prevents_substring_match():
    """'apple' in 'appleseed' should NOT match — word boundary enforced."""
    slug, _ = _dictionary_match("appleseed corporation quarterly results")
    assert slug is None


# ---------------------------------------------------------------------------
# Dictionary match — ambiguous cases (LLM fallback triggered)
# ---------------------------------------------------------------------------

def test_dict_match_multiple_companies_is_ambiguous():
    slug, ambiguous = _dictionary_match("Compare Apple and Microsoft revenue")
    assert slug is None
    assert ambiguous is True


def test_dict_match_three_companies_is_ambiguous():
    slug, ambiguous = _dictionary_match("Which of Apple, MSFT, or TSLA grew fastest?")
    assert slug is None
    assert ambiguous is True


def test_dict_match_pronoun_triggers_fallback():
    slug, ambiguous = _dictionary_match("What about their R&D spend?")
    assert slug is None
    assert ambiguous is True


def test_dict_match_what_about_with_explicit_company_is_not_ambiguous():
    """When the query explicitly names ONE company, we resolve it even if
    'what about' is present — the explicit entity wins over the follow-up hint.

    (Upstream guardrails contextualization already rewrote any pronoun to the
    explicit name before this node runs, so pronoun-only follow-ups don't
    reach us.)
    """
    slug, ambiguous = _dictionary_match("What about Microsoft?")
    assert slug == "microsoft"
    assert ambiguous is False


# ---------------------------------------------------------------------------
# Dictionary match — no company mentioned (valid generic query, no filter)
# ---------------------------------------------------------------------------

def test_dict_match_generic_query_returns_none_non_ambiguous():
    """A query with no companies and no pronoun hints → no filter needed."""
    slug, ambiguous = _dictionary_match("What is the maximum daily travel expense?")
    assert slug is None
    assert ambiguous is False


# ---------------------------------------------------------------------------
# Year extraction
# ---------------------------------------------------------------------------

def test_extract_year_finds_2023():
    assert _extract_year("Apple revenue in fiscal year 2023") == 2023


def test_extract_year_finds_2024():
    assert _extract_year("Microsoft's 2024 earnings") == 2024


def test_extract_year_returns_none_when_absent():
    assert _extract_year("What was Apple's revenue?") is None


def test_extract_year_ignores_out_of_range_years():
    """Years outside 2020-2099 should not match (no 19XX or 1XXX)."""
    assert _extract_year("Founded in 1976, Apple...") is None


def test_extract_year_picks_first_match():
    # Our regex returns the first 20XX year found — acceptable for simple queries
    assert _extract_year("Compare 2022 to 2023 revenue") == 2022


# ---------------------------------------------------------------------------
# Node integration (deterministic path — no LLM call)
# ---------------------------------------------------------------------------

def test_node_extracts_apple_and_year_without_llm():
    state = {
        "sanitized_query": "What was Apple's total revenue in 2023?",
        "messages": [],
    }
    result = entity_extractor_node(state)
    assert result == {"target_company": "apple", "target_fiscal_year": 2023}


def test_node_returns_none_for_generic_query():
    state = {
        "sanitized_query": "What is the travel expense policy?",
        "messages": [],
    }
    result = entity_extractor_node(state)
    assert result == {"target_company": None, "target_fiscal_year": None}


def test_node_empty_query_short_circuits():
    state = {"sanitized_query": "", "messages": []}
    result = entity_extractor_node(state)
    assert result == {"target_company": None, "target_fiscal_year": None}


def test_node_missing_sanitized_query_key_defaults_to_none():
    result = entity_extractor_node({})
    assert result == {"target_company": None, "target_fiscal_year": None}
