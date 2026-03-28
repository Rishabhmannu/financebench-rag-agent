import logging
import re

from src.config.rbac_config import get_permissions
from src.models.state import RAGState

logger = logging.getLogger(__name__)

# Regex to find dollar amounts in text
AMOUNT_PATTERN = re.compile(r"\$[\d,]+(?:\.\d{2})?(?:\s*(?:million|billion|trillion|M|B|T))?", re.IGNORECASE)


def _extract_max_amount(text: str) -> float:
    """Extract the largest dollar amount mentioned in the text."""
    matches = AMOUNT_PATTERN.findall(text)
    if not matches:
        return 0.0

    max_amount = 0.0
    for match in matches:
        cleaned = match.replace("$", "").replace(",", "").strip()
        multiplier = 1.0
        for suffix, mult in [("trillion", 1e12), ("billion", 1e9), ("million", 1e6), ("T", 1e12), ("B", 1e9), ("M", 1e6)]:
            if suffix in cleaned:
                cleaned = cleaned.replace(suffix, "").strip()
                multiplier = mult
                break
        try:
            amount = float(cleaned) * multiplier
            max_amount = max(max_amount, amount)
        except ValueError:
            continue
    return max_amount


def hitl_gate_node(state: RAGState) -> dict:
    """Check if human approval is needed based on answer content and user role."""
    answer = state.get("generated_answer", "")
    user_role = state.get("user_role", "analyst")

    permissions = get_permissions(user_role)
    threshold = permissions.get("requires_hitl_above")

    if threshold is None:
        return {"requires_human_approval": False, "human_decision": None}

    max_amount = _extract_max_amount(answer)

    if max_amount > threshold:
        logger.info(f"HITL triggered: amount=${max_amount:,.0f} exceeds threshold=${threshold:,} for role={user_role}")
        # TODO (Sprint 3): Use langgraph interrupt() here for actual HITL pause
        # For now, auto-approve
        return {"requires_human_approval": True, "human_decision": "approved"}

    return {"requires_human_approval": False, "human_decision": None}
