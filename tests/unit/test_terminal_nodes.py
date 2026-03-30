"""Unit tests for terminal graph nodes (blocked, out-of-scope, clarification, no-info)."""

import pytest

from src.config.prompts import (
    BLOCKED_RESPONSE,
    CLARIFICATION_RESPONSE,
    NO_INFO_RESPONSE,
    OUT_OF_SCOPE_RESPONSE,
)
from src.graph.nodes.terminal_nodes import (
    blocked_response_node,
    clarification_node,
    no_info_node,
    out_of_scope_node,
)


# --- blocked_response_node ---


class TestBlockedResponseNode:
    def test_returns_dict_with_final_response(self, base_state):
        result = blocked_response_node(base_state)
        assert "final_response" in result

    def test_response_matches_prompt_constant(self, base_state):
        result = blocked_response_node(base_state)
        assert result["final_response"] == BLOCKED_RESPONSE

    def test_response_is_nonempty_string(self, base_state):
        result = blocked_response_node(base_state)
        assert isinstance(result["final_response"], str)
        assert len(result["final_response"]) > 0

    def test_includes_response_metadata(self, base_state):
        result = blocked_response_node(base_state)
        assert "response_metadata" in result
        assert result["response_metadata"]["reason"] == "clean"  # base_state guardrail_status

    def test_metadata_reflects_guardrail_status(self):
        state = {"guardrail_status": "injection_detected"}
        result = blocked_response_node(state)
        assert result["response_metadata"]["reason"] == "injection_detected"

    def test_works_with_empty_state(self):
        result = blocked_response_node({})
        assert result["final_response"] == BLOCKED_RESPONSE


# --- out_of_scope_node ---


class TestOutOfScopeNode:
    def test_returns_dict_with_final_response(self, base_state):
        result = out_of_scope_node(base_state)
        assert "final_response" in result

    def test_response_matches_prompt_constant(self, base_state):
        result = out_of_scope_node(base_state)
        assert result["final_response"] == OUT_OF_SCOPE_RESPONSE

    def test_response_is_nonempty_string(self, base_state):
        result = out_of_scope_node(base_state)
        assert isinstance(result["final_response"], str)
        assert len(result["final_response"]) > 0

    def test_metadata_reason_is_out_of_scope(self, base_state):
        result = out_of_scope_node(base_state)
        assert result["response_metadata"]["reason"] == "out_of_scope"

    def test_works_with_empty_state(self):
        result = out_of_scope_node({})
        assert result["final_response"] == OUT_OF_SCOPE_RESPONSE


# --- clarification_node ---


class TestClarificationNode:
    def test_returns_dict_with_final_response(self, base_state):
        result = clarification_node(base_state)
        assert "final_response" in result

    def test_response_matches_prompt_constant(self, base_state):
        result = clarification_node(base_state)
        assert result["final_response"] == CLARIFICATION_RESPONSE

    def test_response_is_nonempty_string(self, base_state):
        result = clarification_node(base_state)
        assert isinstance(result["final_response"], str)
        assert len(result["final_response"]) > 0

    def test_metadata_reason_is_clarification(self, base_state):
        result = clarification_node(base_state)
        assert result["response_metadata"]["reason"] == "clarification"

    def test_works_with_empty_state(self):
        result = clarification_node({})
        assert result["final_response"] == CLARIFICATION_RESPONSE


# --- no_info_node ---


class TestNoInfoNode:
    def test_returns_dict_with_final_response(self, base_state):
        result = no_info_node(base_state)
        assert "final_response" in result

    def test_response_matches_prompt_constant(self, base_state):
        result = no_info_node(base_state)
        assert result["final_response"] == NO_INFO_RESPONSE

    def test_response_is_nonempty_string(self, base_state):
        result = no_info_node(base_state)
        assert isinstance(result["final_response"], str)
        assert len(result["final_response"]) > 0

    def test_metadata_reason_is_no_relevant_info(self, base_state):
        result = no_info_node(base_state)
        assert result["response_metadata"]["reason"] == "no_relevant_info"

    def test_works_with_empty_state(self):
        result = no_info_node({})
        assert result["final_response"] == NO_INFO_RESPONSE
