"""Generator node — produces the final answer from relevant chunks.

Sprint 7b changes:
  - LLM defaults to Claude Sonnet 4.6 (via LLMFactory) with OpenAI fallback.
  - System prompt is marked for Anthropic **ephemeral prompt caching** so
    repeat queries save ~90% on the cached system tokens (~5-minute TTL).
    Only activates when the LLM is ChatAnthropic; OpenAI call path is
    unaffected.
  - Logs cache hit/miss stats when available so we can measure savings in
    production traces.
"""

import logging

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.config.prompts import (
    GENERATOR_SYSTEM_PROMPT,
    GENERATOR_USER_TEMPLATE,
)
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def _format_context(chunks: list[dict]) -> str:
    """Format relevant chunks into a context string with source attribution."""
    parts = []
    for i, chunk in enumerate(chunks, 1):
        meta = chunk.get("metadata", {})
        source = meta.get("source_file", "Unknown")
        page = meta.get("page_number", "?")
        section = meta.get("section_header", "")
        header = f"[Source {i}: {source}, Page {page}]"
        if section:
            header += f" Section: {section}"
        # Use raw_content if available (contextual prefix stripped), else content
        chunk_text = chunk.get("raw_content") or chunk.get("content", "")
        parts.append(f"{header}\n{chunk_text}")
    return "\n\n---\n\n".join(parts)


def _build_system_message(llm) -> SystemMessage:
    """Return a SystemMessage with Anthropic cache_control set when supported.

    For ChatAnthropic, we emit a structured block with `cache_control=ephemeral`
    so the stable system prompt is cached across requests (5-min TTL).
    For OpenAI and others, plain string content (no caching).
    """
    if isinstance(llm, ChatAnthropic):
        return SystemMessage(content=[
            {
                "type": "text",
                "text": GENERATOR_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ])
    return SystemMessage(content=GENERATOR_SYSTEM_PROMPT)


def _log_cache_stats(response) -> None:
    """Emit a compact cache-hit/miss log line when Anthropic reports usage."""
    meta = getattr(response, "response_metadata", {}) or {}
    usage = meta.get("usage", {})
    if not usage:
        return
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    input_tokens = usage.get("input_tokens", 0) or 0
    if cache_read or cache_create:
        total_input = cache_read + cache_create + input_tokens
        hit_pct = (cache_read / total_input * 100) if total_input else 0
        logger.info(
            f"Generator cache: read={cache_read}, created={cache_create}, "
            f"uncached={input_tokens} (hit {hit_pct:.0f}%)"
        )


def generator_node(state: RAGState) -> dict:
    """Generate an answer from relevant chunks. Claude Sonnet 4.6 with prompt caching."""
    query = state.get("sanitized_query", "")
    chunks = state.get("relevant_chunks", [])

    context = _format_context(chunks)
    user_prompt = GENERATOR_USER_TEMPLATE.format(context=context, query=query)

    try:
        llm = LLMFactory.get_generator_llm()
        result = llm.invoke([
            _build_system_message(llm),
            HumanMessage(content=user_prompt),
        ])
        _log_cache_stats(result)
        logger.info(f"Generated answer: {len(result.content)} chars")
        return {
            "generated_answer": result.content,
            "messages": [AIMessage(content=result.content)],
        }
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        return {"generated_answer": "I encountered an error generating a response. Please try again."}
