"""Shared test fixtures."""

import pytest

from src.models.state import RAGState


@pytest.fixture
def base_state() -> RAGState:
    """A clean initial state for testing individual nodes."""
    return {
        "messages": [],
        "user_id": "test_user",
        "user_role": "finance",
        "allowed_doc_types": ["10k", "invoice", "expense_policy"],
        "guardrail_status": "clean",
        "detected_pii_entities": [],
        "sanitized_query": "What was Apple's total revenue in 2023?",
        "query_intent": "",
        "retrieved_chunks": [],
        "retrieval_query": "",
        "relevant_chunks": [],
        "grading_results": [],
        "generated_answer": "",
        "hallucination_status": "",
        "hallucination_score": 0.0,
        "requires_human_approval": False,
        "human_decision": None,
        "retrieval_retry_count": 0,
        "generation_retry_count": 0,
        "final_response": "",
        "response_metadata": {},
    }


@pytest.fixture
def sample_chunks() -> list[dict]:
    """Sample retrieved chunks for testing."""
    return [
        {
            "content": "Apple Inc. reported total net revenue of $383.3 billion for fiscal year 2023.",
            "metadata": {
                "doc_type": "10k",
                "company": "Apple Inc.",
                "source_file": "apple_10k_2023.pdf",
                "page_number": 42,
                "section_header": "Item 6. Selected Financial Data",
                "confidentiality": "public",
            },
            "score": 0.92,
        },
        {
            "content": "Operating expenses increased by 2% year-over-year to $54.8 billion.",
            "metadata": {
                "doc_type": "10k",
                "company": "Apple Inc.",
                "source_file": "apple_10k_2023.pdf",
                "page_number": 43,
                "section_header": "Item 7. Management Discussion",
                "confidentiality": "public",
            },
            "score": 0.85,
        },
        {
            "content": "Services revenue grew 9% to $85.2 billion, driven by App Store and iCloud.",
            "metadata": {
                "doc_type": "10k",
                "company": "Apple Inc.",
                "source_file": "apple_10k_2023.pdf",
                "page_number": 44,
                "section_header": "Item 7. Revenue Breakdown",
                "confidentiality": "public",
            },
            "score": 0.81,
        },
    ]
