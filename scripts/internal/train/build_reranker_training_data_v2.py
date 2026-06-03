"""Sprint 7.19 Step 1: build reranker training data for LoRA FT v2.

Why v2 differs from v1 (Sprint 7.9):

  Sprint 7.9 v1 policy was outcome-conditioned: positives from PASSING-question
  contexts only, hard negatives from top-30 retrieval. Sprint 7.19 Step 0
  measured that loading v1 in the current pipeline REGRESSES pass-rate by
  −5.34pp under κ=0.932 — v1 doesn't generalise to (a) the current downstream
  stack (Sonnet 4.6 hallu + clauses 7-8) or (b) the κ=0.932 judge. v2's
  training-data policy is built to attack the failure modes that v1 doesn't
  handle:

  POSITIVES — gold-anchored, not outcome-anchored:
    - Source: tests/evaluation/phase_eval_data/v1/gold_chunks.jsonl (147 of
      150 questions have explicit gold labels)
    - Includes FAILING questions' gold chunks (v1 excluded these). The chunk
      being right doesn't depend on whether the pipeline succeeded — that's
      generator/synthesizer failure, not chunk failure.
    - Rationale: Sprint 7.19 Step 0 showed outcome-conditioning baked in the
      stale judge framework's failure modes. Gold-anchored labels are judge-
      independent and survive downstream upgrades.

  HARD NEGATIVES — top-200 (not top-30):
    - Source: fresh top-200 hybrid retrieval against the voyage collection,
      minus the gold chunks for that question.
    - Rationale: Sprint 7.18a Signal 15 measured that the failure mode is
      *distractor displacement* at the reranker output — broader pools
      surface topically-relevant-but-numerically-wrong line items. v2 needs
      to learn to discriminate against ranks 31-200 (which v1 never saw).

  EXPLICIT DISTRACTOR NEGATIVES (a new category for v2):
    - For each question that regressed under Sprint 7.18a (k=200 broader
      retrieval), the chunks that made the regression top-8 but ARE NOT in
      gold are known wrong-distractor chunks. These get labeled as hard
      negatives for those queries — a direct supervision signal for the
      mechanism that crashes the pipeline.
    - Source: Sprint 7.18a pipeline cache cross-referenced with gold_chunks.

  STRATIFICATION — by question type:
    - FinanceBench's `question_type` field (domain-relevant / novel-generated
      / metrics-generated) partitioned across train/val so val isn't a single
      type.

Output: data/training/reranker_ft_v2/{train.jsonl, val.jsonl, manifest.json}
        85/15 split BY QUESTION (same as v1; prevents chunk-level leakage).

Each row: {"query": str, "chunk": str, "label": 0 | 1, "fb_id": str,
           "type": "pos" | "hard_neg" | "distractor_neg",
           "question_type": str}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qdrant_client.models import (  # noqa: E402
    FusionQuery,
    Fusion,
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

GOLD_PATH = Path("tests/evaluation/phase_eval_data/v1/gold_chunks.jsonl")
FB_DATASET_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")
# Sprint 7.18a regression run — contains k=200 retrieved chunks that crashed pass-rate.
# Used to harvest explicit distractor-negative labels for the 25 regressing questions.
TOPK_200_PIPELINE_PATH = Path("tests/evaluation/eval_results/financebench_retrieval_topk_200_v1.pipeline.json")
TOPK_200_DIFF_PATH = Path("tests/evaluation/eval_results/financebench_retrieval_topk_200_v1.rejudged_sonnet_v2.diff.json")
BASELINE_CORRECTNESS_PATH = Path("tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_gen_v2.rejudged_sonnet_v2.correctness.json")
COLLECTION = "financebench_corpus_pypdf_voyage_finance2"

OUT_DIR = Path("data/training/reranker_ft_v2")

DEFAULT_TOP_K = 200
DEFAULT_VAL_FRAC = 0.15
DEFAULT_SEED = 42
# Per-question hard-negative cap to keep the train-set pos:neg ratio sane.
# v1 ran at 1:6.1 successfully. Without a cap, top-200 retrieval yields ~1:80
# which dilutes positive gradients and lets the model achieve high accuracy
# by predicting all-negative. Capping at 15 negs per Q × 147 Qs ≈ 2200 negs +
# 363 positives ≈ 1:6 ratio, matching v1. Distractor negatives are NOT capped
# — they're the targeted-supervision signal v2 exists to use.
DEFAULT_NEG_PER_Q = 15


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


def _retrieve_top_k(client, query: str, top_k: int) -> list[dict]:
    """Hybrid retrieval (dense + sparse RRF). Matches production retrieval shape."""
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
            "qdrant_id": str(h.id),
            "content": content,
            "score": float(h.score),
            "company": p.get("company"),
            "doc_type": p.get("doc_type"),
            "page": p.get("page_number"),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Sprint 7.19 Step 1 — reranker FT v2 training data")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--neg-per-q", type=int, default=DEFAULT_NEG_PER_Q,
                        help=f"Cap hard-negatives per question (default {DEFAULT_NEG_PER_Q}). "
                             f"Distractor-negatives are NOT capped.")
    parser.add_argument("--val-frac", type=float, default=DEFAULT_VAL_FRAC)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N questions (for smoke / quick iteration)")
    args = parser.parse_args()

    print("=" * 90)
    print("Sprint 7.19 Step 1 — reranker FT v2 training-data construction")
    print("=" * 90)
    print(f"  gold labels:    {GOLD_PATH}")
    print(f"  collection:     {COLLECTION}")
    print(f"  EMBEDDING:      {settings.EMBEDDING_PROVIDER}/{settings.EMBEDDING_MODEL} (dim={settings.EMBEDDING_DIMENSIONS})")
    print(f"  top-k:          {args.top_k}")
    print(f"  val frac:       {args.val_frac}")
    print(f"  seed:           {args.seed}")
    print(f"  output dir:     {OUT_DIR}\n")

    if settings.EMBEDDING_PROVIDER != "voyage":
        print(f"ABORT: EMBEDDING_PROVIDER must be 'voyage' (got {settings.EMBEDDING_PROVIDER!r}).")
        return 1

    # --- Load sources --------------------------------------------------------
    gold_records = [json.loads(l) for l in GOLD_PATH.open() if l.strip()]
    print(f"Loaded {len(gold_records)} gold records.")

    fb_records = {json.loads(l)["financebench_id"]: json.loads(l) for l in FB_DATASET_PATH.open()}
    print(f"Loaded {len(fb_records)} FinanceBench records.")

    baseline = json.loads(BASELINE_CORRECTNESS_PATH.read_text())
    baseline_pass_by_fb = {r["fb_id"]: r.get("pass") for r in baseline["per_sample"]}
    print(f"Baseline κ=0.932 PASS/FAIL: "
          f"{sum(1 for v in baseline_pass_by_fb.values() if v)}/{len(baseline_pass_by_fb)} pass")

    # Distractor negatives — chunks from Sprint 7.18a top-200 pipeline that ended
    # up in the regressing questions' relevant_chunks but ARE NOT gold.
    distractor_negs_by_fb: dict[str, list[str]] = defaultdict(list)
    if TOPK_200_PIPELINE_PATH.exists() and TOPK_200_DIFF_PATH.exists():
        topk200_pipe = json.loads(TOPK_200_PIPELINE_PATH.read_text())
        topk200_correctness = json.loads(
            Path(str(TOPK_200_PIPELINE_PATH).replace(".pipeline.json", ".rejudged_sonnet_v2.correctness.json")).read_text()
        )
        topk200_pass_by_fb = {r["fb_id"]: r.get("pass") for r in topk200_correctness["per_sample"]}

        # Regressing fb_ids = passing in baseline AND failing in top200
        regressed_ids = {
            fb for fb in baseline_pass_by_fb
            if baseline_pass_by_fb.get(fb) and not topk200_pass_by_fb.get(fb)
        }
        print(f"Sprint 7.18a regressed cases: {len(regressed_ids)}")

        # Match each regressed fb_id back to its top-200 contexts via question text
        questions_to_idx = {q: i for i, q in enumerate(topk200_pipe["questions"])}
        for fb_id in regressed_ids:
            fb_q = fb_records.get(fb_id, {}).get("question", "")
            idx = questions_to_idx.get(fb_q)
            if idx is None:
                continue
            ctx_list = topk200_pipe["contexts"][idx]
            for c in ctx_list:
                if c and c.strip():
                    distractor_negs_by_fb[fb_id].append(c)
        n_distractor = sum(len(v) for v in distractor_negs_by_fb.values())
        print(f"Distractor negatives harvested: {n_distractor} across {len(distractor_negs_by_fb)} regressing questions")
    else:
        print(f"WARN: Sprint 7.18a pipeline cache not found at {TOPK_200_PIPELINE_PATH} — "
              f"no explicit distractor negatives this run.")

    # --- Train/val split BY QUESTION, stratified by question_type ------------
    rng = random.Random(args.seed)
    qs_with_gold = [g for g in gold_records if g.get("gold_chunks")]
    if args.limit:
        qs_with_gold = qs_with_gold[: args.limit]

    by_type: dict[str, list[dict]] = defaultdict(list)
    for g in qs_with_gold:
        fb_id = g["financebench_id"]
        qt = fb_records.get(fb_id, {}).get("question_type", "unknown")
        by_type[qt].append(g)

    train_ids: set[str] = set()
    val_ids: set[str] = set()
    for qt, lst in by_type.items():
        shuffled = lst.copy()
        rng.shuffle(shuffled)
        n_val = max(1, int(round(len(shuffled) * args.val_frac)))
        for g in shuffled[:n_val]:
            val_ids.add(g["financebench_id"])
        for g in shuffled[n_val:]:
            train_ids.add(g["financebench_id"])

    print(f"\nQuestion split (stratified by question_type):")
    for qt, lst in by_type.items():
        n_t = sum(1 for g in lst if g["financebench_id"] in train_ids)
        n_v = sum(1 for g in lst if g["financebench_id"] in val_ids)
        print(f"  {qt}: {n_t} train / {n_v} val (total {len(lst)})")
    print()

    # --- Build training rows -------------------------------------------------
    client = get_qdrant_client()
    train_rows: list[dict] = []
    val_rows: list[dict] = []
    n_pos = n_hard_neg = n_distractor_neg = 0
    skipped_no_neg = 0
    skipped_qdrant_miss = 0

    for j, g in enumerate(qs_with_gold):
        fb_id = g["financebench_id"]
        fb_rec = fb_records.get(fb_id, {})
        q = fb_rec.get("question", "")
        qt = fb_rec.get("question_type", "unknown")
        if not q:
            continue

        # Positives: gold chunks (pull content from Qdrant by qdrant_id)
        gold_qids = [str(c["qdrant_id"]) for c in g["gold_chunks"]]
        if not gold_qids:
            continue
        gold_points = client.retrieve(
            collection_name=COLLECTION, ids=gold_qids,
            with_payload=True, with_vectors=False,
        )
        positives = []
        for p in gold_points:
            content = (p.payload or {}).get("content", "")
            if content.strip():
                positives.append({"qdrant_id": str(p.id), "content": content})
        if not positives:
            skipped_qdrant_miss += 1
            continue
        pos_qids = {p["qdrant_id"] for p in positives}
        pos_hashes = {_content_hash(p["content"]) for p in positives}

        # Hard negatives: top-200 retrieval minus positives, capped per Q.
        # Keep the top-ranked negatives (highest retrieval score) — these are
        # the "near-miss" chunks the reranker most needs to learn to demote.
        retrieved = _retrieve_top_k(client, q, args.top_k)
        hard_negs_all = [
            r for r in retrieved
            if r["qdrant_id"] not in pos_qids
            and _content_hash(r["content"]) not in pos_hashes
        ]
        # retrieved is already sorted by RRF score (highest first); take first N.
        hard_negs = hard_negs_all[: args.neg_per_q]

        # Distractor negatives from Sprint 7.18a (for regressed questions)
        distractors = []
        for c in distractor_negs_by_fb.get(fb_id, []):
            if _content_hash(c) not in pos_hashes:
                distractors.append(c)

        if not hard_negs and not distractors:
            skipped_no_neg += 1
            continue

        target = train_rows if fb_id in train_ids else val_rows
        for p in positives:
            target.append({
                "query": q, "chunk": p["content"], "label": 1,
                "fb_id": fb_id, "type": "pos", "question_type": qt,
            })
            n_pos += 1
        for r in hard_negs:
            target.append({
                "query": q, "chunk": r["content"], "label": 0,
                "fb_id": fb_id, "type": "hard_neg", "question_type": qt,
                "retrieval_score": r["score"],
            })
            n_hard_neg += 1
        for c in distractors:
            target.append({
                "query": q, "chunk": c, "label": 0,
                "fb_id": fb_id, "type": "distractor_neg", "question_type": qt,
            })
            n_distractor_neg += 1

        if (j + 1) % 25 == 0 or j == len(qs_with_gold) - 1:
            baseline_status = "P" if baseline_pass_by_fb.get(fb_id) else "F"
            print(f"  [{j+1}/{len(qs_with_gold)}] {fb_id} {qt[:18]:<18} [{baseline_status}] | "
                  f"+{len(positives)}p / +{len(hard_negs)}hn / +{len(distractors)}dn")

    # --- Persist ------------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    train_path = OUT_DIR / "train.jsonl"
    val_path = OUT_DIR / "val.jsonl"
    manifest_path = OUT_DIR / "manifest.json"

    with train_path.open("w") as f:
        for r in train_rows:
            f.write(json.dumps(r) + "\n")
    with val_path.open("w") as f:
        for r in val_rows:
            f.write(json.dumps(r) + "\n")

    n_train_pos = sum(1 for r in train_rows if r["label"] == 1)
    n_train_neg = sum(1 for r in train_rows if r["label"] == 0)
    n_val_pos = sum(1 for r in val_rows if r["label"] == 1)
    n_val_neg = sum(1 for r in val_rows if r["label"] == 0)

    manifest = {
        "version": "v2",
        "policy": "gold-anchored positives + top-K=200 hard negatives + Sprint 7.18a distractor negatives + question-type stratified split",
        "differences_from_v1": [
            "positives are gold-chunk labels (judge-independent), not outcome-conditioned",
            "includes FAILING questions' gold chunks as positives",
            "top-K=200 hard-negative pool (v1 was top-30)",
            "explicit distractor-negative category from Sprint 7.18a regressions",
            "train/val split stratified by FinanceBench question_type",
        ],
        "source": {
            "gold_jsonl": str(GOLD_PATH),
            "fb_dataset_jsonl": str(FB_DATASET_PATH),
            "baseline_correctness_for_distractor_diff": str(BASELINE_CORRECTNESS_PATH),
            "distractor_source_pipeline": str(TOPK_200_PIPELINE_PATH),
            "qdrant_collection": COLLECTION,
            "embedding_provider": settings.EMBEDDING_PROVIDER,
            "embedding_model": settings.EMBEDDING_MODEL,
            "embedding_dim": settings.EMBEDDING_DIMENSIONS,
        },
        "params": {"top_k": args.top_k, "val_frac": args.val_frac, "seed": args.seed},
        "stats": {
            "n_questions_used": len(qs_with_gold) - skipped_no_neg - skipped_qdrant_miss,
            "n_train_questions": len(train_ids),
            "n_val_questions": len(val_ids),
            "n_train_pos": n_train_pos,
            "n_train_neg": n_train_neg,
            "n_train_total": len(train_rows),
            "n_val_pos": n_val_pos,
            "n_val_neg": n_val_neg,
            "n_val_total": len(val_rows),
            "n_pos_global": n_pos,
            "n_hard_neg_global": n_hard_neg,
            "n_distractor_neg_global": n_distractor_neg,
            "skipped_no_negatives": skipped_no_neg,
            "skipped_qdrant_miss": skipped_qdrant_miss,
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
    print(f"  pos:neg ratio: {manifest['stats']['global_pos_neg_ratio']}")
    print(f"  composition: {n_pos} pos, {n_hard_neg} hard_neg, {n_distractor_neg} distractor_neg")
    if skipped_no_neg or skipped_qdrant_miss:
        print(f"  ⚠ skipped {skipped_no_neg} (no negatives) + {skipped_qdrant_miss} (qdrant miss)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
