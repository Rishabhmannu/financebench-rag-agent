"""Hallucination checker — verifies the generated answer is grounded in sources.

Sprint 7b changes:
  - LLM defaults to Claude Sonnet 4.6 (via LLMFactory) with OpenAI fallback.
  - When the generated answer mentions a dollar amount exceeding the user
    role's HITL threshold, the check escalates to Claude Opus 4.7. Bounds the
    extra cost to HITL-path answers (the ones already most expensive to produce).
  - System prompt is marked for Anthropic ephemeral prompt caching so repeat
    checks save on cached system tokens. Only activates for ChatAnthropic.
"""

import logging

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from src.config.prompts import (
    HALLUCINATION_CHECK_SYSTEM_PROMPT,
    HALLUCINATION_CHECK_USER_TEMPLATE,
)
from src.config.rbac_config import get_permissions
from src.graph.nodes.hitl_gate import _extract_max_amount
from src.models.schemas import HallucinationCheck
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def _is_high_stakes(answer: str, user_role: str) -> bool:
    """True when the answer references a dollar amount ≥ the role's HITL threshold.

    Reuses the same extractor as hitl_gate so the two nodes stay in sync. When
    HITL isn't enabled for the role, this returns False → standard Sonnet path.
    """
    threshold = get_permissions(user_role).get("requires_hitl_above")
    if threshold is None:
        return False
    return _extract_max_amount(answer) > threshold


def _build_system_message(llm) -> SystemMessage:
    """SystemMessage with Anthropic cache_control when applicable.

    See generator.py for the same NOTE: the HALLUCINATION_CHECK_SYSTEM_PROMPT
    is ~217 tokens, below Anthropic's 1024-token minimum cacheable block size.
    Marker is correct and future-proof but no-op for stand-alone graph runs.
    """
    if isinstance(llm, ChatAnthropic):
        return SystemMessage(content=[
            {
                "type": "text",
                "text": HALLUCINATION_CHECK_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ])
    return SystemMessage(content=HALLUCINATION_CHECK_SYSTEM_PROMPT)


def hallucination_checker_node(state: RAGState) -> dict:
    """Check if the generated answer is grounded in the retrieved sources."""
    answer = state.get("generated_answer", "")
    chunks = state.get("relevant_chunks", [])
    user_role = state.get("user_role", "analyst")

    if not answer or not chunks:
        return {"hallucination_status": "grounded", "hallucination_score": 1.0}

    sources = "\n\n---\n\n".join(
        f"[{chunk.get('metadata', {}).get('source_file', 'Unknown')}]\n"
        f"{chunk.get('raw_content') or chunk.get('content', '')}"
        for chunk in chunks
    )

    user_prompt = HALLUCINATION_CHECK_USER_TEMPLATE.format(sources=sources, answer=answer)
    retry_count = state.get("generation_retry_count", 0)

    # Route to Opus 4.7 when the answer is HITL-eligible; otherwise Sonnet 4.6
    high_stakes = _is_high_stakes(answer, user_role)
    llm = (
        LLMFactory.get_high_stakes_hallucination_llm()
        if high_stakes
        else LLMFactory.get_hallucination_llm()
    )
    if high_stakes:
        logger.info("Hallucination check: high-stakes path (Opus 4.7)")

    try:
        structured_llm = llm.with_structured_output(HallucinationCheck)
        result: HallucinationCheck = structured_llm.invoke([
            _build_system_message(llm),
            HumanMessage(content=user_prompt),
        ])

        logger.info(f"Hallucination check: grounded={result.grounded}, score={result.score:.2f}")

        is_grounded = result.grounded
        return {
            "hallucination_status": "grounded" if is_grounded else "hallucinated",
            "hallucination_score": result.score,
            "generation_retry_count": retry_count if is_grounded else retry_count + 1,
        }
    except Exception as e:
        logger.error(f"Hallucination check failed, assuming grounded: {e}")
        return {
            "hallucination_status": "grounded",
            "hallucination_score": 0.5,
            "generation_retry_count": retry_count,
        }
