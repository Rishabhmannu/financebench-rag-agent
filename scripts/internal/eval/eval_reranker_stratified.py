"""Sprint 7.19 Step 1 — stratified reranker eval (sub-component gate before full FB).

Three reranker variants benchmarked on the 147-Q gold-chunk recall set with
NDCG@8 + Recall@8 + Precision@8, stratified four ways:

  1. By Diag 2 bucket (RERANKER_HIT / RETRIEVAL_MISS / RERANKER_MISS)
     — confirms v2 lifts the failing buckets without regressing RERANKER_HIT.
  2. By FinanceBench question_type (domain-relevant / novel-generated /
     metrics-generated) — catches type-specific overfitting.
  3. By gold-page location bin (early / mid / deep) — early=p1-30, mid=p31-100,
     deep=p101+. Catches the deep-footnote bias.
  4. On the 8 structural-failure-mode questions that crashed under BOTH the
     Sprint 7.17 Llama-grader AND the Sprint 7.19 FT v1 reranker. v2 MUST
     NOT regress these — they're the canonical Signal-14/15/17 set.

Variants compared:
  STOCK     — BAAI/bge-reranker-v2-m3 (no adapter)
  FT_V1     — Sprint 7.9 LoRA at data/models/reranker_ft_v1
  FT_V2     — Sprint 7.19 LoRA at data/models/reranker_ft_v2 (this build)

Sub-component gate to pass before launching the full FinanceBench eval:
  - FT_V2 NDCG@8 mean ≥ 0.50 (vs STOCK 0.418)
  - FT_V2 NDCG@8 on RERANKER_MISS bucket ≥ STOCK + 0.10
  - FT_V2 must not regress > 5pp on the 8 structural-failure questions

Output: tests/evaluation/eval_results/eval_reranker_stratified_v2.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import torch
from peft import PeftModel
from qdrant_client.models import (
    FusionQuery, Fusion, Prefetch, SparseVector,
)
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from src.config.settings import settings
from src.services.embeddings import embed_text
from src.services.vector_store import (
    DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME,
    compute_sparse_vectors, get_qdrant_client,
)

GOLD_PATH = ROOT / "tests/evaluation/phase_eval_data/v1/gold_chunks.jsonl"
FB_PATH = ROOT / "data/raw/financebench/financebench_open_source.jsonl"
DIAG2_PATH = ROOT / "tests/evaluation/eval_results/diag2_retrieval_reranker_attribution.json"
COLLECTION = "financebench_corpus_pypdf_voyage_finance2"
BASE_MODEL = "BAAI/bge-reranker-v2-m3"

# The 8 questions that regressed under BOTH Sprint 7.17 Llama-grader AND
# Sprint 7.19 FT v1 reranker — the structural-failure-mode set.
STRUCTURAL_FAILURE_IDS = {
    "financebench_id_00591",  # Adobe FCF conversion
    "financebench_id_00669",  # JnJ gross margin drivers
    "financebench_id_00070",  # AWW working capital
    "financebench_id_01865",  # 3M segment growth excl M&A
    "financebench_id_02987",  # Activision fixed-asset turnover
    "financebench_id_00222",  # AMD quick ratio
    "financebench_id_00685",  # Best Buy gross margin consistency
    "financebench_id_04412",  # financial-approximation calc
}

TOP_K_POOL = 50  # production reranker pool size
TOP_K_OUT = 8    # production RERANKER_TOP_K


def _ndcg_at_k(predictions: list[int], k: int) -> float:
    """predictions[i] = 1 if rank i is gold, 0 otherwise. Idealised at all-1."""
    dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(predictions[:k]))
    n_rel = min(sum(predictions), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(n_rel))
    return dcg / idcg if idcg > 0 else 0.0


def _retrieve_top_k(client, query: str, top_k: int) -> list[dict]:
    qdense = embed_text(query, input_type="query")
    qsparse = compute_sparse_vectors([query])[0]
    res = client.query_points(
        collection_name=COLLECTION,
        prefetch=[
            Prefetch(query=qdense, using=DENSE_VECTOR_NAME, limit=top_k),
            Prefetch(
                query=SparseVector(indices=list(qsparse.indices), values=list(qsparse.values)),
                using=SPARSE_VECTOR_NAME, limit=top_k),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=top_k, with_payload=True,
    )
    return [{"qdrant_id": str(h.id), "content": (h.payload or {}).get("content", ""),
             "page": (h.payload or {}).get("page_number")} for h in res.points
            if (h.payload or {}).get("content")]


def _build_reranker(variant: str, device: str):
    tok = AutoTokenizer.from_pretrained(BASE_MODEL)
    base = AutoModelForSequenceClassification.from_pretrained(BASE_MODEL, num_labels=1)
    if variant == "stock":
        model = base.to(device)
    else:
        adapter_path = str(ROOT / "data/models" / variant)
        if not (Path(adapter_path) / "adapter_config.json").exists():
            raise FileNotFoundError(f"Adapter not found at {adapter_path}")
        model = PeftModel.from_pretrained(base, adapter_path).to(device)
    model.eval()
    return tok, model


def _rerank_scores(tok, model, query: str, chunks: list[str], device: str) -> list[float]:
    """Score every (query, chunk) pair. Returns parallel list of sigmoid scores."""
    if not chunks:
        return []
    enc = tok([query] * len(chunks), [c[:3000] for c in chunks],
              max_length=512, truncation=True, padding=True, return_tensors="pt").to(device)
    with torch.no_grad():
        logits = model(**enc).logits.squeeze(-1)
        if logits.dim() == 0:
            logits = logits.unsqueeze(0)
        return torch.sigmoid(logits).cpu().tolist()


def _page_bin(page: int | None) -> str:
    if page is None:
        return "unknown"
    if page <= 30:
        return "early"
    if page <= 100:
        return "mid"
    return "deep"


def _aggregate(records: list[dict], key_fn) -> dict[str, dict]:
    """Group records and compute mean metrics per group."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        for k in (key_fn(r) if isinstance(key_fn(r), list) else [key_fn(r)]):
            groups[k].append(r)
    out = {}
    for k, rs in groups.items():
        out[k] = {
            "n": len(rs),
            "ndcg_at_8": round(sum(r["ndcg_at_8"] for r in rs) / len(rs), 4),
            "recall_at_8": round(sum(r["recall_at_8"] for r in rs) / len(rs), 4),
            "precision_at_8": round(sum(r["precision_at_8"] for r in rs) / len(rs), 4),
        }
    return out


def evaluate_variant(variant: str, gold_records: list[dict], fb_records: dict,
                     diag2_by_fb: dict, device: str) -> dict:
    print(f"\n--- Variant: {variant} ---", flush=True)
    tok, model = _build_reranker(variant, device)
    client = get_qdrant_client()
    per_record = []

    for j, g in enumerate(gold_records):
        fb_id = g["financebench_id"]
        fb_rec = fb_records.get(fb_id, {})
        q = fb_rec.get("question", "")
        if not q or not g.get("gold_chunks"):
            continue
        gold_qids = {str(c["qdrant_id"]) for c in g["gold_chunks"]}

        # Production retrieval (top-50 hybrid)
        pool = _retrieve_top_k(client, q, TOP_K_POOL)
        if not pool:
            continue
        scores = _rerank_scores(tok, model, q, [p["content"] for p in pool], device)
        ranked = sorted(zip(pool, scores), key=lambda x: -x[1])
        top8 = [p for p, _ in ranked[:TOP_K_OUT]]
        relevance = [1 if p["qdrant_id"] in gold_qids else 0 for p in top8]
        n_gold_in_top8 = sum(relevance)
        n_gold_total = len(gold_qids)

        # Per-question gold page bin (use first gold chunk's page)
        first_gold_page = g["gold_chunks"][0].get("page_number") if g["gold_chunks"] else None

        per_record.append({
            "fb_id": fb_id,
            "question_type": fb_rec.get("question_type", "unknown"),
            "diag2_bucket": diag2_by_fb.get(fb_id, {}).get("bucket", "?"),
            "diag2_baseline_pass": diag2_by_fb.get(fb_id, {}).get("pass"),
            "page_bin": _page_bin(first_gold_page),
            "n_gold_total": n_gold_total,
            "n_gold_in_top8": n_gold_in_top8,
            "ndcg_at_8": round(_ndcg_at_k(relevance, TOP_K_OUT), 4),
            "recall_at_8": round(n_gold_in_top8 / max(1, n_gold_total), 4),
            "precision_at_8": round(n_gold_in_top8 / TOP_K_OUT, 4),
        })
        if (j + 1) % 25 == 0:
            print(f"    [{j+1}/{len(gold_records)}]", flush=True)

    # Aggregates
    n = len(per_record)
    agg_global = {
        "n": n,
        "ndcg_at_8_mean": round(sum(r["ndcg_at_8"] for r in per_record) / max(1, n), 4),
        "ndcg_at_8_median": round(sorted([r["ndcg_at_8"] for r in per_record])[n // 2], 4) if n else 0,
        "recall_at_8_mean": round(sum(r["recall_at_8"] for r in per_record) / max(1, n), 4),
        "precision_at_8_mean": round(sum(r["precision_at_8"] for r in per_record) / max(1, n), 4),
        "n_zero_recall": sum(1 for r in per_record if r["n_gold_in_top8"] == 0),
    }
    by_bucket = _aggregate(per_record, lambda r: r["diag2_bucket"])
    by_type = _aggregate(per_record, lambda r: r["question_type"])
    by_page_bin = _aggregate(per_record, lambda r: r["page_bin"])
    struct_records = [r for r in per_record if r["fb_id"] in STRUCTURAL_FAILURE_IDS]
    on_structural = _aggregate(struct_records, lambda r: "structural_failure_8")

    print(f"  global NDCG@8={agg_global['ndcg_at_8_mean']}  recall@8={agg_global['recall_at_8_mean']}  "
          f"zero_recall={agg_global['n_zero_recall']}/{n}", flush=True)
    print(f"  by bucket: {by_bucket}", flush=True)
    print(f"  structural-failure-8: {on_structural}", flush=True)

    return {
        "variant": variant,
        "n_records": n,
        "global": agg_global,
        "by_diag2_bucket": by_bucket,
        "by_question_type": by_type,
        "by_page_bin": by_page_bin,
        "on_structural_failure_8": on_structural,
        "per_record_sample": per_record[:5],
    }


def main():
    parser = argparse.ArgumentParser(description="Sprint 7.19 Step 1 stratified reranker eval")
    parser.add_argument("--variants", nargs="+",
                        default=["stock", "reranker_ft_v1", "reranker_ft_v2"],
                        help="Subset of variants to evaluate")
    parser.add_argument("--output", default="tests/evaluation/eval_results/eval_reranker_stratified_v2.json")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if settings.EMBEDDING_PROVIDER != "voyage":
        print("ABORT: EMBEDDING_PROVIDER must be voyage")
        sys.exit(1)

    gold_records = [json.loads(l) for l in GOLD_PATH.open() if l.strip()]
    if args.limit:
        gold_records = gold_records[: args.limit]
    fb_records = {json.loads(l)["financebench_id"]: json.loads(l) for l in FB_PATH.open()}
    diag2 = json.load(DIAG2_PATH.open())
    diag2_by_fb = {r["fb_id"]: r for r in diag2["per_record"]}

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"Device: {device}, gold records: {len(gold_records)}, variants: {args.variants}")

    results = {}
    for variant in args.variants:
        try:
            results[variant] = evaluate_variant(variant, gold_records, fb_records, diag2_by_fb, device)
        except FileNotFoundError as e:
            print(f"  SKIP {variant}: {e}")
            results[variant] = {"error": str(e)}

    out_path = ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {out_path}\n")

    # Sub-component gate check
    print("=" * 90)
    print("Sub-component gate vs STOCK baseline")
    print("=" * 90)
    if "stock" in results and "reranker_ft_v2" in results and "error" not in results["reranker_ft_v2"]:
        stock = results["stock"]
        v2 = results["reranker_ft_v2"]
        d_ndcg = v2["global"]["ndcg_at_8_mean"] - stock["global"]["ndcg_at_8_mean"]
        print(f"  ΔNDCG@8 (v2 - stock): {d_ndcg:+.4f}  (must be ≥+0.082, target 0.50 from stock 0.418)")
        for bucket in ("RETRIEVAL_MISS", "RERANKER_MISS", "RERANKER_HIT"):
            sb = stock["by_diag2_bucket"].get(bucket, {})
            vb = v2["by_diag2_bucket"].get(bucket, {})
            if sb and vb:
                dn = vb.get("ndcg_at_8", 0) - sb.get("ndcg_at_8", 0)
                print(f"  ΔNDCG@8 [{bucket}]: {dn:+.4f}")
        sf_stock = stock["on_structural_failure_8"].get("structural_failure_8", {})
        sf_v2 = v2["on_structural_failure_8"].get("structural_failure_8", {})
        if sf_stock and sf_v2:
            dn = sf_v2.get("ndcg_at_8", 0) - sf_stock.get("ndcg_at_8", 0)
            print(f"  ΔNDCG@8 [structural_failure_8] (must not regress > -5pp): {dn:+.4f}")


if __name__ == "__main__":
    main()
