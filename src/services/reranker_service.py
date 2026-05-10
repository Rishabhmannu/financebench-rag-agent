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

Sprint 7.9 Day 6: optional LoRA adapter support. If `RERANKER_ADAPTER_PATH` env
var is set (typically `data/models/reranker_ft_v1`), the base model is wrapped
with the fine-tuned LoRA adapter via `PeftModel.from_pretrained()`. The
adapter has the same input/output contract as the base CrossEncoder
(`predict(pairs) → scores`), so the rest of the pipeline doesn't change.
"""

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_DEVICE = "cpu"


class _FtReranker:
    """Adapter-aware reranker wrapper. Mimics `sentence_transformers.CrossEncoder.predict()`
    so call sites in `rerank()` don't need to branch on whether a fine-tune is loaded.

    The base BGE-reranker is `AutoModelForSequenceClassification` with a single
    sigmoid head (relevance probability in [0,1]). We tokenize (query, chunk)
    pairs the same way `CrossEncoder` does, run a forward pass, and return the
    sigmoid'd logits — matches the shape the existing `rerank()` function
    expects (`scores` parallel to `pairs`).
    """

    def __init__(self, base_model: str, adapter_path: str, device: str):
        import torch
        from peft import PeftModel
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._torch = torch
        self.device = torch.device(device)
        logger.info(
            f"Loading FT reranker: base={base_model}, adapter={adapter_path}, device={device}"
        )
        self.tokenizer = AutoTokenizer.from_pretrained(base_model)
        base = AutoModelForSequenceClassification.from_pretrained(base_model, num_labels=1)
        self.model = PeftModel.from_pretrained(base, adapter_path).to(self.device)
        self.model.eval()

    def predict(self, pairs: list[tuple[str, str]], batch_size: int = 8) -> list[float]:
        """Score a list of (query, chunk) pairs and return sigmoid relevance probs."""
        torch = self._torch
        scores: list[float] = []
        for i in range(0, len(pairs), batch_size):
            batch = pairs[i : i + batch_size]
            queries = [q for q, _ in batch]
            chunks = [c for _, c in batch]
            enc = self.tokenizer(
                queries, chunks,
                padding=True, truncation=True, max_length=512,
                return_tensors="pt",
            ).to(self.device)
            with torch.no_grad():
                logits = self.model(**enc).logits.squeeze(-1)
                probs = torch.sigmoid(logits)
            scores.extend(probs.float().cpu().tolist())
        return scores


@lru_cache(maxsize=1)
def get_reranker():
    """Load the cross-encoder once and cache it. First call downloads the model.

    If `RERANKER_ADAPTER_PATH` is set, returns the LoRA-fine-tuned variant
    instead of the stock BGE-reranker.
    """
    device = os.environ.get("RERANKER_DEVICE", DEFAULT_DEVICE)
    adapter_path = os.environ.get("RERANKER_ADAPTER_PATH", "").strip()
    if adapter_path:
        return _FtReranker(RERANKER_MODEL, adapter_path, device)
    from sentence_transformers import CrossEncoder

    logger.info(f"Loading reranker: {RERANKER_MODEL} on device={device} (stock; first run downloads ~568MB)")
    return CrossEncoder(RERANKER_MODEL, device=device)


def rerank(query: str, chunks: list[dict], top_k: int = 5) -> list[dict]:
    """Rerank chunks by (query, chunk) relevance, return top-k with updated scores.

    Each returned chunk gains a `rerank_score` in metadata alongside its original
    retrieval score (which is kept for diagnostics / debugging).

    Sprint 8e: per-pair Redis cache. Same (query, chunk_text) pair produces
    a deterministic score (cross-encoder at temperature 0), so we look up
    each pair before invoking the model and only call the model on misses.
    Hit rate is high in practice — repeated retrieval retries against the
    same chunk pool, and cross-question chunk overlap on the same filing
    (Apple 10-K chunks score against many Apple-related queries).
    """
    if not chunks:
        return []

    # Identify the model variant in the cache key so swapping the LoRA
    # adapter or the base model invalidates pre-FT scores automatically.
    adapter_path = os.environ.get("RERANKER_ADAPTER_PATH", "").strip()
    model_id = f"{RERANKER_MODEL}|adapter={adapter_path}" if adapter_path else RERANKER_MODEL

    # Probe each pair's cache. Track which need a model forward pass.
    from src.services.result_cache import _KEY_PREFIX, _client, _hash_key

    cache_prefix = _KEY_PREFIX["reranker"]
    cached_scores: dict[int, float] = {}
    pending_indices: list[int] = []
    pending_pairs: list[tuple[str, str]] = []

    for i, c in enumerate(chunks):
        chunk_text = c.get("content", "")
        key = cache_prefix + _hash_key(model_id, query, chunk_text)
        hit = _client.get(key)
        if isinstance(hit, (int, float)):
            cached_scores[i] = float(hit)
        else:
            pending_indices.append(i)
            pending_pairs.append((query, chunk_text))

    # Compute scores only for misses, then store them.
    if pending_pairs:
        reranker = get_reranker()
        new_scores = reranker.predict(pending_pairs)
        for idx, score in zip(pending_indices, new_scores):
            score_f = float(score)
            cached_scores[idx] = score_f
            chunk_text = chunks[idx].get("content", "")
            key = cache_prefix + _hash_key(model_id, query, chunk_text)
            _client.set(key, score_f)

    scored = [
        {**chunk, "rerank_score": cached_scores[i]}
        for i, chunk in enumerate(chunks)
    ]
    scored.sort(key=lambda c: c["rerank_score"], reverse=True)
    return scored[:top_k]
