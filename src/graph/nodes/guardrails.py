import logging

from src.models.state import RAGState
from src.services.guardrails_service import (
    check_injection_llm,
    check_injection_llm_guard,
    check_injection_regex,
    detect_pii,
)

logger = logging.getLogger(__name__)


def guardrails_node(state: RAGState) -> dict:
    """Run PII detection and 3-layer prompt injection defense.

    Layers run in order of increasing cost/latency:
      Layer 1: Regex heuristics (~0ms) — catches obvious patterns
      Layer 2: LLM Guard model (~100ms) — catches sophisticated attacks
      Layer 3: LLM classifier (~1-2s) — borderline cases only
    """
    messages = state.get("messages", [])
    if not messages:
        return {"guardrail_status": "clean", "sanitized_query": "", "detected_pii_entities": []}

    query = messages[-1].content

    # --- PII Detection (always runs first) ---
    sanitized_query, pii_entities = detect_pii(query)
    if pii_entities:
        logger.warning(f"PII detected and redacted: {[e['type'] for e in pii_entities]}")

    blocked = {"guardrail_status": "injection_detected", "sanitized_query": sanitized_query, "detected_pii_entities": pii_entities}

    # --- Layer 1: Regex (cheapest) ---
    if check_injection_regex(query):
        logger.warning(f"Injection blocked by Layer 1 (regex): {query[:100]}")
        return blocked

    # --- Layer 2: LLM Guard (local model) ---
    is_injection, risk_score = check_injection_llm_guard(query)
    if is_injection:
        logger.warning(f"Injection blocked by Layer 2 (LLM Guard, score={risk_score:.2f}): {query[:100]}")
        return blocked

    # --- Layer 3: LLM classifier (only for borderline cases) ---
    # Trigger Layer 3 if LLM Guard returned a moderate risk score (0.5-0.9)
    if risk_score >= 0.5:
        is_injection, confidence = check_injection_llm(query)
        if is_injection and confidence >= 0.7:
            logger.warning(f"Injection blocked by Layer 3 (LLM, conf={confidence:.2f}): {query[:100]}")
            return blocked

    return {
        "guardrail_status": "clean",
        "sanitized_query": sanitized_query,
        "detected_pii_entities": pii_entities,
    }
