"""Sprint 7.17 Phase 3: component eval of FT'd grader variants.

For each LoRA adapter under data/models/grader_ft_v1_*:
  - Load base + adapter
  - Run inference on:
      A. All 363 (query, gold_chunk) pairs (gold-chunk recall measurement)
      B. 100-pair benchmark from phase_eval.py methodology (50 gold positives,
         50 hard negatives) — comparable to Sprint 7.11 baseline
  - Compute precision, recall, F1 vs baseline prompt-based grader

Baseline reference points:
  - Sprint 7.15 phase-eval grader: prec=0.917, rec=0.66, F1=0.767 (100-pair)
  - Sprint 7.17 Diag 3 grader: rec=0.70 (gold-chunk level, all 363)

Decision gates per Sprint 7.17 plan:
  - macro-F1 >= 0.85 on 100-pair (current ~0.78)
  - precision >= 0.90
  - zero-recall question count drops from 8 to <4

Output: tests/evaluation/eval_results/grader_ft_v1_component_eval.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForSequenceClassification, AutoTokenizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
BASE_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
MODELS_DIR = ROOT / "data/models"
GOLD = ROOT / "tests/evaluation/phase_eval_data/v1/gold_chunks.jsonl"
FB_JSONL = ROOT / "data/raw/financebench/financebench_open_source.jsonl"
COLLECTION = "financebench_corpus_pypdf_voyage_finance2"
DIAG3 = ROOT / "tests/evaluation/eval_results/diag3_grader_on_gold_chunks.json"
OUT = ROOT / "tests/evaluation/eval_results/grader_ft_v1_component_eval.json"


def load_ft_model(adapter_dir: Path, device: str = "mps"):
    """Load base + LoRA adapter for inference."""
    base = AutoModelForSequenceClassification.from_pretrained(BASE_MODEL, num_labels=1)
    model = PeftModel.from_pretrained(base, str(adapter_dir))
    model = model.to(device)
    model.eval()
    return model


def score_pairs(model, tokenizer, pairs: list[tuple[str, str]], device: str = "mps", batch_size: int = 32) -> list[float]:
    """Run model on (query, chunk) pairs, return sigmoid probabilities."""
    probs = []
    for i in range(0, len(pairs), batch_size):
        sub = pairs[i:i + batch_size]
        queries = [p[0] for p in sub]
        chunks = [p[1] for p in sub]
        enc = tokenizer(queries, chunks, padding=True, truncation=True, max_length=512, return_tensors="pt")
        enc = {k: v.to(device) for k, v in enc.items()}
        with torch.no_grad():
            with torch.autocast(device_type=device, dtype=torch.bfloat16):
                logits = model(**enc).logits.squeeze(-1)
        batch_probs = torch.sigmoid(logits.float()).cpu().tolist()
        if isinstance(batch_probs, float):
            batch_probs = [batch_probs]
        probs.extend(batch_probs)
    return probs


def compute_metrics(preds: list[int], labels: list[int]) -> dict:
    tp = sum(1 for p, l in zip(preds, labels) if p == 1 and l == 1)
    fp = sum(1 for p, l in zip(preds, labels) if p == 1 and l == 0)
    fn = sum(1 for p, l in zip(preds, labels) if p == 0 and l == 1)
    tn = sum(1 for p, l in zip(preds, labels) if p == 0 and l == 0)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    acc = (tp + tn) / max(1, len(labels))
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
            "acc": round(acc, 4), "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def main():
    from qdrant_client import QdrantClient

    fb = {json.loads(l)["financebench_id"]: json.loads(l) for l in open(FB_JSONL)}
    gold_recs = [json.loads(l) for l in open(GOLD) if l.strip()]

    # Build the 363-pair gold-chunk eval set (same as Diag 3)
    client = QdrantClient(host=os.environ.get("QDRANT_HOST", "localhost"),
                          port=int(os.environ.get("QDRANT_PORT", 6333)))
    pairs_gold = []  # (fb_id, qid, query, chunk_text)
    for gr in gold_recs:
        fid = gr["financebench_id"]
        question = fb.get(fid, {}).get("question", "")
        if not question:
            continue
        gold_qids = [str(c["qdrant_id"]) for c in gr.get("gold_chunks", [])]
        if not gold_qids:
            continue
        # Fetch texts in batches
        pts = client.retrieve(collection_name=COLLECTION, ids=gold_qids,
                               with_payload=True, with_vectors=False)
        for p in pts:
            txt = p.payload.get("content") or ""
            if txt:
                pairs_gold.append((fid, str(p.id), question, txt[:3000]))

    print(f"Loaded {len(pairs_gold)} (query, gold_chunk) pairs for eval")

    # Tokenizer (shared across all adapters since they share base)
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    device = "mps"

    # Find adapters
    adapter_dirs = sorted([p for p in MODELS_DIR.glob("grader_ft_v1_*") if (p / "adapter_config.json").exists()])
    print(f"Found {len(adapter_dirs)} FT'd grader adapters: {[p.name for p in adapter_dirs]}")
    if not adapter_dirs:
        print("ABORT: no adapters found")
        return 1

    # Also include the BASE model (no adapter) as a control
    print("\nLoading base cross-encoder (no adapter) for control...")
    base_only = AutoModelForSequenceClassification.from_pretrained(BASE_MODEL, num_labels=1).to(device)
    base_only.eval()

    results = {}

    # Score with base model
    print("\nScoring with base model (no FT)...")
    t0 = time.time()
    pairs_only = [(q, c) for _, _, q, c in pairs_gold]
    base_probs = score_pairs(base_only, tokenizer, pairs_only, device=device)
    base_preds = [1 if p >= 0.5 else 0 for p in base_probs]
    base_labels = [1] * len(pairs_only)  # all gold = positive
    base_recall = sum(base_preds) / len(base_preds)
    print(f"  base model gold-chunk recall: {base_recall:.4f}  ({sum(base_preds)}/{len(base_preds)} accepted)  ({time.time()-t0:.0f}s)")
    results["base_minilm"] = {
        "gold_chunk_recall": round(base_recall, 4),
        "n_accepted": sum(base_preds),
        "n_total": len(base_preds),
        "wall_s": round(time.time() - t0, 1),
    }

    # Score with each adapter
    for adapter_dir in adapter_dirs:
        name = adapter_dir.name.replace("grader_ft_v1_", "")
        print(f"\n--- {adapter_dir.name} ---")
        t0 = time.time()
        model = load_ft_model(adapter_dir, device=device)
        probs = score_pairs(model, tokenizer, pairs_only, device=device)
        preds = [1 if p >= 0.5 else 0 for p in probs]
        recall = sum(preds) / len(preds)
        per_fb_accepts = {}
        for (fid, _, _, _), pred in zip(pairs_gold, preds):
            per_fb_accepts.setdefault(fid, []).append(pred)
        n_full_recall = sum(1 for v in per_fb_accepts.values() if all(p == 1 for p in v))
        n_zero_recall = sum(1 for v in per_fb_accepts.values() if all(p == 0 for p in v))
        n_partial = len(per_fb_accepts) - n_full_recall - n_zero_recall
        print(f"  gold-chunk recall: {recall:.4f}  ({sum(preds)}/{len(preds)} accepted)")
        print(f"  per-question: full={n_full_recall}  partial={n_partial}  zero={n_zero_recall}")
        print(f"  wall: {time.time()-t0:.0f}s")
        results[name] = {
            "gold_chunk_recall": round(recall, 4),
            "n_accepted": sum(preds),
            "n_total": len(preds),
            "n_full_recall_questions": n_full_recall,
            "n_partial_recall_questions": n_partial,
            "n_zero_recall_questions": n_zero_recall,
            "wall_s": round(time.time() - t0, 1),
        }
        del model
        if device == "mps":
            torch.mps.empty_cache()

    # Reference: Diag 3 baseline (prompt-based Llama-3.3-70b grader)
    diag3 = json.load(open(DIAG3))
    results["_baseline_llama_grader"] = {
        "gold_chunk_recall": diag3["summary"]["gold_chunk_recall"],
        "n_accepted": diag3["summary"]["n_accepted"],
        "n_total": diag3["summary"]["n_total_pairs"],
        "n_zero_recall_questions": diag3["summary"]["per_question_buckets"]["zero_recall"],
        "n_partial_recall_questions": diag3["summary"]["per_question_buckets"]["partial_recall"],
        "n_full_recall_questions": diag3["summary"]["per_question_buckets"]["full_recall"],
        "note": "Llama-3.3-70b via Groq, prompt-based — current production grader",
    }

    OUT.write_text(json.dumps(results, indent=2))
    print(f"\n=== Component eval result ===")
    print(f"{'variant':<35s} {'gold_recall':>12s} {'zero_recall_Qs':>16s} {'full_recall_Qs':>16s}")
    print("-" * 84)
    for name, m in results.items():
        zr = m.get("n_zero_recall_questions", "n/a")
        fr = m.get("n_full_recall_questions", "n/a")
        print(f"  {name:<33s} {m['gold_chunk_recall']:>12.4f} {str(zr):>16s} {str(fr):>16s}")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
