"""Export a Qdrant collection to a HuggingFace Hub dataset (parquet + manifest + README).

Reads dense + sparse vectors and all payload fields, writes to local parquet,
generates a manifest.json and a non-stale HF dataset README. Does NOT upload —
upload is a separate explicit step (see `--upload` flag).

Output layout:
  <output_dir>/
    chunks.parquet     — all points (chunk_id + content + dense_vector + sparse_*  + metadata)
    manifest.json      — pipeline config + ingest timestamp + source-project version pin
    README.md          — HF Hub dataset card (frozen at snapshot time)

Usage (local generation only):
  python scripts/export_to_hf.py \\
      --collection financebench_corpus_pypdf_voyage_finance2 \\
      --output-dir dist/hf-snapshot-$(date +%Y%m%d_%H%M%S) \\
      --project-version 0.3.0 \\
      --dataset-slug cmpunkmannu/financebench-voyage-finance-2-embeddings

Usage (upload after local review):
  python scripts/export_to_hf.py \\
      --snapshot-dir dist/hf-snapshot-20260601_220000 \\
      --upload \\
      --dataset-slug cmpunkmannu/financebench-voyage-finance-2-embeddings
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from qdrant_client import QdrantClient


# --- Schema --------------------------------------------------------------

# Pyarrow schema for chunks.parquet. Keeping dense + sparse as separate
# columns (rather than a struct) so downstream consumers can read with
# vanilla pandas without struct-handling.
def _parquet_schema(dense_dim: int) -> pa.Schema:
    return pa.schema([
        ("chunk_id", pa.string()),
        ("content", pa.string()),
        ("dense_vector", pa.list_(pa.float32(), dense_dim)),
        ("sparse_indices", pa.list_(pa.int32())),
        ("sparse_values", pa.list_(pa.float32())),
        ("doc_type", pa.string()),
        ("company", pa.string()),
        ("company_name", pa.string()),
        ("fiscal_year", pa.int32()),
        ("confidentiality", pa.string()),
        ("source_file", pa.string()),
        ("num_pages", pa.int32()),
        ("financebench_doc_name", pa.string()),
        ("fb_company", pa.string()),
        ("fb_doc_period", pa.string()),
        ("fb_gics_sector", pa.string()),
        ("chunk_index", pa.int32()),
        ("page_number", pa.int32()),
    ])


# --- Export --------------------------------------------------------------

def _scroll_batched(client: QdrantClient, collection: str, batch_size: int):
    """Yield batches of points from a Qdrant scroll. with_vectors=True returns
    all named vectors (dense + sparse) as a dict keyed by vector name."""
    offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection,
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break
        yield points
        if next_offset is None:
            break
        offset = next_offset


def _point_to_row(point, dense_dim: int) -> dict:
    """Convert one Qdrant point to a parquet-ready row dict."""
    payload = point.payload or {}
    vectors = point.vector or {}

    dense = vectors.get("dense") if isinstance(vectors, dict) else None
    if dense is None or len(dense) != dense_dim:
        # Skipping points without the expected dense vector — should never happen
        # in a clean export, but guarding so a stray legacy point doesn't blow
        # up the whole run. The caller filters None rows.
        return None

    sparse = vectors.get("sparse") if isinstance(vectors, dict) else None
    sparse_indices = list(sparse.indices) if sparse else []
    sparse_values = list(sparse.values) if sparse else []

    return {
        "chunk_id": str(point.id),
        "content": payload.get("content") or "",
        "dense_vector": list(dense),
        "sparse_indices": [int(i) for i in sparse_indices],
        "sparse_values": [float(v) for v in sparse_values],
        "doc_type": payload.get("doc_type"),
        "company": payload.get("company"),
        "company_name": payload.get("company_name"),
        "fiscal_year": payload.get("fiscal_year"),
        "confidentiality": payload.get("confidentiality"),
        "source_file": payload.get("source_file"),
        "num_pages": payload.get("num_pages"),
        "financebench_doc_name": payload.get("financebench_doc_name"),
        "fb_company": payload.get("fb_company"),
        "fb_doc_period": payload.get("fb_doc_period"),
        "fb_gics_sector": payload.get("fb_gics_sector"),
        "chunk_index": payload.get("chunk_index"),
        "page_number": payload.get("page_number"),
    }


def export_collection(
    client: QdrantClient,
    collection: str,
    output_dir: Path,
    batch_size: int,
) -> dict:
    """Stream all points from a collection to chunks.parquet, writing in
    batches so peak memory stays bounded. Returns export stats for the manifest."""
    info = client.get_collection(collection)
    vectors_cfg = info.config.params.vectors
    if hasattr(vectors_cfg, "size"):
        dense_dim = vectors_cfg.size
        dense_distance = vectors_cfg.distance.value if hasattr(vectors_cfg.distance, "value") else str(vectors_cfg.distance)
    else:
        dense_cfg = vectors_cfg["dense"]
        dense_dim = dense_cfg.size
        dense_distance = dense_cfg.distance.value if hasattr(dense_cfg.distance, "value") else str(dense_cfg.distance)

    schema = _parquet_schema(dense_dim)
    parquet_path = output_dir / "chunks.parquet"

    total = 0
    skipped = 0
    distinct_docs: set[str] = set()
    sector_counts: dict[str, int] = {}
    t0 = time.monotonic()
    writer = pq.ParquetWriter(parquet_path, schema, compression="snappy")
    try:
        for batch in _scroll_batched(client, collection, batch_size):
            rows = []
            for point in batch:
                row = _point_to_row(point, dense_dim)
                if row is None:
                    skipped += 1
                    continue
                rows.append(row)
                if row["financebench_doc_name"]:
                    distinct_docs.add(row["financebench_doc_name"])
                if row["fb_gics_sector"]:
                    sector_counts[row["fb_gics_sector"]] = sector_counts.get(row["fb_gics_sector"], 0) + 1
            if not rows:
                continue
            table = pa.Table.from_pylist(rows, schema=schema)
            writer.write_table(table)
            total += len(rows)
            if total % 5000 < batch_size:
                elapsed = time.monotonic() - t0
                rate = total / elapsed if elapsed > 0 else 0
                print(f"  scrolled {total:>6}/{info.points_count} ({rate:.0f} points/s)", flush=True)
    finally:
        writer.close()

    elapsed = time.monotonic() - t0
    size_bytes = parquet_path.stat().st_size
    print(f"  done: {total} points written, {skipped} skipped, "
          f"{size_bytes / 1024 / 1024:.1f} MB in {elapsed:.1f}s")

    return {
        "point_count": total,
        "skipped_count": skipped,
        "dense_dim": dense_dim,
        "dense_distance": dense_distance,
        "parquet_size_bytes": size_bytes,
        "export_seconds": round(elapsed, 1),
        "distinct_doc_count": len(distinct_docs),
        "sector_counts": dict(sorted(sector_counts.items(), key=lambda kv: -kv[1])),
    }


# --- Manifest + README ---------------------------------------------------

def write_manifest(
    output_dir: Path,
    collection: str,
    project_version: str,
    dataset_slug: str,
    export_stats: dict,
) -> dict:
    manifest = {
        "schema_version": "1",
        "source_collection": collection,
        "point_count": export_stats["point_count"],
        "distinct_doc_count": export_stats["distinct_doc_count"],
        "skipped_count": export_stats["skipped_count"],
        "sector_counts": export_stats["sector_counts"],
        "parser": "pypdf",
        "dense_embedding": {
            "model": "voyage/voyage-finance-2",
            "dim": export_stats["dense_dim"],
            "distance": export_stats["dense_distance"],
            "provider": "Voyage AI",
        },
        "sparse_embedding": {
            "encoder": "qdrant-fastembed-bm25",
            "kind": "BM25",
        },
        "license": "CC-BY-NC-4.0",
        "snapshot_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "generator_project": "Rishabhmannu/financebench-rag-agent",
        "generator_version": project_version,
        "dataset_slug": dataset_slug,
        "parquet_size_bytes": export_stats["parquet_size_bytes"],
        "source_corpus": {
            "name": "FinanceBench",
            "url": "https://github.com/patronus-ai/financebench",
            "license": "CC-BY-NC-4.0",
            "citation_arxiv": "2311.11944",
        },
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


_README_TEMPLATE = """---
license: cc-by-nc-4.0
language:
  - en
tags:
  - rag
  - financial-qa
  - finance
  - embeddings
  - voyage
  - qdrant
  - financebench
size_categories:
  - 10K<n<100K
task_categories:
  - sentence-similarity
  - question-answering
---

# FinanceBench (voyage-finance-2 embeddings)

Pre-computed embeddings for the [FinanceBench](https://github.com/patronus-ai/financebench) corpus. Skips ~$5-15 of Voyage API cost and ~30 minutes of ingest time vs re-embedding from raw PDFs. Intended consumer: the RAG agent at [`Rishabhmannu/financebench-rag-agent`](https://github.com/Rishabhmannu/financebench-rag-agent) (install: `pip install financebench-rag-agent`).

## What's in the box (frozen)

| Field | Value |
|---|---|
| Source corpus | FinanceBench (SEC filings: 10-K, 10-Q, 8-K, earnings releases) |
| Parser | pypdf (canonical) |
| Documents | {doc_count} distinct SEC filings |
| Chunks | {point_count_fmt} |
| Dense vectors | `{dense_model}` ({dense_dim}-dim, {dense_distance_lower}) |
| Sparse vectors | BM25 tokens (Qdrant fastembed) |
| File format | Parquet (snappy) + manifest.json |
| License | CC BY-NC 4.0 (inherited from FinanceBench) |

### Sector coverage

{sector_table}

## Use it

> Prerequisites: install the CLI and bring up the local stack — see the project's [docs/setup.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/setup.md). The CLI command below assumes the api container is already running.

### CLI consumer (recommended)

```bash
financebench seed --from-hf {dataset_slug}
```

### Direct parquet (any RAG stack — no project setup needed)

```python
from huggingface_hub import hf_hub_download
import pandas as pd

path = hf_hub_download(
    repo_id="{dataset_slug}",
    filename="chunks.parquet",
    repo_type="dataset",
)
df = pd.read_parquet(path)
```

## Schema (`chunks.parquet`)

| Column | Type | Description |
|---|---|---|
| `chunk_id` | string (UUID) | Stable point ID |
| `content` | string | Header-annotated chunk text |
| `dense_vector` | fixed_size_list<float32>[{dense_dim}] | voyage-finance-2 embedding |
| `sparse_indices` | list<int32> | BM25 sparse vector indices |
| `sparse_values` | list<float32> | BM25 sparse vector values (parallel to indices) |
| `doc_type` | string | e.g. `10k`, `10q`, `earnings` |
| `fb_company` | string | FinanceBench canonical company |
| `fb_doc_period` | string | Fiscal period (year) |
| `fb_gics_sector` | string | GICS sector |
| `financebench_doc_name` | string | FB document ID |
| `page_number` | int32 | PDF page number |
| `chunk_index` | int32 | Position within document |
| `source_file` | string | PDF filename |
| `company`, `company_name`, `fiscal_year`, `confidentiality`, `num_pages` | mixed | Additional payload fields |

`manifest.json` contains pipeline config, the exact dense distance metric, and the snapshot timestamp.

## License and attribution

The chunk text is derived from [FinanceBench](https://github.com/patronus-ai/financebench) (Islam et al., 2023), released under CC BY-NC 4.0. **This dataset inherits that license — non-commercial use only.**

[Voyage AI's `voyage-finance-2`](https://docs.voyageai.com/docs/embeddings) is a commercial API. Embeddings are redistributed under their terms of service. Cite Voyage AI when using.

```bibtex
@article{{islam2023financebench,
  title={{FinanceBench: A New Benchmark for Financial Question Answering}},
  author={{Islam, Pranab and Kannappan, Anand and Kiela, Douwe and others}},
  journal={{arXiv preprint arXiv:2311.11944}},
  year={{2023}}
}}
```

## Snapshot provenance (frozen)

- **Snapshot generated**: `{snapshot_timestamp}`
- **Generated by**: `financebench-rag-agent` `v{generator_version}`
- **Source project**: https://github.com/Rishabhmannu/financebench-rag-agent
- For current eval results, methodology, and pipeline updates: see the project repo's [`docs/`](https://github.com/Rishabhmannu/financebench-rag-agent/tree/main/docs). This README is frozen at snapshot time; the project may have moved on.
"""


def write_readme(output_dir: Path, manifest: dict) -> None:
    distance_lower = manifest["dense_embedding"]["distance"].lower()

    # Sector table — one row per sector ordered by chunk count desc.
    sector_lines = ["| Sector | Chunks |", "|---|---:|"]
    for sec, n in manifest["sector_counts"].items():
        sector_lines.append(f"| {sec} | {n:,} |")
    sector_table = "\n".join(sector_lines)

    content = _README_TEMPLATE.format(
        point_count_fmt=f"{manifest['point_count']:,}",
        doc_count=manifest["distinct_doc_count"],
        sector_table=sector_table,
        dense_model=manifest["dense_embedding"]["model"],
        dense_dim=manifest["dense_embedding"]["dim"],
        dense_distance_lower=distance_lower,
        dataset_slug=manifest["dataset_slug"],
        snapshot_timestamp=manifest["snapshot_timestamp_utc"],
        generator_version=manifest["generator_version"],
    )
    (output_dir / "README.md").write_text(content)


# --- Upload --------------------------------------------------------------

def upload_to_hf(snapshot_dir: Path, dataset_slug: str, private: bool, token: str) -> None:
    """Upload chunks.parquet + manifest.json + README.md to HF Hub as a dataset.
    Creates the repo if it doesn't exist."""
    from huggingface_hub import HfApi, create_repo  # noqa: PLC0415

    api = HfApi(token=token)
    create_repo(
        repo_id=dataset_slug,
        repo_type="dataset",
        private=private,
        exist_ok=True,
        token=token,
    )
    print(f"  repo ready: {dataset_slug} (private={private})", flush=True)

    print(f"  uploading {snapshot_dir} ...", flush=True)
    api.upload_folder(
        folder_path=str(snapshot_dir),
        repo_id=dataset_slug,
        repo_type="dataset",
        commit_message=f"Snapshot upload {snapshot_dir.name}",
    )
    print(f"  done. https://huggingface.co/datasets/{dataset_slug}", flush=True)


# --- CLI -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Local generation flags
    parser.add_argument("--collection", help="Qdrant collection to export.")
    parser.add_argument("--output-dir", help="Local directory to write parquet + manifest + README.")
    parser.add_argument("--project-version", help="Generator project version pin (e.g. 0.3.0).")
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--batch-size", type=int, default=256)

    # Upload flags
    parser.add_argument("--upload", action="store_true", help="Upload a previously-generated snapshot.")
    parser.add_argument("--snapshot-dir", help="Existing local snapshot directory to upload.")
    parser.add_argument("--private", action="store_true", help="Create the dataset as private. Default is public.")

    # Shared
    parser.add_argument("--dataset-slug", required=True, help="HF dataset slug, e.g. user/dataset-name.")

    args = parser.parse_args()

    if args.upload:
        snapshot_dir = Path(args.snapshot_dir) if args.snapshot_dir else None
        if snapshot_dir is None or not snapshot_dir.exists():
            print(f"ERROR: --snapshot-dir {snapshot_dir} not found.", file=sys.stderr)
            sys.exit(2)
        token = os.environ.get("HF_ACCESS_TOKEN") or os.environ.get("HF_TOKEN")
        if not token:
            print("ERROR: HF_ACCESS_TOKEN (or HF_TOKEN) not in environment.", file=sys.stderr)
            sys.exit(2)
        upload_to_hf(
            snapshot_dir=snapshot_dir,
            dataset_slug=args.dataset_slug,
            private=args.private,
            token=token,
        )
        return

    # Local generation path
    if not all([args.collection, args.output_dir, args.project_version]):
        print("ERROR: --collection, --output-dir, --project-version required for local export.", file=sys.stderr)
        sys.exit(2)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Exporting collection '{args.collection}' from {args.qdrant_host}:{args.qdrant_port}")
    print(f"Output: {output_dir}")
    client = QdrantClient(host=args.qdrant_host, port=args.qdrant_port)
    export_stats = export_collection(
        client=client,
        collection=args.collection,
        output_dir=output_dir,
        batch_size=args.batch_size,
    )

    print("\nWriting manifest.json + README.md ...")
    manifest = write_manifest(
        output_dir=output_dir,
        collection=args.collection,
        project_version=args.project_version,
        dataset_slug=args.dataset_slug,
        export_stats=export_stats,
    )
    write_readme(output_dir, manifest)

    print(f"\nSnapshot ready at: {output_dir}")
    print(f"  chunks.parquet  ({export_stats['parquet_size_bytes'] / 1024 / 1024:.1f} MB)")
    print(f"  manifest.json")
    print(f"  README.md")
    print("\nReview the outputs, then upload with:")
    print(f"  python scripts/export_to_hf.py --upload --snapshot-dir {output_dir} --dataset-slug {args.dataset_slug}")


if __name__ == "__main__":
    main()
