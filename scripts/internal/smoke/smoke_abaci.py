"""Smoke test for AbaciNLP-hosted Fin-E5 embeddings.

Probes the AbaciNLP API at https://abacinlp.com/v1 to discover:
  - Which model name(s) actually work (their docs use `abacinlp-text-v1` as
    an example but don't confirm a single canonical name)
  - The output embedding dimension (not documented anywhere we've seen)
  - Whether the OpenAI-SDK shim works against their endpoint
  - The Instruct-prefix mechanism for queries (docs say to prepend it manually)
  - Per-chunk latency and a token-budget projection for the 68k re-embed

Run after setting:
    EMBEDDING_PROVIDER=abaci
    EMBEDDING_MODEL=<model-name>      # try abacinlp-text-v1 first
    EMBEDDING_DIMENSIONS=<discovered>  # set after we know the dim
    ABACI_NLP_API_KEY=sk-...

Usage:
    EMBEDDING_PROVIDER=abaci EMBEDDING_MODEL=abacinlp-text-v1 \
        python scripts/smoke_abaci.py
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
SAMPLE_QUERY = "What were CVS Health total revenues in 2022?"


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na * nb else 0.0


def main() -> int:
    print("=== AbaciNLP-hosted Fin-E5 smoke ===")
    print(f"  EMBEDDING_PROVIDER:   {settings.EMBEDDING_PROVIDER}")
    print(f"  EMBEDDING_MODEL:      {settings.EMBEDDING_MODEL}")
    print(f"  EMBEDDING_DIMENSIONS: {settings.EMBEDDING_DIMENSIONS} (will verify)")
    print(f"  ABACI_NLP_API_KEY:    {'set' if settings.ABACI_NLP_API_KEY else 'MISSING'}")
    print()

    if settings.EMBEDDING_PROVIDER != "abaci":
        print("ABORT: EMBEDDING_PROVIDER must be 'abaci' for this smoke.")
        return 1
    if not settings.ABACI_NLP_API_KEY:
        print("ABORT: ABACI_NLP_API_KEY not set in .env.")
        return 1

    # Try a 1-text probe first to discover dimension cheaply (and surface auth errors clearly)
    print("[1] Single-text probe — verify auth + discover dim")
    try:
        t0 = time.time()
        v = embed_text("Total revenues for fiscal year 2022", input_type="query")
        t = time.time() - t0
        print(f"    OK — dim={len(v)}  latency={t:.2f}s  first-3-vals={[round(x,4) for x in v[:3]]}")
    except Exception as e:
        print(f"    FAIL — {type(e).__name__}: {e}")
        print()
        print("    Common causes:")
        print("      - Wrong EMBEDDING_MODEL (try: abacinlp-text-v1, abaci-finance-v1, fine5)")
        print("      - Wrong base URL (we use https://abacinlp.com/v1)")
        print("      - API key invalid/expired")
        print("      - Rate-limited; retry in 60s")
        return 1
    discovered_dim = len(v)
    print()

    # Pull 5 corpus chunks and embed them as documents
    print("[2] Document batch — embed 5 corpus chunks")
    client = get_qdrant_client()
    points, _ = client.scroll(
        collection_name=COLLECTION,
        limit=N_CHUNKS,
        with_payload=True,
        with_vectors=False,
    )
    contents = [(p.payload or {}).get("content", "") for p in points]
    companies = [(p.payload or {}).get("company", "?") for p in points]
    for i, (c, comp) in enumerate(zip(contents, companies), 1):
        print(f"    [{i}] {comp} | {len(c)} chars | {c[:80]!r}...")

    t0 = time.time()
    chunk_vecs = embed_texts(contents, input_type="document")
    t_doc = time.time() - t0
    print(f"    embed_texts(document): {t_doc:.2f}s for 5 chunks "
          f"({1000 * t_doc / 5:.0f} ms/chunk)  dim={len(chunk_vecs[0])}")
    if len(chunk_vecs[0]) != discovered_dim:
        print(f"    WARNING: doc-dim {len(chunk_vecs[0])} != query-dim {discovered_dim}")
    print()

    # Query↔chunk cosine sanity
    print(f"[3] Cosine sanity — Query: {SAMPLE_QUERY!r}")
    t0 = time.time()
    qvec = embed_text(SAMPLE_QUERY, input_type="query")
    t_q = time.time() - t0
    print(f"    embed_text(query): {t_q:.2f}s  dim={len(qvec)}")
    print(f"    Cosine vs each chunk:")
    sims = [_cosine(qvec, cv) for cv in chunk_vecs]
    for sim, comp, c in sorted(zip(sims, companies, contents), reverse=True):
        print(f"      {sim:+.4f}  {comp:<25}  {c[:90]!r}")
    print()

    # Token + cost projection
    avg_chars = sum(len(c) for c in contents) / len(contents)
    est_tokens_per_chunk = avg_chars / 4
    est_total_tokens = est_tokens_per_chunk * 68_059
    print("[4] Projection for 68k re-embed:")
    print(f"    avg chunk: {avg_chars:.0f} chars (~{est_tokens_per_chunk:.0f} tokens)")
    print(f"    est total: {est_total_tokens / 1e6:.1f}M tokens")
    print(f"    AbaciNLP free tier (per docs): 10M tokens — "
          f"{'YES, FITS' if est_total_tokens < 10e6 else 'NO, EXCEEDS'}")
    print(f"    @ {5 / t_doc:.1f} chunks/sec batched, 68k chunks ≈ "
          f"{68_059 * t_doc / 5 / 60:.1f} min wall (single-stream)")

    print()
    print("Smoke OK. Set EMBEDDING_DIMENSIONS=" f"{discovered_dim}"
          " in env for the re-embed run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
