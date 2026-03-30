"""Unit tests for the RBAC gate node in src/graph/nodes/rbac_gate.py."""

import pytest

from src.graph.nodes.rbac_gate import rbac_gate


# ---------------------------------------------------------------------------
# Role-specific permission tests
# ---------------------------------------------------------------------------


class TestRbacGateRoles:
    """Verify that each known role produces the correct allowed_doc_types."""

    def test_analyst_role(self):
        state = {"user_role": "analyst"}
        result = rbac_gate(state)
        assert result["allowed_doc_types"] == ["10k"]

    def test_finance_role(self):
        state = {"user_role": "finance"}
        result = rbac_gate(state)
        assert result["allowed_doc_types"] == ["10k", "invoice", "expense_policy"]

    def test_hr_role(self):
        state = {"user_role": "hr"}
        result = rbac_gate(state)
        assert result["allowed_doc_types"] == ["expense_policy"]

    def test_c_level_role(self):
        state = {"user_role": "c_level"}
        result = rbac_gate(state)
        assert result["allowed_doc_types"] == [
            "10k",
            "invoice",
            "expense_policy",
            "board_report",
        ]

    def test_admin_role(self):
        state = {"user_role": "admin"}
        result = rbac_gate(state)
        assert result["allowed_doc_types"] == ["*"]


# ---------------------------------------------------------------------------
# Fallback behavior
# ---------------------------------------------------------------------------


class TestRbacGateFallback:
    """Verify fallback to analyst permissions for unknown or missing roles."""

    def test_unknown_role_falls_back_to_analyst(self):
        state = {"user_role": "intern"}
        result = rbac_gate(state)
        assert result["allowed_doc_types"] == ["10k"]

    def test_empty_string_role_falls_back_to_analyst(self):
        state = {"user_role": ""}
        result = rbac_gate(state)
        assert result["allowed_doc_types"] == ["10k"]

    def test_missing_role_key_defaults_to_analyst(self):
        """When user_role is absent, rbac_gate defaults to 'analyst'."""
        state = {}
        result = rbac_gate(state)
        assert result["allowed_doc_types"] == ["10k"]


# ---------------------------------------------------------------------------
# Return shape
# ---------------------------------------------------------------------------


class TestRbacGateReturnShape:
    """Verify the structure of the dict returned by rbac_gate."""

    def test_returns_dict_with_allowed_doc_types_key(self):
        state = {"user_role": "finance"}
        result = rbac_gate(state)
        assert isinstance(result, dict)
        assert "allowed_doc_types" in result

    def test_return_contains_only_allowed_doc_types(self):
        """The node should return only the keys it is responsible for updating."""
        state = {"user_role": "finance"}
        result = rbac_gate(state)
        assert list(result.keys()) == ["allowed_doc_types"]

    def test_allowed_doc_types_is_a_list(self):
        state = {"user_role": "admin"}
        result = rbac_gate(state)
        assert isinstance(result["allowed_doc_types"], list)
