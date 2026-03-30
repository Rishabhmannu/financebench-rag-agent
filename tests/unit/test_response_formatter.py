"""Unit tests for the response_formatter_node."""

import pytest

from src.graph.nodes.response_formatter import response_formatter_node


def _make_chunk(source_file, page_number=1, section_header="", doc_type="10k"):
    """Helper to build a chunk dict matching the format expected by the formatter."""
    return {
        "content": "Some chunk text.",
        "metadata": {
            "source_file": source_file,
            "page_number": page_number,
            "section_header": section_header,
            "doc_type": doc_type,
        },
    }


# ── Formats answer with source citations correctly ─────────────────────────

def test_formats_answer_with_source_citations():
    state = {
        "generated_answer": "Revenue grew 12% year-over-year.",
        "relevant_chunks": [
            _make_chunk("annual_report_2024.pdf", page_number=5, section_header="Revenue", doc_type="10k"),
            _make_chunk("earnings_q4.pdf", page_number=2, section_header="Summary", doc_type="earnings"),
        ],
        "hallucination_score": 0.9,
        "hallucination_status": "grounded",
    }

    result = response_formatter_node(state)

    assert result["final_response"] == "Revenue grew 12% year-over-year."
    sources = result["response_metadata"]["sources"]
    assert len(sources) == 2
    assert sources[0]["file"] == "annual_report_2024.pdf"
    assert sources[0]["page"] == 5
    assert sources[0]["section"] == "Revenue"
    assert sources[0]["doc_type"] == "10k"
    assert sources[1]["file"] == "earnings_q4.pdf"


# ── Deduplicates sources by source_file ────────────────────────────────────

def test_deduplicates_sources():
    state = {
        "generated_answer": "Answer.",
        "relevant_chunks": [
            _make_chunk("report.pdf", page_number=1),
            _make_chunk("report.pdf", page_number=3),
            _make_chunk("other.pdf", page_number=1),
        ],
        "hallucination_score": 0.8,
        "hallucination_status": "grounded",
    }

    result = response_formatter_node(state)
    sources = result["response_metadata"]["sources"]

    # Only two unique source_files
    assert len(sources) == 2
    source_files = [s["file"] for s in sources]
    assert "report.pdf" in source_files
    assert "other.pdf" in source_files


# ── Adds disclaimer when hallucination_status is "hallucinated" ────────────

def test_adds_disclaimer_when_hallucinated():
    state = {
        "generated_answer": "The company earned $5B.",
        "relevant_chunks": [_make_chunk("report.pdf")],
        "hallucination_score": 0.3,
        "hallucination_status": "hallucinated",
    }

    result = response_formatter_node(state)

    assert result["final_response"].startswith("**Note:**")
    assert "could not be fully verified" in result["final_response"]
    # The original answer must still appear after the disclaimer
    assert "The company earned $5B." in result["final_response"]


# ── No disclaimer when hallucination_status is "grounded" ─────────────────

def test_no_disclaimer_when_grounded():
    state = {
        "generated_answer": "Net income was $1.2B.",
        "relevant_chunks": [_make_chunk("report.pdf")],
        "hallucination_score": 0.95,
        "hallucination_status": "grounded",
    }

    result = response_formatter_node(state)

    assert result["final_response"] == "Net income was $1.2B."
    assert "**Note:**" not in result["final_response"]


# ── Handles empty generated_answer ────────────────────────────────────────

def test_handles_empty_generated_answer():
    """When generated_answer is empty, final_response should be the empty string
    (no crash, and no disclaimer prefix on an empty answer)."""
    state = {
        "generated_answer": "",
        "relevant_chunks": [_make_chunk("report.pdf")],
        "hallucination_score": 0.0,
        "hallucination_status": "unknown",
    }

    result = response_formatter_node(state)

    # The node returns the empty answer as-is (status is not "hallucinated")
    assert result["final_response"] == ""
    assert "response_metadata" in result


# ── Handles empty relevant_chunks ─────────────────────────────────────────

def test_handles_empty_relevant_chunks():
    state = {
        "generated_answer": "Some fallback answer.",
        "relevant_chunks": [],
        "hallucination_score": 0.0,
        "hallucination_status": "unknown",
    }

    result = response_formatter_node(state)

    assert result["final_response"] == "Some fallback answer."
    assert result["response_metadata"]["sources"] == []
    assert result["response_metadata"]["chunks_used"] == 0


# ── response_metadata includes correct keys ───────────────────────────────

def test_response_metadata_keys():
    state = {
        "generated_answer": "Answer text.",
        "relevant_chunks": [
            _make_chunk("a.pdf"),
            _make_chunk("b.pdf"),
        ],
        "hallucination_score": 0.85,
        "hallucination_status": "grounded",
    }

    result = response_formatter_node(state)
    meta = result["response_metadata"]

    assert "sources" in meta
    assert "confidence" in meta
    assert "chunks_used" in meta
    assert "hallucination_status" in meta

    assert meta["confidence"] == 0.85
    assert meta["chunks_used"] == 2
    assert meta["hallucination_status"] == "grounded"
    assert isinstance(meta["sources"], list)


# ── Handles missing state keys gracefully (uses defaults) ─────────────────

def test_handles_missing_state_keys():
    """The node uses .get() with defaults, so an empty dict should not crash."""
    state = {}

    result = response_formatter_node(state)

    assert result["final_response"] == ""
    assert result["response_metadata"]["sources"] == []
    assert result["response_metadata"]["chunks_used"] == 0
    assert result["response_metadata"]["confidence"] == 0.0
    assert result["response_metadata"]["hallucination_status"] == "unknown"
