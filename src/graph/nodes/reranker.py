"""Reranker node — narrows hybrid-search candidates to the top-K most relevant.

Reads `retrieved_chunks` (~50 candidates from hybrid dense+BM25 search),
scores each with a cross-encoder against the sanitized query, and writes the
top-K to `reranked_chunks`. Downstream grader + generator read from
`reranked_chunks` instead of the raw retrieval pool.
"""

import logging

from src.config.settings import settings
from src.models.state import RAGState
from src.services.reranker_service import rerank

logger = logging.getLogger(__name__)


def reranker_node(state: RAGState) -> dict:
    """Rerank retrieved candidates with a cross-encoder and select top-K."""
    query = state.get("retrieval_query") or state.get("sanitized_query", "")
    candidates = state.get("retrieved_chunks", [])

    if not candidates:
        return {"reranked_chunks": []}

    top_k = settings.RERANKER_TOP_K
    reranked = rerank(query, candidates, top_k=top_k)
    logger.info(
        f"Reranked {len(candidates)} -> {len(reranked)} chunks "
        f"(top score: {reranked[0]['rerank_score']:.3f} | bottom: {reranked[-1]['rerank_score']:.3f})"
    )
    return {"reranked_chunks": reranked}
