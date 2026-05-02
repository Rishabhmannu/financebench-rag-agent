"""Cross-encoder reranker service.

Hybrid search (dense + sparse BM25) yields a broad candidate set that casts a
wide net over potentially-relevant chunks. A cross-encoder scores each
(query, chunk) pair jointly — much more accurate than the bi-encoder used at
retrieval — and reorders the candidates so the truly-relevant chunks land at
the top.

We use BAAI/bge-reranker-v2-m3 (multilingual cross-encoder, ~568MB).
The model is cached in ~/.cache/huggingface after the first run.

The reranker is loaded lazily on first use so importing this module has zero
startup cost for code paths that never touch retrieval.

Device selection: defaults to CPU for stability (Apple Silicon's MPS pool is
shared with the OS unified memory and tends to OOM after ~50 inferences during
long FinanceBench eval runs). Override with `RERANKER_DEVICE=mps|cuda|cpu` env
var. CPU latency is ~100-200ms for 8-chunk batches, well within budget; MPS is
~3x faster but unstable for our long-running eval workload.
"""

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_DEVICE = "cpu"


@lru_cache(maxsize=1)
def get_reranker():
    """Load the cross-encoder once and cache it. First call downloads the model."""
    from sentence_transformers import CrossEncoder

    device = os.environ.get("RERANKER_DEVICE", DEFAULT_DEVICE)
    logger.info(f"Loading reranker: {RERANKER_MODEL} on device={device} (first run downloads ~568MB)")
    return CrossEncoder(RERANKER_MODEL, device=device)


def rerank(query: str, chunks: list[dict], top_k: int = 5) -> list[dict]:
    """Rerank chunks by (query, chunk) relevance, return top-k with updated scores.

    Each returned chunk gains a `rerank_score` in metadata alongside its original
    retrieval score (which is kept for diagnostics / debugging).
    """
    if not chunks:
        return []

    reranker = get_reranker()
    pairs = [(query, c.get("content", "")) for c in chunks]
    scores = reranker.predict(pairs)

    scored = [
        {**chunk, "rerank_score": float(score)}
        for chunk, score in zip(chunks, scores)
    ]
    scored.sort(key=lambda c: c["rerank_score"], reverse=True)
    return scored[:top_k]
