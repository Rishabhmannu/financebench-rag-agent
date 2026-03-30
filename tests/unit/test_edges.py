"""Unit tests for conditional edge routing functions in src/graph/edges.py."""

import pytest

from src.graph.edges import (
    route_after_grading,
    route_after_guardrails,
    route_after_hallucination,
    route_after_hitl,
    route_after_router,
)


# ---------------------------------------------------------------------------
# route_after_guardrails
# ---------------------------------------------------------------------------


class TestRouteAfterGuardrails:
    """Tests for the guardrails routing function."""

    def test_clean_status_returns_clean(self):
        state = {"guardrail_status": "clean"}
        assert route_after_guardrails(state) == "clean"

    def test_pii_detected_returns_blocked(self):
        state = {"guardrail_status": "pii_detected"}
        assert route_after_guardrails(state) == "blocked"

    def test_injection_detected_returns_blocked(self):
        state = {"guardrail_status": "injection_detected"}
        assert route_after_guardrails(state) == "blocked"

    def test_out_of_scope_returns_blocked(self):
        state = {"guardrail_status": "out_of_scope"}
        assert route_after_guardrails(state) == "blocked"

    def test_missing_key_defaults_to_clean(self):
        """When guardrail_status is absent, the function defaults to 'clean'."""
        state = {}
        assert route_after_guardrails(state) == "clean"

    def test_arbitrary_non_clean_value_returns_blocked(self):
        state = {"guardrail_status": "something_unexpected"}
        assert route_after_guardrails(state) == "blocked"


# ---------------------------------------------------------------------------
# route_after_router
# ---------------------------------------------------------------------------


class TestRouteAfterRouter:
    """Tests for the router routing function."""

    def test_retrieval_intent(self):
        state = {"query_intent": "retrieval"}
        assert route_after_router(state) == "retrieval"

    def test_out_of_scope_intent(self):
        state = {"query_intent": "out_of_scope"}
        assert route_after_router(state) == "out_of_scope"

    def test_clarification_intent(self):
        state = {"query_intent": "clarification"}
        assert route_after_router(state) == "clarification"

    def test_missing_key_defaults_to_retrieval(self):
        """When query_intent is absent, the function defaults to 'retrieval'."""
        state = {}
        assert route_after_router(state) == "retrieval"

    def test_unknown_intent_falls_through_to_clarification(self):
        """Any unrecognized intent value falls through to 'clarification'."""
        state = {"query_intent": "unknown_intent"}
        assert route_after_router(state) == "clarification"


# ---------------------------------------------------------------------------
# route_after_grading
# ---------------------------------------------------------------------------


class TestRouteAfterGrading:
    """Tests for the grading routing function.

    Uses settings defaults:
        GRADING_MIN_RELEVANT_CHUNKS = 1
        MAX_RETRIEVAL_RETRIES = 2
    """

    def test_sufficient_with_one_relevant_chunk(self):
        """One relevant chunk meets the minimum threshold of 1."""
        state = {"relevant_chunks": [{"id": "chunk1"}], "retrieval_retry_count": 0}
        assert route_after_grading(state) == "sufficient"

    def test_sufficient_with_multiple_relevant_chunks(self):
        state = {
            "relevant_chunks": [{"id": "chunk1"}, {"id": "chunk2"}, {"id": "chunk3"}],
            "retrieval_retry_count": 0,
        }
        assert route_after_grading(state) == "sufficient"

    def test_retry_when_no_relevant_chunks_and_retries_available(self):
        """No relevant chunks and retry_count < MAX_RETRIEVAL_RETRIES (2) triggers retry."""
        state = {"relevant_chunks": [], "retrieval_retry_count": 0}
        assert route_after_grading(state) == "retry"

    def test_retry_on_second_attempt(self):
        """retry_count=1 is still below MAX_RETRIEVAL_RETRIES=2, so retry."""
        state = {"relevant_chunks": [], "retrieval_retry_count": 1}
        assert route_after_grading(state) == "retry"

    def test_no_info_after_retries_exhausted(self):
        """retry_count=2 equals MAX_RETRIEVAL_RETRIES, so no more retries."""
        state = {"relevant_chunks": [], "retrieval_retry_count": 2}
        assert route_after_grading(state) == "no_info"

    def test_no_info_when_retries_exceed_max(self):
        state = {"relevant_chunks": [], "retrieval_retry_count": 5}
        assert route_after_grading(state) == "no_info"

    def test_sufficient_overrides_retry_count(self):
        """Even with high retry count, having enough chunks returns 'sufficient'."""
        state = {"relevant_chunks": [{"id": "chunk1"}], "retrieval_retry_count": 10}
        assert route_after_grading(state) == "sufficient"

    def test_missing_keys_defaults(self):
        """Missing relevant_chunks defaults to [] (length 0), retry_count defaults to 0 -> retry."""
        state = {}
        assert route_after_grading(state) == "retry"


# ---------------------------------------------------------------------------
# route_after_hallucination
# ---------------------------------------------------------------------------


class TestRouteAfterHallucination:
    """Tests for the hallucination routing function.

    Uses settings defaults:
        HALLUCINATION_THRESHOLD = 0.7
        MAX_GENERATION_RETRIES = 2
    """

    def test_grounded_with_high_score(self):
        """Status 'grounded' and score >= 0.7 returns 'grounded'."""
        state = {
            "hallucination_status": "grounded",
            "hallucination_score": 0.95,
            "generation_retry_count": 0,
        }
        assert route_after_hallucination(state) == "grounded"

    def test_grounded_at_exact_threshold(self):
        """Score exactly at 0.7 with 'grounded' status still returns 'grounded'."""
        state = {
            "hallucination_status": "grounded",
            "hallucination_score": 0.7,
            "generation_retry_count": 0,
        }
        assert route_after_hallucination(state) == "grounded"

    def test_retry_when_hallucinated_with_retries_available(self):
        """Status 'hallucinated' with retry_count < 2 triggers retry."""
        state = {
            "hallucination_status": "hallucinated",
            "hallucination_score": 0.3,
            "generation_retry_count": 0,
        }
        assert route_after_hallucination(state) == "retry"

    def test_retry_when_score_below_threshold_but_status_grounded(self):
        """Even if status is 'grounded', a score below 0.7 fails the compound check."""
        state = {
            "hallucination_status": "grounded",
            "hallucination_score": 0.5,
            "generation_retry_count": 0,
        }
        assert route_after_hallucination(state) == "retry"

    def test_retry_on_second_attempt(self):
        """retry_count=1 is still below MAX_GENERATION_RETRIES=2."""
        state = {
            "hallucination_status": "hallucinated",
            "hallucination_score": 0.2,
            "generation_retry_count": 1,
        }
        assert route_after_hallucination(state) == "retry"

    def test_disclaimer_after_retries_exhausted(self):
        """retry_count=2 equals MAX_GENERATION_RETRIES, so return 'disclaimer'."""
        state = {
            "hallucination_status": "hallucinated",
            "hallucination_score": 0.3,
            "generation_retry_count": 2,
        }
        assert route_after_hallucination(state) == "disclaimer"

    def test_disclaimer_when_retries_exceed_max(self):
        state = {
            "hallucination_status": "hallucinated",
            "hallucination_score": 0.1,
            "generation_retry_count": 5,
        }
        assert route_after_hallucination(state) == "disclaimer"

    def test_grounded_overrides_high_retry_count(self):
        """Even with many retries, grounded+high score still returns 'grounded'."""
        state = {
            "hallucination_status": "grounded",
            "hallucination_score": 0.9,
            "generation_retry_count": 10,
        }
        assert route_after_hallucination(state) == "grounded"

    def test_missing_keys_defaults_to_grounded(self):
        """Defaults: status='grounded', score=1.0, retry_count=0 -> grounded."""
        state = {}
        assert route_after_hallucination(state) == "grounded"

    def test_hallucinated_status_with_high_score_triggers_retry(self):
        """The 'and' condition requires BOTH status=='grounded' AND score>=threshold."""
        state = {
            "hallucination_status": "hallucinated",
            "hallucination_score": 0.95,
            "generation_retry_count": 0,
        }
        assert route_after_hallucination(state) == "retry"


# ---------------------------------------------------------------------------
# route_after_hitl
# ---------------------------------------------------------------------------


class TestRouteAfterHitl:
    """Tests for the HITL routing function."""

    def test_no_approval_needed_when_flag_false(self):
        state = {"requires_human_approval": False, "human_decision": None}
        assert route_after_hitl(state) == "no_approval_needed"

    def test_approved_when_decision_is_approved(self):
        state = {"requires_human_approval": True, "human_decision": "approved"}
        assert route_after_hitl(state) == "approved"

    def test_rejected_when_decision_is_rejected(self):
        state = {"requires_human_approval": True, "human_decision": "rejected"}
        assert route_after_hitl(state) == "rejected"

    def test_no_approval_needed_when_requires_true_but_no_decision(self):
        """Approval required but no decision yet falls through to 'no_approval_needed'."""
        state = {"requires_human_approval": True, "human_decision": None}
        assert route_after_hitl(state) == "no_approval_needed"

    def test_no_approval_needed_with_unknown_decision(self):
        """An unrecognized decision value falls through to 'no_approval_needed'."""
        state = {"requires_human_approval": True, "human_decision": "pending"}
        assert route_after_hitl(state) == "no_approval_needed"

    def test_missing_keys_defaults_to_no_approval_needed(self):
        """Missing requires_human_approval defaults to False."""
        state = {}
        assert route_after_hitl(state) == "no_approval_needed"

    def test_approval_false_ignores_approved_decision(self):
        """When requires_human_approval is False, the decision field is irrelevant."""
        state = {"requires_human_approval": False, "human_decision": "approved"}
        assert route_after_hitl(state) == "no_approval_needed"

    def test_approval_false_ignores_rejected_decision(self):
        state = {"requires_human_approval": False, "human_decision": "rejected"}
        assert route_after_hitl(state) == "no_approval_needed"
