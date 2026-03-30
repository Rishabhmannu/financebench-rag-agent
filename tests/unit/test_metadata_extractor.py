"""Unit tests for src/ingestion/metadata_extractor.py."""

import pytest
from pathlib import Path

from src.ingestion.metadata_extractor import (
    _detect_doc_type,
    _extract_company,
    extract_metadata,
)


# ---------------------------------------------------------------------------
# _detect_doc_type tests
# ---------------------------------------------------------------------------

def test_detect_doc_type_10k_in_filename():
    """Filenames containing '10-k' or '10k' patterns are detected as '10k'."""
    assert _detect_doc_type("apple_10-k_2024", "") == "10k"


def test_detect_doc_type_annual_report_in_filename():
    """'annual report' pattern in filename is detected as '10k' via regex."""
    # The pattern list includes "annual report" which uses re.search on combined text
    assert _detect_doc_type("annual report filing", "") == "10k"


def test_detect_doc_type_invoice_in_filename():
    """Filenames containing 'invoice' are detected as 'invoice'."""
    assert _detect_doc_type("tesla_invoice_q1_2024", "") == "invoice"


def test_detect_doc_type_invoice_keyword_in_filename():
    """Filenames with the word 'invoice' are detected as 'invoice'."""
    assert _detect_doc_type("customer_invoice_march", "") == "invoice"


def test_detect_doc_type_expense_policy_in_filename():
    """Filenames containing 'expense policy' or 'reimbursement' are detected as 'expense_policy'."""
    assert _detect_doc_type("expense policy 2024", "") == "expense_policy"


def test_detect_doc_type_falls_back_to_text_content():
    """When the filename has no match, doc type is detected from text content."""
    assert _detect_doc_type("generic_document", "this is a 10-K filing for fiscal year ended") == "10k"


def test_detect_doc_type_invoice_from_text_content():
    """Invoice detection from text content when filename is generic."""
    assert _detect_doc_type("report_2024", "invoice number: 12345, bill to: acme corp") == "invoice"


def test_detect_doc_type_returns_unknown_for_unrecognized():
    """Unrecognized filenames with no matching text patterns return 'unknown'."""
    assert _detect_doc_type("random_notes", "some unrelated content about weather") == "unknown"


# ---------------------------------------------------------------------------
# _extract_company tests
# ---------------------------------------------------------------------------

def test_extract_company_from_filename():
    """Company name is extracted when it appears in the filename."""
    result = _extract_company("apple_10k_2024", "")
    assert result == "Apple Inc."


def test_extract_company_from_text_content():
    """Company name is extracted from text when filename has no match."""
    result = _extract_company("generic_report", "this report covers microsoft quarterly earnings")
    assert result == "Microsoft Inc."


def test_extract_company_tesla():
    """Tesla is correctly identified and formatted."""
    result = _extract_company("tesla_annual", "")
    assert result == "Tesla Inc."


def test_extract_company_returns_unknown_when_no_match():
    """Returns 'Unknown' when no known company is found."""
    result = _extract_company("quarterly_report", "some numbers and data about widgets")
    assert result == "Unknown"


# ---------------------------------------------------------------------------
# Confidentiality detection (tested via extract_metadata since it is inline)
# ---------------------------------------------------------------------------

def test_confidentiality_detects_confidential():
    """Text containing 'confidential' results in 'internal' confidentiality."""
    doc = {"text": "This document is strictly confidential and should not be shared."}
    result = extract_metadata(Path("report.pdf"), doc)
    assert result["confidentiality"] == "internal"


def test_confidentiality_detects_internal_use_only():
    """Text containing 'internal use only' results in 'internal' confidentiality."""
    doc = {"text": "For internal use only. Distribution is restricted."}
    result = extract_metadata(Path("memo.pdf"), doc)
    assert result["confidentiality"] == "internal"


def test_confidentiality_defaults_to_public():
    """Text with no confidentiality markers defaults to 'public'."""
    doc = {"text": "General public information about the quarterly results."}
    result = extract_metadata(Path("public_report.pdf"), doc)
    assert result["confidentiality"] == "public"


# ---------------------------------------------------------------------------
# extract_metadata integration tests
# ---------------------------------------------------------------------------

def test_extract_metadata_uses_doc_type_override():
    """When doc_type_override is provided, it takes precedence over detection."""
    doc = {"text": "This is an invoice document."}
    result = extract_metadata(Path("random_file.pdf"), doc, doc_type_override="expense_policy")
    assert result["doc_type"] == "expense_policy"


def test_extract_metadata_returns_complete_dict():
    """extract_metadata returns a dict with all required keys."""
    doc = {"text": "Apple Inc. annual report for fiscal year.", "num_pages": 42}
    result = extract_metadata(Path("apple_10k_2024.pdf"), doc)

    required_keys = {"doc_type", "company", "confidentiality", "source_file", "num_pages"}
    assert required_keys == set(result.keys()), (
        f"Missing keys: {required_keys - set(result.keys())}"
    )
    assert result["doc_type"] == "10k"
    assert result["company"] == "Apple Inc."
    assert result["confidentiality"] == "public"
    assert result["source_file"] == "apple_10k_2024.pdf"
    assert result["num_pages"] == 42


def test_extract_metadata_source_file_is_filename_only():
    """source_file should be just the filename, not the full path."""
    doc = {"text": "Some content."}
    result = extract_metadata(Path("/data/raw/tesla_invoice.pdf"), doc)
    assert result["source_file"] == "tesla_invoice.pdf"


def test_extract_metadata_num_pages_defaults_to_zero():
    """When num_pages is not in the document dict, it defaults to 0."""
    doc = {"text": "Some content."}
    result = extract_metadata(Path("report.pdf"), doc)
    assert result["num_pages"] == 0
