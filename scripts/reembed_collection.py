"""Re-embed an existing Qdrant collection with a new embedding model.

Avoids re-running Docling + chunking (which takes ~90 min for FinanceBench).
Reads chunks from the source collection, re-computes dense embeddings via the
configured EMBEDDING_MODEL, regenerates sparse (BM25) vectors, and writes the
result to a new collection.

Usage:
    EMBEDDING_MODEL=text-embedding-3-large EMBEDDING_DIMENSIONS=3072 \
    RAG_COST_RUN_ID=sprint_7_7_day6_embed_large \
    python scripts/reembed_collection.py \
      --source financebench_corpus_pypdf_clean \
      --target financebench_corpus_pypdf_emb_large \
      --batch-size 256

Cost note: OpenAI text-embedding-3-large is $0.13/MTok. 68k chunks at ~500
tokens each ≈ 34M tokens ≈ ~$4.42. text-embedding-3-small ≈ $0.68 to do the
same.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from pathlib import Path

# Ensure repo root importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qdrant_client.models import PointStruct  # noqa: E402
from tqdm import tqdm  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.services.embeddings import embed_texts  # noqa: E402
from src.services.vector_store import (  # noqa: E402
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    compute_sparse_vectors,
    ensure_collection,
    get_qdrant_client,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _scroll_all(client, source: str, page_size: int):
    """Yield all points from the source collection (with payload, no vectors)."""
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=source,
            limit=page_size,
            with_payload=True,
            with_vectors=False,  # we're recomputing both dense and sparse
            offset=offset,
        )
        for p in points:
            yield p
        if offset is None:
            return


def _count_points(client, collection: str) -> int:
    return client.count(collection_name=collection, count_filter=None).count


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-embed an existing Qdrant collection with a new embedding model.")
    parser.add_argument("--source", required=True, help="Source collection (read existing chunks from here)")
    parser.add_argument("--target", required=True, help="Target collection (created if missing, written here)")
    parser.add_argument("--batch-size", type=int, default=256, help="Embedding + upsert batch size")
    parser.add_argument("--scroll-size", type=int, default=256, help="Qdrant scroll page size")
    parser.add_argument("--limit", type=int, default=None, help="Re-embed only first N points (smoke test)")
    args = parser.parse_args()

    print(f"Re-embedding configuration:")
    print(f"  EMBEDDING_MODEL:      {settings.EMBEDDING_MODEL}")
    print(f"  EMBEDDING_DIMENSIONS: {settings.EMBEDDING_DIMENSIONS}")
    print(f"  source -> target:     {args.source} -> {args.target}")
    print(f"  batch_size:           {args.batch_size}")
    print()

    client = get_qdrant_client()
    n_source = _count_points(client, args.source)
    print(f"Source has {n_source:,} points")
    if args.limit:
        n_source = min(n_source, args.limit)
        print(f"  --limit set; re-embedding first {n_source} points")

    # Create target collection. ensure_collection reads settings.EMBEDDING_DIMENSIONS,
    # so the dim of the new dense vector slot matches the new model automatically.
    ensure_collection(client, args.target)
    n_target_existing = _count_points(client, args.target)
    if n_target_existing > 0:
        print(f"  ⚠ target already has {n_target_existing:,} points (resume not supported here; will append)")

    start = time.time()
    n_done = 0
    batch_chunks: list = []  # buffer of source-points for the current upsert batch
    pbar = tqdm(total=n_source, desc="re-embed", unit="pt")

    def flush(buf: list) -> None:
        if not buf:
            return
        # Use payload["content"] (with contextual prefix) — same as the original ingest
        texts = [(p.payload or {}).get("content", "") for p in buf]
        dense_vectors = embed_texts(texts)
        sparse_vectors = compute_sparse_vectors(texts)
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector={
                    DENSE_VECTOR_NAME: dense,
                    SPARSE_VECTOR_NAME: sparse,
                },
                payload=dict(p.payload or {}),  # copy as-is (preserves all FB metadata)
            )
            for p, dense, sparse in zip(buf, dense_vectors, sparse_vectors)
        ]
        client.upsert(collection_name=args.target, points=points)

    for p in _scroll_all(client, args.source, args.scroll_size):
        batch_chunks.append(p)
        if len(batch_chunks) >= args.batch_size:
            flush(batch_chunks)
            n_done += len(batch_chunks)
            pbar.update(len(batch_chunks))
            batch_chunks = []
        if args.limit and n_done >= args.limit:
            break

    # Flush trailing batch
    if batch_chunks and (not args.limit or n_done < args.limit):
        flush(batch_chunks)
        n_done += len(batch_chunks)
        pbar.update(len(batch_chunks))
    pbar.close()

    elapsed = time.time() - start
    n_target_after = _count_points(client, args.target)
    print()
    print(f"Done in {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print(f"  re-embedded:            {n_done:,}")
    print(f"  target collection size: {n_target_after:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
