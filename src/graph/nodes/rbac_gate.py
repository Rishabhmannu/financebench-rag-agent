import logging

from src.config.rbac_config import get_permissions
from src.models.state import RAGState

logger = logging.getLogger(__name__)


def rbac_gate(state: RAGState) -> dict:
    """Validate user role and set allowed document types."""
    role = state.get("user_role", "analyst")
    permissions = get_permissions(role)

    logger.info(f"RBAC gate: role={role}, allowed_doc_types={permissions['allowed_doc_types']}")

    return {
        "allowed_doc_types": permissions["allowed_doc_types"],
    }
