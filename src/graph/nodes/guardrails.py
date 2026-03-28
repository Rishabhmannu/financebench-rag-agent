import logging

from src.models.state import RAGState
from src.services.guardrails_service import check_injection_regex, detect_pii

logger = logging.getLogger(__name__)


def guardrails_node(state: RAGState) -> dict:
    """Run PII detection, prompt injection check, and scope check on the user query."""
    messages = state.get("messages", [])
    if not messages:
        return {"guardrail_status": "clean", "sanitized_query": "", "detected_pii_entities": []}

    query = messages[-1].content

    # --- PII Detection ---
    sanitized_query, pii_entities = detect_pii(query)
    if pii_entities:
        logger.warning(f"PII detected and redacted: {[e['type'] for e in pii_entities]}")

    # --- Prompt Injection (Layer 1: Regex) ---
    if check_injection_regex(query):
        logger.warning(f"Prompt injection detected via regex: {query[:100]}")
        return {
            "guardrail_status": "injection_detected",
            "sanitized_query": sanitized_query,
            "detected_pii_entities": pii_entities,
        }

    # TODO (Sprint 3): Add Layer 2 (LLM Guard) and Layer 3 (LLM classifier)
    # TODO (Sprint 3): Add scope check via LLM

    return {
        "guardrail_status": "clean",
        "sanitized_query": sanitized_query,
        "detected_pii_entities": pii_entities,
    }
