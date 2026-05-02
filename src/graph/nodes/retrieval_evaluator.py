"""Selective retrieval evaluator node (optional CRAG-style branch)."""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage

from src.config.prompts import RETRIEVAL_EVALUATOR_PROMPT
from src.config.settings import settings
from src.models.schemas import RetrievalEvalDecision
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def retrieval_evaluator_node(state: RAGState) -> dict:
    """Evaluate whether reranked candidates are sufficient before grading.

    This path is optional and enabled via ENABLE_SELECTIVE_RETRIEVAL_EVALUATOR.
    """
    chunks = state.get("reranked_chunks") or state.get("retrieved_chunks", [])
    query = state.get("sanitized_query", "")
    if not settings.ENABLE_SELECTIVE_RETRIEVAL_EVALUATOR or not chunks:
        return {"retrieval_evaluator_decision": "accept", "retrieval_evaluator_confidence": 1.0}

    # Cheap confidence prior from reranker scores. If very strong, skip extra call.
    top = float(chunks[0].get("rerank_score", 0.0) or 0.0)
    second = float(chunks[1].get("rerank_score", 0.0) or 0.0) if len(chunks) > 1 else 0.0
    if top >= 0.75 and (top - second) >= 0.1:
        return {"retrieval_evaluator_decision": "accept", "retrieval_evaluator_confidence": top}

    llm = LLMFactory.get_router_llm()
    structured = llm.with_structured_output(RetrievalEvalDecision)
    chunk_preview = "\n\n".join(
        f"[{i}] {c.get('content', '')[:500]}"
        for i, c in enumerate(chunks[:5], start=1)
    )
    prompt = RETRIEVAL_EVALUATOR_PROMPT.format(query=query, chunks=chunk_preview)
    try:
        result: RetrievalEvalDecision = structured.invoke([HumanMessage(content=prompt)])
        decision = "accept"
        if result.decision == "retry" and result.confidence < settings.RETRIEVAL_EVALUATOR_MIN_CONFIDENCE:
            decision = "retry"
        logger.info(f"Retrieval evaluator decision={decision} confidence={result.confidence:.3f}")
        return {
            "retrieval_evaluator_decision": decision,
            "retrieval_evaluator_confidence": result.confidence,
        }
    except Exception as e:
        logger.warning(f"Retrieval evaluator failed; defaulting to accept: {e}")
        return {"retrieval_evaluator_decision": "accept", "retrieval_evaluator_confidence": 0.5}

