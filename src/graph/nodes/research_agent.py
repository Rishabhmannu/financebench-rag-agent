"""Research agent — selective subgraph for `research_required` queries.

Sprint 7.6 Day 2. Runs ONLY for queries the router classifies as
`research_required` (calc with formula, multi-section synthesis, comparative,
applicability-judgment). Lookup queries stay on the existing fast path.

Internal loop (turn-budgeted at 5 LLM-driven turns total):

  1. **decompose**       — Claude breaks the query into 2–4 atomic sub-questions
                           and surfaces qualifiers + required quantities.
  2. **retrieve+grade**  — for each sub-question, calls the existing retrieval
                           pipeline (retrieval_node + reranker_node + grader_node)
                           via state-mutation so RBAC, entity filter, and
                           progressive relaxation all flow through unchanged.
                           This is the architectural lockdown from the
                           Sprint 7.6 Day 1 RBAC audit: agent retrieval
                           delegates to the existing path; it does NOT call
                           hybrid_search directly.
  3. **sufficiency**     — Claude judges whether collected evidence covers
                           every required quantity. If not, emits one targeted
                           follow-up sub-question (subject to turn budget).
  4. **synthesize**      — Claude writes a structured-markdown context block
                           that calls out qualifiers explicitly + lists each
                           required quantity with value + source. The main
                           generator consumes this alongside `relevant_chunks`
                           and produces the final answer; the hallucination
                           checker still grounds against `relevant_chunks`.

Output (merged into RAGState):
  - relevant_chunks: deduped union across all sub-question retrievals
  - grading_results: pre-marked "agent" so downstream router skips re-grading
  - agent_synthesis: structured markdown block (also flows into the generator)
  - agent_turns_used: int (for diagnostics)
  - agent_sub_questions: list[str]
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from src.config.prompts import (
    AGENT_SYNTHESIZER_SYSTEM_PROMPT,
    DECOMPOSE_SYSTEM_PROMPT,
    SUFFICIENCY_SYSTEM_PROMPT,
)
from src.graph.nodes.grader import grader_node
from src.graph.nodes.reranker import reranker_node
from src.graph.nodes.retrieval import retrieval_node
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


MAX_LLM_TURNS = 5  # decompose + N retrievals + sufficiency + synthesize <= 5
MAX_SUB_QUESTIONS = 4
MAX_FOLLOWUP_ROUNDS = 2  # how many sufficiency-driven follow-ups to run


# ────────────────────────────────────────────────────────────
# Pydantic schemas for structured output (agent-internal only)
# ────────────────────────────────────────────────────────────


class _Decomposition(BaseModel):
    """Output of the decompose step."""

    qualifiers: list[str] = Field(
        description="Question qualifiers that change WHAT counts as a correct answer "
                    "(e.g. 'exclude M&A', 'organic', 'year over year direction'). "
                    "Use ['no qualifiers'] if none."
    )
    required_quantities: list[str] = Field(
        description="The 2+ inputs the question's answer requires (current assets, "
                    "current liabilities, operating income FY-1, etc)."
    )
    sub_questions: list[str] = Field(
        description="2–4 atomic sub-questions, each retrievable from a single section."
    )


class _SufficiencyVerdict(BaseModel):
    """Output of the sufficiency-judge step."""

    decision: str = Field(description="'sufficient' or 'need_more'")
    missing_quantity: str | None = Field(default=None)
    follow_up_question: str | None = Field(default=None)
    reason: str = Field(description="One sentence explaining the verdict.")


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────


def _claude_with_caching() -> ChatAnthropic:
    """Generator-tier Claude (Sonnet 4.6) — same model the agent's calls expect.

    Note: cache_control on agent system prompts will start firing here once
    accumulated agent context exceeds 1024 tokens (per generator.py NOTE).
    """
    return LLMFactory.get_generator_llm()


def _system_block(prompt_text: str, llm) -> SystemMessage:
    """Anthropic cache_control marker on system text when llm is ChatAnthropic."""
    if isinstance(llm, ChatAnthropic):
        return SystemMessage(content=[{
            "type": "text",
            "text": prompt_text,
            "cache_control": {"type": "ephemeral"},
        }])
    return SystemMessage(content=prompt_text)


def _chunk_id(chunk: dict) -> tuple[str, Any]:
    """Stable identity key for dedup across sub-question retrievals."""
    meta = chunk.get("metadata", {})
    return (
        meta.get("source_file", "?"),
        meta.get("chunk_index", chunk.get("id", id(chunk))),
    )


def _evidence_summary(chunks: list[dict], max_chars_per: int = 400) -> str:
    """Compact summarization of collected chunks for the sufficiency prompt.

    The sufficiency judge doesn't need full chunks — just enough to confirm
    each required quantity is present. Truncating each chunk keeps the prompt
    bounded as the agent collects more evidence across turns.
    """
    if not chunks:
        return "(no evidence collected yet)"
    parts = []
    for i, c in enumerate(chunks, 1):
        meta = c.get("metadata", {})
        src = meta.get("source_file", "?")
        page = meta.get("page_number", "?")
        text = (c.get("raw_content") or c.get("content", ""))[:max_chars_per]
        parts.append(f"[{i}] {src} p{page}: {text}")
    return "\n".join(parts)


def _full_evidence(chunks: list[dict]) -> str:
    """Full chunk text for the synthesizer (no truncation)."""
    if not chunks:
        return "(no evidence collected)"
    parts = []
    for i, c in enumerate(chunks, 1):
        meta = c.get("metadata", {})
        src = meta.get("source_file", "?")
        page = meta.get("page_number", "?")
        text = c.get("raw_content") or c.get("content", "")
        parts.append(f"[Source {i}: {src}, Page {page}]\n{text}")
    return "\n\n---\n\n".join(parts)


def _retrieve_and_grade_for_subq(state: RAGState, sub_question: str) -> list[dict]:
    """Run the sub-question through the EXISTING retrieval/reranker/grader path.

    This is the RBAC-preservation lockdown: by mutating only `retrieval_query`
    on a copy of the parent state and routing through retrieval_node, we
    inherit RBAC filtering, target_company filter, year filter, and the
    progressive-relaxation backoff for free.
    """
    # Make a shallow sub-state with the sub-question as the retrieval query.
    sub_state = dict(state)
    sub_state["retrieval_query"] = sub_question

    # 1) retrieval
    retrieval_out = retrieval_node(sub_state)
    sub_state["retrieved_chunks"] = retrieval_out.get("retrieved_chunks", [])
    if not sub_state["retrieved_chunks"]:
        return []

    # 2) reranker
    rerank_out = reranker_node(sub_state)
    sub_state["reranked_chunks"] = rerank_out.get("reranked_chunks", [])
    sub_state["candidate_diagnostics"] = rerank_out.get("candidate_diagnostics", [])
    if not sub_state["reranked_chunks"]:
        return []

    # 3) grader (uses gpt-4o-mini, parallelized)
    grade_out = grader_node(sub_state)
    return grade_out.get("relevant_chunks", []) or []


# ────────────────────────────────────────────────────────────
# LLM-driven steps
# ────────────────────────────────────────────────────────────


def _decompose(query: str, target_company: str | None, target_fiscal_year: int | None) -> _Decomposition:
    """Turn 1 — Claude decomposes the query."""
    llm = _claude_with_caching()
    structured = llm.with_structured_output(_Decomposition)
    prompt = DECOMPOSE_SYSTEM_PROMPT.format(
        query=query,
        target_company=target_company or "(not specified)",
        target_fiscal_year=target_fiscal_year or "(not specified)",
    )
    try:
        result: _Decomposition = structured.invoke([
            _system_block(prompt, llm),
            HumanMessage(content=f"Decompose this question: {query}"),
        ])
        # Defensive truncation
        result.sub_questions = result.sub_questions[:MAX_SUB_QUESTIONS]
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Decompose failed, falling back to single-question agent: {exc}")
        return _Decomposition(
            qualifiers=["no qualifiers"],
            required_quantities=[query],
            sub_questions=[query],
        )


def _judge_sufficiency(
    query: str,
    decomposition: _Decomposition,
    chunks: list[dict],
) -> _SufficiencyVerdict:
    """Sufficiency check — does the evidence cover every required quantity?"""
    llm = _claude_with_caching()
    structured = llm.with_structured_output(_SufficiencyVerdict)
    decomp_text = (
        f"Qualifiers: {decomposition.qualifiers}\n"
        f"Required quantities: {decomposition.required_quantities}\n"
        f"Sub-questions: {decomposition.sub_questions}"
    )
    prompt = SUFFICIENCY_SYSTEM_PROMPT.format(
        query=query,
        decomposition=decomp_text,
        n_chunks=len(chunks),
        evidence_summary=_evidence_summary(chunks),
    )
    try:
        verdict: _SufficiencyVerdict = structured.invoke([
            _system_block(prompt, llm),
            HumanMessage(content="Judge sufficiency now."),
        ])
        return verdict
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Sufficiency judge failed, defaulting to sufficient: {exc}")
        return _SufficiencyVerdict(
            decision="sufficient",
            reason=f"judge_error: {type(exc).__name__}",
        )


def _synthesize(
    query: str,
    decomposition: _Decomposition,
    chunks: list[dict],
) -> str:
    """Final turn — Claude writes the structured context block for the generator."""
    llm = _claude_with_caching()
    decomp_text = (
        f"Qualifiers: {decomposition.qualifiers}\n"
        f"Required quantities: {decomposition.required_quantities}\n"
        f"Sub-questions executed: {decomposition.sub_questions}"
    )
    prompt = AGENT_SYNTHESIZER_SYSTEM_PROMPT.format(
        query=query,
        decomposition=decomp_text,
        n_chunks=len(chunks),
        evidence=_full_evidence(chunks),
    )
    try:
        result = llm.invoke([
            _system_block(prompt, llm),
            HumanMessage(content="Synthesize now."),
        ])
        return result.content if isinstance(result.content, str) else str(result.content)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Synthesizer failed, falling back to chunk concatenation: {exc}")
        return "## Research findings\n\n(synthesizer error — main generator will use raw chunks)"


# ────────────────────────────────────────────────────────────
# Top-level node
# ────────────────────────────────────────────────────────────


def research_agent_node(state: RAGState) -> dict:
    """Selective research agent for complex queries.

    Turn budget rationale:
      - Turn 1: decompose
      - Turns 2..(2+K): K sub-question retrievals (K=2..4)
      - Turn 2+K..: sufficiency + follow-ups, up to MAX_FOLLOWUP_ROUNDS
      - Final: synthesize
    Total LLM-driven turns capped at MAX_LLM_TURNS (5).
    """
    query = state.get("sanitized_query", "")
    if not query:
        return {"relevant_chunks": [], "agent_synthesis": None}

    target_company = state.get("target_company")
    target_fiscal_year = state.get("target_fiscal_year")

    # Turn 1: decompose
    decomp = _decompose(query, target_company, target_fiscal_year)
    sub_questions = decomp.sub_questions
    logger.info(
        f"Agent decomposed into {len(sub_questions)} sub-questions; "
        f"qualifiers={decomp.qualifiers}; required={decomp.required_quantities}"
    )

    # Turns 2..(2+K): retrieve + grade per sub-question
    chunks_by_id: dict[tuple, dict] = {}
    for subq in sub_questions:
        for c in _retrieve_and_grade_for_subq(state, subq):
            cid = _chunk_id(c)
            if cid not in chunks_by_id:
                chunks_by_id[cid] = c

    logger.info(f"Agent collected {len(chunks_by_id)} unique chunks across {len(sub_questions)} sub-questions")

    # Sufficiency loop — at most MAX_FOLLOWUP_ROUNDS additional retrievals
    sufficiency_history: list[dict] = []
    for round_idx in range(MAX_FOLLOWUP_ROUNDS):
        chunks = list(chunks_by_id.values())
        verdict = _judge_sufficiency(query, decomp, chunks)
        sufficiency_history.append({
            "decision": verdict.decision,
            "missing": verdict.missing_quantity,
            "follow_up": verdict.follow_up_question,
            "reason": verdict.reason,
        })
        if verdict.decision == "sufficient":
            logger.info(f"Agent: sufficient after round {round_idx} ({verdict.reason})")
            break
        if not verdict.follow_up_question:
            logger.info(f"Agent: 'need_more' but no follow-up — exiting loop")
            break
        logger.info(f"Agent follow-up [{round_idx}]: {verdict.follow_up_question} ({verdict.reason})")
        for c in _retrieve_and_grade_for_subq(state, verdict.follow_up_question):
            cid = _chunk_id(c)
            if cid not in chunks_by_id:
                chunks_by_id[cid] = c

    # Final turn: synthesize
    relevant_chunks = list(chunks_by_id.values())
    synthesis = _synthesize(query, decomp, relevant_chunks)

    # Compute total LLM turns: 1 decompose + len(sufficiency_history) + 1 synthesize
    # (sub-question retrievals don't count Claude calls; only grader calls, which
    # use gpt-4o-mini and are accounted for separately.)
    turns_used = 1 + len(sufficiency_history) + 1

    logger.info(
        f"Agent done: {len(relevant_chunks)} chunks, {turns_used} Claude turns, "
        f"sub_questions={sub_questions}"
    )

    # Mark grading_results so the main grader path doesn't re-grade.
    grading_results = [{"agent_curated": True} for _ in relevant_chunks]

    return {
        "relevant_chunks": relevant_chunks,
        "grading_results": grading_results,
        "agent_synthesis": synthesis,
        "agent_turns_used": turns_used,
        "agent_sub_questions": sub_questions,
    }
