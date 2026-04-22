import logging

from langchain_core.messages import AIMessage, HumanMessage

from src.config.prompts import QUERY_CONTEXTUALIZER_PROMPT
from src.models.state import RAGState
from src.services.guardrails_service import (
    check_injection_llm,
    check_injection_llm_guard,
    check_injection_regex,
    detect_pii,
)
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def _format_history(messages: list, max_turns: int = 3) -> str:
    """Format the last N turns of conversation (excluding the current message) for the contextualizer."""
    prior = messages[:-1]
    if not prior:
        return ""
    # Keep only the last max_turns*2 messages (human+ai pairs)
    recent = prior[-(max_turns * 2):]
    lines = []
    for msg in recent:
        if isinstance(msg, HumanMessage):
            lines.append(f"User: {msg.content}")
        elif isinstance(msg, AIMessage):
            # Truncate long AI responses to keep the context window small
            content = msg.content[:400] + ("..." if len(msg.content) > 400 else "")
            lines.append(f"Assistant: {content}")
    return "\n".join(lines)


def _contextualize_query(query: str, messages: list) -> str:
    """Rewrite a follow-up query as a standalone question using prior conversation.

    Returns the original query if there's no prior conversation or the rewrite fails.
    """
    if len(messages) <= 1:
        return query

    history = _format_history(messages)
    if not history:
        return query

    try:
        llm = LLMFactory.get_router_llm()
        prompt = QUERY_CONTEXTUALIZER_PROMPT.format(history=history, query=query)
        result = llm.invoke([HumanMessage(content=prompt)])
        rewritten = result.content.strip().strip('"').strip("'")
        if rewritten and rewritten.lower() != query.lower():
            logger.info(f"Contextualized query: '{query}' -> '{rewritten}'")
            return rewritten
        return query
    except Exception as e:
        logger.warning(f"Query contextualization failed, using original: {e}")
        return query


def guardrails_node(state: RAGState) -> dict:
    """Run PII detection, 3-layer prompt injection defense, and query contextualization.

    Layers run in order of increasing cost/latency:
      Layer 1: Regex heuristics (~0ms) — catches obvious patterns
      Layer 2: LLM Guard model (~100ms) — catches sophisticated attacks
      Layer 3: LLM classifier (~1-2s) — borderline cases only

    After safety checks pass, coreferential follow-ups (e.g., "what about Microsoft?")
    are rewritten using conversation history so downstream retrieval/generation
    can work with a standalone query.
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

    # --- Query contextualization (resolve coreferences in multi-turn conversations) ---
    contextualized = _contextualize_query(sanitized_query, messages)

    return {
        "guardrail_status": "clean",
        "sanitized_query": contextualized,
        "detected_pii_entities": pii_entities,
    }
