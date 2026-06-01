"""Round-trip verify the HF snapshot: download → restore → query → compare.

One-off verification script for 0.3.0. The restore logic here is the
prototype for the `financebench seed --from-hf` CLI flag — once this
script proves the contract, the same code moves into cli/commands/seed.py.

Not shipped to users; lives under scripts/ for reproducibility.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm


DATASET_SLUG = "cmpunkmannu/financebench-voyage-finance-2-embeddings"
SOURCE_COLLECTION = "financebench_corpus_pypdf_voyage_finance2"
TEST_COLLECTION = "financebench_test_roundtrip"
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
BATCH_SIZE = 256


def download_parquet() -> Path:
    print(f"Downloading chunks.parquet from {DATASET_SLUG} ...")
    t0 = time.monotonic()
    path = hf_hub_download(
        repo_id=DATASET_SLUG,
        filename="chunks.parquet",
        repo_type="dataset",
    )
    elapsed = time.monotonic() - t0
    size_mb = Path(path).stat().st_size / 1024 / 1024
    print(f"  done: {path} ({size_mb:.1f} MB in {elapsed:.1f}s)")
    return Path(path)


def restore_to_qdrant(client: QdrantClient, parquet_path: Path) -> int:
    """Create test collection and upsert all points from parquet."""
    # Match the source collection's vector config exactly.
    print(f"Creating test collection '{TEST_COLLECTION}' ...")
    try:
        client.delete_collection(TEST_COLLECTION)
    except Exception:
        pass
    client.create_collection(
        collection_name=TEST_COLLECTION,
        vectors_config={"dense": qm.VectorParams(size=1024, distance=qm.Distance.COSINE)},
        sparse_vectors_config={"sparse": qm.SparseVectorParams()},
    )

    print(f"Restoring parquet → Qdrant in batches of {BATCH_SIZE} ...")
    t = pq.read_table(parquet_path)
    total = t.num_rows
    t0 = time.monotonic()

    count = 0
    for start in range(0, total, BATCH_SIZE):
        end = min(start + BATCH_SIZE, total)
        batch = t.slice(start, end - start).to_pylist()
        points = []
        for row in batch:
            sparse_vec = qm.SparseVector(
                indices=row["sparse_indices"],
                values=row["sparse_values"],
            ) if row["sparse_indices"] else None
            vectors: dict = {"dense": row["dense_vector"]}
            if sparse_vec is not None:
                vectors["sparse"] = sparse_vec
            payload = {k: v for k, v in row.items() if k not in {"chunk_id", "dense_vector", "sparse_indices", "sparse_values"}}
            points.append(
                qm.PointStruct(
                    id=row["chunk_id"],
                    vector=vectors,
                    payload=payload,
                )
            )
        client.upsert(collection_name=TEST_COLLECTION, points=points, wait=False)
        count += len(points)
        if count % 5000 < BATCH_SIZE:
            elapsed = time.monotonic() - t0
            rate = count / elapsed if elapsed > 0 else 0
            print(f"  upserted {count:>6}/{total} ({rate:.0f} points/s)", flush=True)

    elapsed = time.monotonic() - t0
    print(f"  done: {count} points in {elapsed:.1f}s")
    return count


def verify_query_match(client: QdrantClient) -> bool:
    """Pull one chunk from the source, query its embedding against BOTH
    collections, confirm the top-3 IDs match exactly."""
    print(f"\nVerifying query parity: source vs test collection ...")

    # Grab one known point from the source — use its dense vector as the query.
    sample_points, _ = client.scroll(
        collection_name=SOURCE_COLLECTION,
        limit=1,
        with_vectors=True,
        with_payload=True,
    )
    if not sample_points:
        print("  ERROR: source collection is empty?")
        return False
    sample = sample_points[0]
    query_vec = sample.vector["dense"] if isinstance(sample.vector, dict) else sample.vector
    print(f"  query chunk_id: {sample.id}")
    print(f"  query origin: {sample.payload.get('source_file')} page {sample.payload.get('page_number')}")

    def _top3(collection: str) -> list[str]:
        res = client.search(
            collection_name=collection,
            query_vector=("dense", query_vec),
            limit=3,
            with_payload=False,
        )
        return [str(r.id) for r in res]

    src_top3 = _top3(SOURCE_COLLECTION)
    tst_top3 = _top3(TEST_COLLECTION)
    print(f"  source top-3:  {src_top3}")
    print(f"  test   top-3:  {tst_top3}")
    match = src_top3 == tst_top3
    print(f"  match: {'YES' if match else 'NO — divergence!'}")
    return match


def main():
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=120)

    parquet_path = download_parquet()
    count = restore_to_qdrant(client, parquet_path)
    print(f"\nRestored {count} points to '{TEST_COLLECTION}'.")
    # Give qdrant a moment to index before querying.
    time.sleep(3)
    ok = verify_query_match(client)
    if not ok:
        sys.exit(1)
    print("\n=== ROUND-TRIP VERIFIED ===")


if __name__ == "__main__":
    main()
