"""Sprint 7.9 Day 4: build training data for BGE reranker LoRA fine-tune.

Data construction policy (outcome-conditioned + hard negatives, locked Day 4):

  POSITIVES — chunks the pipeline used for a question that PASSED correctness:
    - Source: contexts list in `financebench_pypdf_voyage_finance2.pipeline.json`,
      filtered to questions where `correctness.pass == True`
    - Rationale: these chunks were good enough to lead to a correct answer end-to-end

  HARD NEGATIVES — chunks that retrieval surfaced but the agent rejected:
    - Source: fresh top-30 hybrid retrieval against the voyage collection,
      minus the chunks already used as positives for that question
    - Rationale: chunks the dense+BM25 model thought were relevant but the
      pipeline excluded — exactly the discriminations a fine-tuned reranker
      needs to learn

  EXCLUDED:
    - Chunks from FAILING questions (label noise — unclear if chunk was bad
      or generator failed)
    - Random cross-question negatives (too easy; weaker training signal than
      same-question hard negatives)

Output: data/training/reranker_ft_v1/{train.jsonl, val.jsonl, manifest.json}
        85/15 split BY QUESTION (val questions are different from train ones,
        prevents test-leakage where the model learns chunk-level patterns).

Each row: {"query": str, "chunk": str, "label": 0 | 1, "fb_id": str, "split": "pos" | "hard_neg"}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qdrant_client.models import (  # noqa: E402
    Filter,
    FusionQuery,
    Fusion,
    NamedSparseVector,
    Prefetch,
    SparseVector,
)

from src.config.settings import settings  # noqa: E402
from src.services.embeddings import embed_text  # noqa: E402
from src.services.vector_store import (  # noqa: E402
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    compute_sparse_vectors,
    get_qdrant_client,
)

PIPELINE_PATH = Path("tests/evaluation/eval_results/financebench_pypdf_voyage_finance2.pipeline.json")
CORRECTNESS_PATH = Path("tests/evaluation/eval_results/financebench_pypdf_voyage_finance2.correctness.json")
FB_DATASET_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")
COLLECTION = "financebench_corpus_pypdf_voyage_finance2"

OUT_DIR = Path("data/training/reranker_ft_v1")

DEFAULT_TOP_K = 30
DEFAULT_VAL_FRAC = 0.15
DEFAULT_SEED = 42


def _content_hash(text: str) -> str:
    """Stable identity for a chunk — lets us match a pipeline-saved string
    against a Qdrant scroll point without payload IDs."""
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


def _retrieve_top_k(client, query: str, top_k: int) -> list[dict]:
    """Hybrid retrieval (dense + sparse, RRF) — same pattern as
    src/services/vector_store.py. Returns raw payload + score per hit.
    """
    qdense = embed_text(query, input_type="query")
    qsparse = compute_sparse_vectors([query])[0]
    res = client.query_points(
        collection_name=COLLECTION,
        prefetch=[
            Prefetch(query=qdense, using=DENSE_VECTOR_NAME, limit=top_k),
            Prefetch(
                query=SparseVector(indices=list(qsparse.indices), values=list(qsparse.values)),
                using=SPARSE_VECTOR_NAME,
                limit=top_k,
            ),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=top_k,
        with_payload=True,
    )
    out: list[dict] = []
    for h in res.points:
        p = h.payload or {}
        content = p.get("content") or ""
        if not content:
            continue
        out.append({
            "content": content,
            "score": float(h.score),
            "company": p.get("company"),
            "doc_type": p.get("doc_type"),
            "page": p.get("page_number"),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Build reranker LoRA-FT training data (Sprint 7.9 Day 4)")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                        help=f"Top-K hybrid retrieval per question (default: {DEFAULT_TOP_K})")
    parser.add_argument("--val-frac", type=float, default=DEFAULT_VAL_FRAC,
                        help=f"Fraction of questions held out for validation (default: {DEFAULT_VAL_FRAC})")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N passing questions (for smoke / quick iteration)")
    args = parser.parse_args()

    print("=" * 90)
    print("Sprint 7.9 Day 4 — reranker training data construction")
    print("=" * 90)
    print(f"  pipeline:       {PIPELINE_PATH}")
    print(f"  correctness:    {CORRECTNESS_PATH}")
    print(f"  collection:     {COLLECTION}")
    print(f"  EMBEDDING:      {settings.EMBEDDING_PROVIDER}/{settings.EMBEDDING_MODEL} (dim={settings.EMBEDDING_DIMENSIONS})")
    print(f"  top-k:          {args.top_k}")
    print(f"  val frac:       {args.val_frac}")
    print(f"  seed:           {args.seed}")
    print(f"  output dir:     {OUT_DIR}\n")

    # Sanity: voyage settings must be active so retrieval matches the canonical baseline
    if settings.EMBEDDING_PROVIDER != "voyage":
        print(f"ABORT: EMBEDDING_PROVIDER must be 'voyage' (got {settings.EMBEDDING_PROVIDER!r}).\n"
              f"Run with: EMBEDDING_PROVIDER=voyage EMBEDDING_MODEL=voyage-finance-2 EMBEDDING_DIMENSIONS=1024 python ...")
        return 1

    # Load all three sources
    pipe = json.loads(PIPELINE_PATH.read_text())
    correctness = json.loads(CORRECTNESS_PATH.read_text())["per_sample"]
    questions: list[str] = pipe["questions"]
    contexts: list[list[str]] = pipe["contexts"]
    n_q = len(questions)
    print(f"Loaded {n_q} questions from pipeline cache; {sum(1 for r in correctness if r.get('pass'))} passing.\n")

    # Build by-id index aligned to pipeline order
    correctness_by_idx = {i: r for i, r in enumerate(correctness)}

    # Filter to passing questions only
    passing_idxs = [i for i, r in correctness_by_idx.items() if r.get("pass")]
    if args.limit:
        passing_idxs = passing_idxs[: args.limit]
    print(f"Building training data for {len(passing_idxs)} passing questions...\n")

    # Train/val split BY QUESTION (not by row) so val chunks aren't seen during train
    rng = random.Random(args.seed)
    shuffled = passing_idxs.copy()
    rng.shuffle(shuffled)
    n_val = max(1, int(round(len(shuffled) * args.val_frac)))
    val_idxs = set(shuffled[:n_val])
    train_idxs = set(shuffled[n_val:])
    print(f"Question split: {len(train_idxs)} train / {len(val_idxs)} val\n")

    client = get_qdrant_client()

    # Construction loop
    train_rows: list[dict] = []
    val_rows: list[dict] = []
    n_pos_total = 0
    n_neg_total = 0
    skipped_no_neg = 0

    for j, idx in enumerate(passing_idxs):
        q = questions[idx]
        fb_id = correctness_by_idx[idx]["fb_id"]
        positives = [c for c in contexts[idx] if c.strip()]
        if not positives:
            print(f"  [{j+1}/{len(passing_idxs)}] {fb_id}: no positives, skipping")
            continue

        pos_hashes = {_content_hash(c) for c in positives}

        # Fresh top-K retrieval to harvest hard negatives
        retrieved = _retrieve_top_k(client, q, args.top_k)
        # Hard negatives = retrieved chunks not already positive (by content hash)
        hard_negs = [r for r in retrieved if _content_hash(r["content"]) not in pos_hashes]

        if not hard_negs:
            skipped_no_neg += 1
            print(f"  [{j+1}/{len(passing_idxs)}] {fb_id}: top-{args.top_k} retrieval was a subset of positives; no hard negs (skipping)")
            continue

        target = train_rows if idx in train_idxs else val_rows
        for c in positives:
            target.append({
                "query": q,
                "chunk": c,
                "label": 1,
                "fb_id": fb_id,
                "type": "pos",
            })
            n_pos_total += 1
        for r in hard_negs:
            target.append({
                "query": q,
                "chunk": r["content"],
                "label": 0,
                "fb_id": fb_id,
                "type": "hard_neg",
                "retrieval_score": r["score"],
            })
            n_neg_total += 1
        if (j + 1) % 10 == 0 or j == len(passing_idxs) - 1:
            print(f"  [{j+1}/{len(passing_idxs)}] {fb_id} | "
                  f"+{len(positives)} pos, +{len(hard_negs)} hard_neg")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    train_path = OUT_DIR / "train.jsonl"
    val_path = OUT_DIR / "val.jsonl"
    manifest_path = OUT_DIR / "manifest.json"

    def _write_jsonl(rows: list[dict], path: Path) -> None:
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    _write_jsonl(train_rows, train_path)
    _write_jsonl(val_rows, val_path)

    n_train_pos = sum(1 for r in train_rows if r["label"] == 1)
    n_train_neg = sum(1 for r in train_rows if r["label"] == 0)
    n_val_pos = sum(1 for r in val_rows if r["label"] == 1)
    n_val_neg = sum(1 for r in val_rows if r["label"] == 0)

    manifest = {
        "version": "v1",
        "policy": "outcome-conditioned positives + hard-negative-from-top-K-retrieval",
        "source": {
            "pipeline_json": str(PIPELINE_PATH),
            "correctness_json": str(CORRECTNESS_PATH),
            "qdrant_collection": COLLECTION,
            "embedding_provider": settings.EMBEDDING_PROVIDER,
            "embedding_model": settings.EMBEDDING_MODEL,
            "embedding_dim": settings.EMBEDDING_DIMENSIONS,
        },
        "params": {
            "top_k": args.top_k,
            "val_frac": args.val_frac,
            "seed": args.seed,
        },
        "stats": {
            "n_passing_questions_used": len(passing_idxs) - skipped_no_neg,
            "n_train_questions": len(train_idxs),
            "n_val_questions": len(val_idxs),
            "n_train_pos": n_train_pos,
            "n_train_neg": n_train_neg,
            "n_train_total": len(train_rows),
            "n_val_pos": n_val_pos,
            "n_val_neg": n_val_neg,
            "n_val_total": len(val_rows),
            "skipped_no_negatives": skipped_no_neg,
            "global_pos_neg_ratio": f"1:{(n_train_neg + n_val_neg) / max(1, n_train_pos + n_val_pos):.1f}",
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))

    print()
    print("=" * 90)
    print("Done")
    print("=" * 90)
    print(f"  train: {len(train_rows):,} rows  ({n_train_pos:,} pos / {n_train_neg:,} neg)  → {train_path}")
    print(f"  val:   {len(val_rows):,} rows  ({n_val_pos:,} pos / {n_val_neg:,} neg)  → {val_path}")
    print(f"  manifest:                                                          → {manifest_path}")
    print(f"  pos:neg ratio: {manifest['stats']['global_pos_neg_ratio']}")
    if skipped_no_neg:
        print(f"  ⚠ skipped {skipped_no_neg} questions where top-{args.top_k} retrieval ⊆ positives")
    return 0


if __name__ == "__main__":
    sys.exit(main())
