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


def _format_prior_qa(messages: list, max_turns: int = 2) -> str:
    """Phase 3.8 conversation memory: format the most recent (human, ai) pairs
    as plain text so the generator can resolve references like "compare to that"
    or "in 2022 instead". Excludes the current turn (the latest HumanMessage
    is the one being answered now).
    """
    if not messages:
        return ""
    pairs: list[tuple[str, str]] = []
    last_human: str | None = None
    for msg in messages[:-1]:  # skip the current-turn HumanMessage
        cls_name = type(msg).__name__
        content = (getattr(msg, "content", "") or "").strip()
        if cls_name == "HumanMessage":
            last_human = content
        elif cls_name == "AIMessage" and last_human is not None:
            pairs.append((last_human, content[:600]))
            last_human = None
    if not pairs:
        return ""
    recent = pairs[-max_turns:]
    lines = []
    for q, a in recent:
        lines.append(f"User asked earlier: {q}")
        lines.append(f"You answered: {a}")
        lines.append("")
    return "\n".join(lines).strip()


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

    NOTE on effective caching: Anthropic requires a minimum cacheable block size
    of ~1024 tokens for Sonnet/Opus (~2048 for Haiku). The current
    GENERATOR_SYSTEM_PROMPT is ~215 tokens, so this marker is a no-op for the
    stand-alone graph today — Anthropic silently skips creating a cache entry
    below threshold, hence cost_log shows cache_read_input_tokens=0.
    The marker stays correct and future-proof: it will start firing as soon as
    a caller (e.g. the Sprint 7.6 research agent) issues multiple LLM calls
    with the same system prompt within a single FinanceBench question, where
    the agent system prompt + accumulated reasoning easily clear 1024 tokens.
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


async def generator_node(state: RAGState) -> dict:
    """Generate an answer from relevant chunks. Claude Sonnet 4.6 with prompt caching.

    Sprint 7.6: when the research agent ran (agent_synthesis is set), prepend
    its structured findings block above the raw chunks. The generator now
    sees both the agent's curated synthesis AND the raw chunks the
    hallucination checker will ground against — best of both worlds.

    Async + astream so /v1/chat/stream surfaces per-token events via
    LangGraph's astream_events (the CLI's REPL renders these as live tokens).
    Total wall-time is unchanged vs the prior `invoke` path; the result is
    accumulated by adding chunks together (langchain BaseMessageChunk supports
    `__add__`).
    """
    query = state.get("sanitized_query", "")
    chunks = state.get("relevant_chunks", [])
    agent_synthesis = state.get("agent_synthesis")

    # Empty-chunks path is owned by no_info_node (terminal_nodes.py) which
    # detects RBAC-driven empties and returns an informative refusal. Generator
    # is only reached when relevant_chunks >= GRADING_MIN_RELEVANT_CHUNKS.

    raw_context = _format_context(chunks)
    if agent_synthesis:
        context = (
            f"## Research-agent synthesis (structured findings):\n\n"
            f"{agent_synthesis}\n\n"
            f"---\n\n"
            f"## Raw retrieved chunks (for verification):\n\n"
            f"{raw_context}"
        )
    else:
        context = raw_context

    # Phase 3.8 conversation memory: if this thread has prior turns, prepend the
    # last (human, ai) pairs so the generator can resolve references in the
    # current query ("compare to that", "in 2022 instead"). Skipped on first
    # turn since there's nothing to add.
    prior_qa = _format_prior_qa(state.get("messages") or [], max_turns=2)
    if prior_qa:
        context = (
            f"## Recent conversation (for context):\n\n{prior_qa}\n\n"
            f"---\n\n{context}"
        )
    user_prompt = GENERATOR_USER_TEMPLATE.format(context=context, query=query)

    try:
        llm = LLMFactory.get_generator_llm()
        messages = [
            _build_system_message(llm),
            HumanMessage(content=user_prompt),
        ]
        result = None
        async for chunk in llm.astream(messages):
            result = chunk if result is None else result + chunk
        if result is None:
            logger.warning("Generator astream produced no chunks")
            return {"generated_answer": "I couldn't generate a response. Please try again."}
        _log_cache_stats(result)
        logger.info(f"Generated answer: {len(result.content)} chars")
        from src.services.event_log import emit
        from src.config.settings import settings as _settings
        emit("generator", answer_chars=len(result.content),
             n_context_chunks=len(chunks) if chunks else 0,
             model=_settings.GENERATOR_MODEL)
        return {
            "generated_answer": result.content,
            "messages": [AIMessage(content=result.content)],
        }
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        return {"generated_answer": "I encountered an error generating a response. Please try again."}
