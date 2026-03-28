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
    return ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["analyst"])
