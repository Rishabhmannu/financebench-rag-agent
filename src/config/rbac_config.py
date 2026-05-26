ROLE_PERMISSIONS: dict[str, dict] = {
    "analyst": {
        "allowed_doc_types": ["10k"],
        "allowed_confidentiality": ["public"],
        "max_results": 5,
        "requires_hitl_above": None,
    },
    "finance": {
        "allowed_doc_types": ["10k", "invoice", "expense_policy"],
        "allowed_confidentiality": ["public", "internal"],
        "max_results": 10,
        "requires_hitl_above": 100_000,
    },
    "hr": {
        "allowed_doc_types": ["expense_policy"],
        "allowed_confidentiality": ["public", "internal"],
        "max_results": 5,
        "requires_hitl_above": None,
    },
    "c_level": {
        "allowed_doc_types": ["10k", "invoice", "expense_policy", "board_report"],
        "allowed_confidentiality": ["public", "internal", "confidential"],
        "max_results": 15,
        "requires_hitl_above": 1_000_000,
    },
    "admin": {
        "allowed_doc_types": ["*"],
        "allowed_confidentiality": ["*"],
        "max_results": 20,
        "requires_hitl_above": None,
    },
}


def get_permissions(role: str) -> dict:
    """Return the RBAC permissions for a role.

    Sprint 9.0: delegates to ``src.services.roles_service.get_permissions``,
    which is DB-first with a fallback to the static ``ROLE_PERMISSIONS``
    dict above. Deferred import avoids a circular-dependency cycle
    (roles_service reads ROLE_PERMISSIONS for its fallback).

    Every existing call site keeps working unchanged — graph nodes
    (rbac_gate, retrieval, hitl_gate, hallucination) automatically pick
    up edits made through ``/admin/roles`` without code changes.
    """
    from src.services.roles_service import get_permissions as _db_first
    return _db_first(role)


# Phase 3.5 — multi-party HITL approval hierarchy. Codified in source (not
# DB-driven like ROLE_PERMISSIONS) because org-level approval policy isn't a
# per-tenant knob that should be editable through /admin/roles. Adding a
# can_approve_for column to the dynamic roles table would invite footguns
# like a tenant accidentally making analysts able to approve clevel queries.
#
# Read: CAN_APPROVE_FOR[approver_role] = set of roles whose HITL-paused
# queries the approver can approve. "*" matches all roles. Empty set means
# the role has no approval authority.
#
# admin sees no HITL gates of its own (requires_hitl_above=None) but can
# approve anyone. clevel can approve finance/hr/analyst but not its own
# queries (so a clevel question above $1M still requires admin).
CAN_APPROVE_FOR: dict[str, set[str]] = {
    "analyst": set(),
    "finance": set(),
    "hr": set(),
    "c_level": {"finance", "hr", "analyst"},
    "admin": {"*"},
}


def can_approve(approver_role: str, requester_role: str) -> bool:
    """True iff `approver_role` is authorized to approve a HITL-paused query
    submitted by `requester_role`. Self-approval is never allowed."""
    if approver_role == requester_role:
        return False
    allowed = CAN_APPROVE_FOR.get(approver_role, set())
    return "*" in allowed or requester_role in allowed


def approvers_for(requester_role: str) -> list[str]:
    """List of role names authorized to approve `requester_role`'s queries."""
    return sorted(
        role for role, allowed in CAN_APPROVE_FOR.items()
        if role != requester_role and ("*" in allowed or requester_role in allowed)
    )
