import logging
import re

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from src.config.rbac_config import get_permissions
from src.models.state import RAGState

logger = logging.getLogger(__name__)

# Regex to find dollar amounts in text
AMOUNT_PATTERN = re.compile(
    r"\$[\d,]+(?:\.\d{1,2})?(?:\s*(?:million|billion|trillion|thousand|M|B|T|k))?", re.IGNORECASE
)


def _extract_max_amount(text: str) -> float:
    """Extract the largest dollar amount mentioned in the text."""
    matches = AMOUNT_PATTERN.findall(text)
    if not matches:
        return 0.0

    max_amount = 0.0
    for match in matches:
        cleaned = match.replace("$", "").replace(",", "").strip()
        multiplier = 1.0
        for suffix, mult in [("trillion", 1e12), ("billion", 1e9), ("million", 1e6), ("thousand", 1e3), ("T", 1e12), ("B", 1e9), ("M", 1e6), ("k", 1e3)]:
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
    query = state.get("sanitized_query", "")
    user_role = state.get("user_role", "analyst")

    permissions = get_permissions(user_role)
    threshold = permissions.get("requires_hitl_above")

    if threshold is None:
        return {"requires_human_approval": False, "human_decision": None}

    # Bug F (audit): extract from BOTH query and answer. Answer-only missed
    # cases where the user named a high-stakes amount ("approve $200K transfer")
    # but the AI's response referenced a different number ("per policy, your
    # director can approve up to $5K") — HITL would silently not fire. Take the
    # max of both so the gate trips whenever EITHER side mentions an amount
    # above threshold.
    max_amount = max(_extract_max_amount(answer), _extract_max_amount(query))

    if max_amount > threshold:
        logger.info(f"HITL triggered: amount=${max_amount:,.0f} exceeds threshold=${threshold:,} for role={user_role}")

        # Check if HITL persistence is available (checkpointer configured)
        hitl_enabled = (config or {}).get("metadata", {}).get("hitl_enabled", False)
        if not hitl_enabled:
            logger.warning("HITL triggered but no checkpointer available — auto-approving")
            return {"requires_human_approval": True, "human_decision": "approved"}

        # Pause the graph — state is checkpointed via PostgresSaver.
        # Phase 3.7: include submitted_at so the approver can see how long the
        # request has been pending. The approver resumes with either a string
        # ("approved" / "rejected") for back-compat or a dict
        # {decision, decided_at, decided_by, decided_by_role, reason} so the
        # decision audit trail can flow into state.
        from datetime import datetime, timezone
        submitted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        decision = interrupt({
            "type": "approval_required",
            "reason": f"Answer references ${max_amount:,.0f} which exceeds the ${threshold:,} threshold for role '{user_role}'",
            "max_amount": max_amount,
            "threshold": threshold,
            "submitted_at": submitted_at,
        })

        # Execution resumes here after the human responds. NOTE: this whole
        # function runs again from the top on resume, so the local
        # `submitted_at` was just recomputed (it's now() of the resume call,
        # NOT the original pause time). The API forwards the ORIGINAL value
        # through the resume dict's "submitted_at" field to preserve the
        # audit trail. Fall back to the local value only if the approver used
        # the legacy bare-string resume.
        logger.info(f"HITL decision received: {decision}")
        if isinstance(decision, dict):
            human_decision = "approved" if decision.get("decision") == "approved" else "rejected"
            decision_at = decision.get("decided_at")
            decision_by = decision.get("decided_by")
            decision_by_role = decision.get("decided_by_role")
            decision_reason = decision.get("reason") or ""
            original_submitted_at = decision.get("submitted_at") or submitted_at
        else:
            human_decision = "approved" if decision == "approved" else "rejected"
            decision_at = None
            decision_by = None
            decision_by_role = None
            decision_reason = ""
            original_submitted_at = submitted_at

        return {
            "requires_human_approval": True,
            "human_decision": human_decision,
            "human_decision_at": decision_at,
            "human_decision_by": decision_by,
            "human_decision_by_role": decision_by_role,
            "human_decision_reason": decision_reason,
            "hitl_submitted_at": original_submitted_at,
        }

    return {"requires_human_approval": False, "human_decision": None}
