import logging
import re

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

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


def hitl_gate_node(state: RAGState, config: RunnableConfig | None = None) -> dict:
    """Check if human approval is needed based on answer content and user role.

    Uses LangGraph interrupt() to pause the graph when approval is required.
    The graph state is checkpointed to PostgresSaver so it can be resumed
    via the /hitl/approve or /hitl/reject endpoints.

    If no checkpointer is available (hitl_enabled=False in config metadata),
    the node auto-approves to avoid crashing.
    """
    answer = state.get("generated_answer", "")
    user_role = state.get("user_role", "analyst")

    permissions = get_permissions(user_role)
    threshold = permissions.get("requires_hitl_above")

    if threshold is None:
        return {"requires_human_approval": False, "human_decision": None}

    max_amount = _extract_max_amount(answer)

    if max_amount > threshold:
        logger.info(f"HITL triggered: amount=${max_amount:,.0f} exceeds threshold=${threshold:,} for role={user_role}")

        # Check if HITL persistence is available (checkpointer configured)
        hitl_enabled = (config or {}).get("metadata", {}).get("hitl_enabled", False)
        if not hitl_enabled:
            logger.warning("HITL triggered but no checkpointer available — auto-approving")
            return {"requires_human_approval": True, "human_decision": "approved"}

        # Pause the graph — state is checkpointed via PostgresSaver.
        # The caller resumes with Command(resume="approved") or Command(resume="rejected").
        decision = interrupt({
            "type": "approval_required",
            "reason": f"Answer references ${max_amount:,.0f} which exceeds the ${threshold:,} threshold for role '{user_role}'",
            "answer_preview": answer[:500],
            "max_amount": max_amount,
            "threshold": threshold,
        })

        # Execution resumes here after the human responds
        logger.info(f"HITL decision received: {decision}")
        human_decision = "approved" if decision == "approved" else "rejected"
        return {"requires_human_approval": True, "human_decision": human_decision}

    return {"requires_human_approval": False, "human_decision": None}
