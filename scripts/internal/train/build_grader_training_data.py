"""Sprint 7.17 Phase 1: build training data for grader LoRA fine-tune.

Three parallel datasets covering different negative-sampling strategies (per
"When Fine-Tuning Fails" arXiv 2506.18535 caveat: hard negatives don't always
help — test multiple strategies):

  random   — positives + random non-gold chunks from the same source document
  hard     — positives + hard negatives from retrieval top-50 minus gold (Sprint 7.9 pattern)
  mixed    — positives + 50% random / 50% hard negatives

Positives: 363 gold chunks across 147 FinanceBench questions, from the
Sprint 7.11 gold-chunk labeling (`phase_eval_data/v1/gold_chunks.jsonl`).
Hard-negative pool: top-50 retrieved chunks per question, minus gold (from
`phase_eval_results/financebench_phase_eval_v1_per_question.jsonl`).

Output: data/training/grader_ft_v1/{random,hard,mixed}/{train.jsonl,val.jsonl,manifest.json}
80/15 split BY QUESTION (val questions disjoint from train).

Each row: {"query": str, "chunk": str, "label": 0|1, "fb_id": str, "type": "pos"|"neg_random"|"neg_hard"}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qdrant_client import QdrantClient

GOLD = Path("tests/evaluation/phase_eval_data/v1/gold_chunks.jsonl")
PER_Q = Path("tests/evaluation/phase_eval_results/financebench_phase_eval_v1_per_question.jsonl")
FB_JSONL = Path("data/raw/financebench/financebench_open_source.jsonl")
COLLECTION = "financebench_corpus_pypdf_voyage_finance2"
OUT_DIR = Path("data/training/grader_ft_v1")
POS_NEG_RATIO = 4  # negatives per positive
SEED = 42


def fetch_chunk_text_by_qid(client: QdrantClient, qids: list[str]) -> dict[str, str]:
    """Batch-retrieve chunk texts from Qdrant by qdrant_id (UUID)."""
    out = {}
    batch_size = 100
    for i in range(0, len(qids), batch_size):
        sub = qids[i:i + batch_size]
        pts = client.retrieve(collection_name=COLLECTION, ids=sub, with_payload=True, with_vectors=False)
        for p in pts:
            text = p.payload.get("content") or p.payload.get("raw_content") or ""
            out[str(p.id)] = text
    return out


def fetch_doc_chunk_ids(client: QdrantClient, doc_name: str, exclude_qids: set[str], limit: int = 200) -> list[str]:
    """Scroll same-document chunks (excluding gold) to build the random-negative pool."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    flt = Filter(must=[FieldCondition(key="source_file", match=MatchValue(value=doc_name))])
    pool = []
    offset = None
    while len(pool) < limit:
        records, offset = client.scroll(
            collection_name=COLLECTION, scroll_filter=flt, limit=128,
            offset=offset, with_payload=False, with_vectors=False,
        )
        for r in records:
            if str(r.id) not in exclude_qids:
                pool.append(str(r.id))
        if offset is None:
            break
    return pool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pos-neg-ratio", type=int, default=POS_NEG_RATIO)
    parser.add_argument("--val-frac", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    # Load inputs
    gold_recs = [json.loads(l) for l in open(GOLD) if l.strip()]
    print(f"Loaded {len(gold_recs)} gold-chunk-labeled questions")
    per_q = {json.loads(l)["financebench_id"]: json.loads(l) for l in open(PER_Q) if l.strip()}
    print(f"Loaded {len(per_q)} phase-eval per-question records")
    fb = {json.loads(l)["financebench_id"]: json.loads(l) for l in open(FB_JSONL)}
    print(f"Loaded {len(fb)} FinanceBench questions")

    client = QdrantClient(
        host=os.environ.get("QDRANT_HOST", "localhost"),
        port=int(os.environ.get("QDRANT_PORT", 6333)),
    )

    # Build per-question: question text, gold qids, hard-neg pool (top-50 minus gold)
    per_question = {}
    for gr in gold_recs:
        fid = gr["financebench_id"]
        question = fb.get(fid, {}).get("question", "")
        if not question:
            continue
        gold_qids = [str(c["qdrant_id"]) for c in gr.get("gold_chunks", [])]
        if not gold_qids:
            continue
        # hard negatives: top-50 retrieved minus gold; per_q has retrieved ids as [source_file, chunk_index] tuples
        # but we need qdrant_id (UUID). The hard-neg pool from per_q is by chunk_index, not UUID.
        # We'll do hard-negative selection differently: use same-doc chunks NOT in gold and NOT the gold itself
        # Normalize doc_name to include .pdf extension to match Qdrant source_file
        doc_name = gr["doc_name"]
        if not doc_name.endswith(".pdf"):
            doc_name = doc_name + ".pdf"
        per_question[fid] = {
            "question": question,
            "doc_name": doc_name,
            "gold_qids": gold_qids,
        }
    print(f"Built per-question records for {len(per_question)} Qs")

    # Strategy: for hard negatives, we use top-50 retrieved chunks (by chunk_index) but need to
    # resolve to qdrant_ids. The per_q file has retrieved_top_50_ids as [source_file, chunk_index]
    # pairs. We need to map (source_file, chunk_index) -> qdrant_id.
    print("\nResolving hard-negative qdrant_ids (top-50 retrieved minus gold per question)...")
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    hard_neg_pool = {}  # fid -> [qdrant_id list]
    for fid, info in per_question.items():
        pq = per_q.get(fid, {})
        retrieved = pq.get("retrieved_top_50_ids", [])
        gold_set = set(info["gold_qids"])
        # We need to find qdrant_ids for these (source_file, chunk_index) pairs
        hard_ids = []
        for src_file, ch_idx in retrieved:
            if src_file != info["doc_name"]:
                continue  # only same-doc as gold for fair negatives
            # Query Qdrant for the chunk with this (source_file, chunk_index)
            flt = Filter(must=[
                FieldCondition(key="source_file", match=MatchValue(value=src_file)),
                FieldCondition(key="chunk_index", match=MatchValue(value=ch_idx)),
            ])
            recs, _ = client.scroll(collection_name=COLLECTION, scroll_filter=flt, limit=1,
                                     with_payload=False, with_vectors=False)
            if recs:
                qid = str(recs[0].id)
                if qid not in gold_set:
                    hard_ids.append(qid)
        hard_neg_pool[fid] = hard_ids[:50]  # cap at 50
    print(f"  Hard-negative pools built. Median pool size: "
          f"{sorted([len(v) for v in hard_neg_pool.values()])[len(hard_neg_pool)//2]}")

    # Build random-negative pools (same-doc, not gold) — much larger pool
    print("\nBuilding random-negative pools (same-doc chunks, excluding gold)...")
    random_neg_pool = {}
    for fid, info in per_question.items():
        gold_set = set(info["gold_qids"])
        pool = fetch_doc_chunk_ids(client, info["doc_name"], exclude_qids=gold_set, limit=100)
        random_neg_pool[fid] = pool
    print(f"  Random-negative pools built. Median pool size: "
          f"{sorted([len(v) for v in random_neg_pool.values()])[len(random_neg_pool)//2]}")

    # Fetch chunk texts (gold + all negatives)
    print("\nFetching chunk texts...")
    all_qids = set()
    for fid, info in per_question.items():
        all_qids.update(info["gold_qids"])
        all_qids.update(hard_neg_pool.get(fid, []))
        all_qids.update(random_neg_pool.get(fid, []))
    print(f"  unique chunk IDs to fetch: {len(all_qids)}")
    chunk_text = fetch_chunk_text_by_qid(client, list(all_qids))
    print(f"  fetched {len(chunk_text)} texts")

    # Train/val split BY QUESTION
    fids = sorted(per_question.keys())
    rng.shuffle(fids)
    n_val = int(len(fids) * args.val_frac)
    val_fids = set(fids[:n_val])
    train_fids = set(fids[n_val:])
    print(f"\nSplit: {len(train_fids)} train Qs / {len(val_fids)} val Qs")

    # Build the 3 datasets
    def build_rows(strategy: str, fid_set: set[str]) -> list[dict]:
        rows = []
        for fid in fid_set:
            info = per_question[fid]
            q = info["question"]
            # Positives
            for qid in info["gold_qids"]:
                txt = chunk_text.get(qid)
                if not txt: continue
                rows.append({"query": q, "chunk": txt[:3000], "label": 1, "fb_id": fid, "type": "pos"})
            # Negatives — sample N per gold positive
            n_neg = max(1, len(info["gold_qids"]) * args.pos_neg_ratio)
            if strategy == "random":
                pool = list(random_neg_pool.get(fid, []))
                rng.shuffle(pool)
                neg_qids = pool[:n_neg]
                neg_types = ["neg_random"] * len(neg_qids)
            elif strategy == "hard":
                pool = list(hard_neg_pool.get(fid, []))
                rng.shuffle(pool)
                neg_qids = pool[:n_neg]
                neg_types = ["neg_hard"] * len(neg_qids)
            else:  # mixed
                half = n_neg // 2
                hard_pool = list(hard_neg_pool.get(fid, []))
                random_pool = list(random_neg_pool.get(fid, []))
                rng.shuffle(hard_pool); rng.shuffle(random_pool)
                neg_qids = hard_pool[:half] + random_pool[:n_neg - half]
                neg_types = ["neg_hard"] * len(hard_pool[:half]) + ["neg_random"] * len(random_pool[:n_neg - half])
            for qid, ntype in zip(neg_qids, neg_types):
                txt = chunk_text.get(qid)
                if not txt: continue
                rows.append({"query": q, "chunk": txt[:3000], "label": 0, "fb_id": fid, "type": ntype})
        return rows

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for strategy in ["random", "hard", "mixed"]:
        sub_dir = OUT_DIR / strategy
        sub_dir.mkdir(parents=True, exist_ok=True)
        train_rows = build_rows(strategy, train_fids)
        val_rows = build_rows(strategy, val_fids)
        with open(sub_dir / "train.jsonl", "w") as f:
            for r in train_rows: f.write(json.dumps(r) + "\n")
        with open(sub_dir / "val.jsonl", "w") as f:
            for r in val_rows: f.write(json.dumps(r) + "\n")
        manifest = {
            "strategy": strategy,
            "pos_neg_ratio": args.pos_neg_ratio,
            "n_train_questions": len(train_fids),
            "n_val_questions": len(val_fids),
            "n_train_examples": len(train_rows),
            "n_val_examples": len(val_rows),
            "n_train_positives": sum(1 for r in train_rows if r["label"] == 1),
            "n_train_negatives": sum(1 for r in train_rows if r["label"] == 0),
            "seed": args.seed,
            "source": "Sprint 7.11 gold_chunks.jsonl + Sprint 7.15 per_question.jsonl",
        }
        with open(sub_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"\n{strategy}: wrote {len(train_rows)} train / {len(val_rows)} val rows")
        print(f"  pos: {manifest['n_train_positives']}  neg: {manifest['n_train_negatives']}")


if __name__ == "__main__":
    main()
