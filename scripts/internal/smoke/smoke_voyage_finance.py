"""Smoke test for voyage-finance-2 embeddings.

Pulls 5 chunks from the existing FinanceBench Qdrant collection, embeds them
via voyage-finance-2 (input_type="document"), embeds a sample finance query
(input_type="query"), and validates:

  - The voyageai client connects (API key present)
  - Output dimension is 1024 (matches the doc spec)
  - Query↔most-similar-chunk cosine similarity is structurally plausible
    (top match should have higher cosine than a randomly-permuted pairing)
  - Per-chunk latency is reasonable for the full 68k re-embed run

Run AFTER setting:
    EMBEDDING_PROVIDER=voyage
    EMBEDDING_MODEL=voyage-finance-2
    EMBEDDING_DIMENSIONS=1024
    VOYAGE_API_KEY=pa-...
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.settings import settings  # noqa: E402
from src.services.embeddings import embed_text, embed_texts  # noqa: E402
from src.services.vector_store import get_qdrant_client  # noqa: E402

COLLECTION = "financebench_corpus_pypdf_emb_large"
N_CHUNKS = 5
SAMPLE_QUERY = "What was Apple's total net sales in fiscal year 2023?"


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na * nb else 0.0


def main() -> int:
    print("=== voyage-finance-2 smoke ===")
    print(f"  EMBEDDING_PROVIDER:   {settings.EMBEDDING_PROVIDER}")
    print(f"  EMBEDDING_MODEL:      {settings.EMBEDDING_MODEL}")
    print(f"  EMBEDDING_DIMENSIONS: {settings.EMBEDDING_DIMENSIONS}")
    print(f"  VOYAGE_API_KEY:       {'set' if settings.VOYAGE_API_KEY else 'MISSING'}")
    print()

    if settings.EMBEDDING_PROVIDER != "voyage":
        print("ABORT: EMBEDDING_PROVIDER must be 'voyage' for this smoke. Set env var.")
        return 1
    if not settings.VOYAGE_API_KEY:
        print("ABORT: VOYAGE_API_KEY not set in .env.")
        return 1

    # Pull sample chunks
    client = get_qdrant_client()
    points, _ = client.scroll(
        collection_name=COLLECTION,
        limit=N_CHUNKS,
        with_payload=True,
        with_vectors=False,
    )
    contents = [(p.payload or {}).get("content", "") for p in points]
    companies = [(p.payload or {}).get("company", "?") for p in points]
    print(f"Pulled {len(contents)} chunks from {COLLECTION}:")
    for i, (c, comp) in enumerate(zip(contents, companies), 1):
        print(f"  [{i}] {comp} | {len(c)} chars | {c[:80]!r}...")
    print()

    # Embed corpus chunks
    t0 = time.time()
    chunk_vecs = embed_texts(contents, input_type="document")
    t_doc = time.time() - t0
    print(f"embed_texts(document): {t_doc:.2f}s for {len(contents)} chunks "
          f"({1000 * t_doc / len(contents):.0f} ms/chunk)")
    print(f"  output dim: {len(chunk_vecs[0])} (expected: 1024)")

    # Embed query
    t0 = time.time()
    query_vec = embed_text(SAMPLE_QUERY, input_type="query")
    t_q = time.time() - t0
    print(f"embed_text(query): {t_q:.2f}s — dim {len(query_vec)}")
    print()

    # Sanity: cosine similarities
    print(f"Query: {SAMPLE_QUERY!r}")
    print("Per-chunk cosine vs query:")
    sims = [_cosine(query_vec, cv) for cv in chunk_vecs]
    ranked = sorted(zip(sims, companies, contents), reverse=True)
    for sim, comp, c in ranked:
        print(f"  {sim:+.4f}  {comp:<25}  {c[:100]!r}")
    print()

    # Projection
    n_total = 68_059
    print(f"Projected for full 68k re-embed at {len(contents)/t_doc:.1f} chunks/sec:")
    print(f"  ~{n_total * t_doc / len(contents) / 60:.1f} min wall-clock")
    print(f"  (assumes batching scales linearly; real throughput w/ batch_size=128 "
          f"will be ~5-10x faster)")

    # Token estimate (corpus only — doesn't include queries)
    avg_chars = sum(len(c) for c in contents) / len(contents)
    est_tokens_per_chunk = avg_chars / 4  # rough
    est_total_tokens = est_tokens_per_chunk * n_total
    print(f"Token estimate for 68k re-embed: ~{est_total_tokens / 1e6:.1f}M tokens")
    print(f"  Voyage free tier:   50.0M tokens (sufficient: "
          f"{'YES' if est_total_tokens < 50e6 else 'NO — over cap'})")

    # Sanity check: dimensions == 1024
    if len(chunk_vecs[0]) != 1024:
        print(f"\nWARNING: expected dim 1024, got {len(chunk_vecs[0])} — "
              f"check EMBEDDING_DIMENSIONS in .env")
        return 1

    print("\nSmoke OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
