"""Sprint 7.17 Phase 3 (refined): 4-way grader model comparison.

Compares 4 grader backends on the same labeled benchmarks:
  CONTROL   — OpenAI gpt-4o-mini (current production grader after USE_GROQ_FAST_PATH=false)
  EXP_A     — Anthropic Claude Haiku 4.5 (per 2026 best practice for binary classification)
  EXP_B     — Groq Llama-3.3-70b-versatile (uses GROQ_API_KEY_GRADER_TEST to isolate from
              the production runtime's USE_GROQ_FAST_PATH=false setting; flipping the flag
              would also affect the router, which we want to leave alone)
  EXP_C     — BAAI/bge-reranker-v2-m3 with sigmoid+threshold (free, already in pipeline)

Each backend exposes grade(query, chunk) -> (relevant: bool, score: float).

Benchmarks:
  1. 363-pair gold-chunk recall set (same as Sprint 7.17 Diag 3):
       all (question, gold_chunk) pairs from phase_eval_data/v1/gold_chunks.jsonl
       Metric: gold-chunk recall + per-question full/partial/zero buckets
  2. 100-pair balanced sample (50 random gold positives + 50 same-doc non-retrieved
     hard negatives) — built once on the fly:
       Metrics: precision, recall, F1, accuracy

Also tracks: wall time per call (latency), pricing-based cost-per-150Q-eval estimate.

Output: tests/evaluation/eval_results/grader_models_compare.json
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# --- env bootstrap (no API key leakage through shell) -----------------------
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# --- imports that need API keys in env --------------------------------------
import torch
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_groq import ChatGroq
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from src.models.schemas import GradeResult
from src.config.prompts import GRADER_PROMPT

GOLD = ROOT / "tests/evaluation/phase_eval_data/v1/gold_chunks.jsonl"
PER_Q = ROOT / "tests/evaluation/phase_eval_results/financebench_phase_eval_v1_per_question.jsonl"
FB_JSONL = ROOT / "data/raw/financebench/financebench_open_source.jsonl"
COLLECTION = "financebench_corpus_pypdf_voyage_finance2"
OUT = ROOT / "tests/evaluation/eval_results/grader_models_compare.json"

# Pricing (per million tokens, USD), used for cost estimates
PRICE = {
    "gpt-4o-mini":            {"in": 0.15, "out": 0.60},
    "claude-haiku-4-5":       {"in": 1.00, "out": 5.00},
    "groq-llama-3.3-70b":     {"in": 0.59, "out": 0.79},  # paid tier; free tier $0
    "bge-reranker-v2-m3":     {"in": 0.0,  "out": 0.0},   # self-hosted, no API cost
}


# ---------------------------------------------------------------------------
# Backend implementations — each exposes grade(query, chunk) -> (bool, float)
# ---------------------------------------------------------------------------

class GraderBackend:
    name: str
    pricing_key: str

    def grade(self, query: str, chunk: str) -> tuple[bool, float]:
        raise NotImplementedError


def _llm_grade_with_prompt(llm, query, chunk) -> tuple[bool, float, str | None]:
    """Shared path for LLM-based graders using the canonical GRADER_PROMPT.

    Returns (relevant_bool, score, error_msg_or_None). On exception, returns
    (False, 0.0, error_msg) so the caller can count errors separately from
    actual rejections (Bug 3 fix from prior run).
    """
    structured = llm.with_structured_output(GradeResult)
    prompt = GRADER_PROMPT.format(query=query, chunk=chunk[:3000])
    try:
        r = structured.invoke([HumanMessage(content=prompt)])
        return bool(r.relevant), 1.0 if r.relevant else 0.0, None
    except Exception as e:
        return False, 0.0, f"{type(e).__name__}: {str(e)[:160]}"


class GPT4oMiniGrader(GraderBackend):
    name = "gpt-4o-mini (control)"
    pricing_key = "gpt-4o-mini"
    def __init__(self):
        # Bug 1 fix: max_tokens=512 (consistent across all LLM backends so structured
        # output isn't truncated before the schema's `reason` field completes).
        # Bug 5 fix: seed=42 matches production LLMFactory._openai() determinism.
        self.llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.0, seed=42,
                              max_tokens=512,
                              api_key=os.environ["OPENAI_API_KEY"])
    def grade(self, query, chunk):
        return _llm_grade_with_prompt(self.llm, query, chunk)


class HaikuGrader(GraderBackend):
    name = "claude-haiku-4-5 (experiment A)"
    pricing_key = "claude-haiku-4-5"
    def __init__(self):
        # Bug 1 fix: max_tokens raised from 256 → 512 to avoid truncating the
        # structured output's `reason` field, which was causing silent parse
        # failures → false-negative rejections.
        self.llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0.0,
                                  anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
                                  max_tokens=512)
    def grade(self, query, chunk):
        return _llm_grade_with_prompt(self.llm, query, chunk)


class GroqLlamaGrader(GraderBackend):
    """Groq Llama-3.3-70b with explicit pacing to stay under the 18,000 tokens/min
    free-tier rate limit (Bug 2 fix). Each request ≈ 1530 input + 30 output tokens,
    so capping at 10 req/min (= 6s sleep) gives ~15,600 tokens/min — safely under
    the 18K limit with margin for token-count variance per chunk.
    """
    name = "groq llama-3.3-70b (experiment B)"
    pricing_key = "groq-llama-3.3-70b"
    MIN_SECONDS_BETWEEN_CALLS = 6.0  # 18K tokens/min limit → cap to 10 req/min
    def __init__(self):
        key = os.environ.get("GROQ_API_KEY_GRADER_TEST")
        if not key:
            raise RuntimeError("GROQ_API_KEY_GRADER_TEST not set in env — required to isolate "
                               "this experiment from the production USE_GROQ_FAST_PATH setting")
        self.llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0.0,
                            max_tokens=512, api_key=key)
        self._last_call_time = 0.0
    def grade(self, query, chunk):
        # Token-rate pacing: enforce minimum interval between calls
        elapsed = time.time() - self._last_call_time
        if elapsed < self.MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(self.MIN_SECONDS_BETWEEN_CALLS - elapsed)
        result = _llm_grade_with_prompt(self.llm, query, chunk)
        self._last_call_time = time.time()
        return result


class BGEThresholdGrader(GraderBackend):
    """Use BGE-reranker-v2-m3 cross-encoder + production LoRA-FT'd adapter as a
    binary grader by thresholding the sigmoid of its raw relevance score. This
    matches the actual production reranker (Bug 4 fix — prior eval used the
    base model without the Sprint 7.9 LoRA adapter that ships in production).
    """
    name = "bge-reranker-v2-m3 + LoRA-FT v1 (experiment C, free)"
    pricing_key = "bge-reranker-v2-m3"
    def __init__(self, threshold: float = 0.5, device: str = "mps"):
        self.threshold = threshold
        self.device = device
        base = "BAAI/bge-reranker-v2-m3"
        self.tok = AutoTokenizer.from_pretrained(base)
        base_model = AutoModelForSequenceClassification.from_pretrained(base, num_labels=1)
        # Bug 4 fix: load the production LoRA adapter that ships at runtime
        from peft import PeftModel
        adapter_path = ROOT / "data/models/reranker_ft_v1"
        if not (adapter_path / "adapter_config.json").exists():
            raise RuntimeError(f"production LoRA adapter not found at {adapter_path}")
        self.model = PeftModel.from_pretrained(base_model, str(adapter_path)).to(device)
        self.model.eval()

    def grade(self, query, chunk):
        enc = self.tok(query, chunk[:3000], max_length=512, truncation=True,
                       padding=True, return_tensors="pt").to(self.device)
        with torch.no_grad():
            logits = self.model(**enc).logits.squeeze(-1)
            prob = torch.sigmoid(logits).item()
        return prob >= self.threshold, float(prob), None


# ---------------------------------------------------------------------------
# Benchmark builders
# ---------------------------------------------------------------------------

def load_gold_chunk_pairs(client):
    """Build the 363-pair gold-chunk recall benchmark."""
    fb = {json.loads(l)["financebench_id"]: json.loads(l) for l in open(FB_JSONL)}
    gold_recs = [json.loads(l) for l in open(GOLD) if l.strip()]

    # Collect all gold qdrant_ids
    all_gold_qids = []
    fb_gold = {}
    for gr in gold_recs:
        fid = gr["financebench_id"]
        q = fb.get(fid, {}).get("question", "")
        if not q:
            continue
        qids = [str(c["qdrant_id"]) for c in gr.get("gold_chunks", [])]
        if not qids:
            continue
        fb_gold[fid] = {"question": q, "qids": qids, "doc_name": gr["doc_name"] + ".pdf"}
        all_gold_qids.extend(qids)

    # Fetch chunk texts
    chunk_text = {}
    for i in range(0, len(all_gold_qids), 100):
        sub = all_gold_qids[i:i + 100]
        pts = client.retrieve(collection_name=COLLECTION, ids=sub, with_payload=True, with_vectors=False)
        for p in pts:
            chunk_text[str(p.id)] = p.payload.get("content", "")

    pairs = []
    for fid, info in fb_gold.items():
        for qid in info["qids"]:
            txt = chunk_text.get(qid)
            if txt:
                pairs.append({"fb_id": fid, "qid": qid, "query": info["question"],
                              "chunk": txt[:3000], "doc_name": info["doc_name"]})
    return pairs, fb_gold


def build_balanced_sample(pairs, fb_gold, client, n_per_class=50, seed=42):
    """Build 50 random positives + 50 same-doc non-retrieved negatives."""
    rng = random.Random(seed)
    positives = list(pairs)
    rng.shuffle(positives)
    pos_sample = positives[:n_per_class]

    # Hard negatives: same-doc as the positive, not in any gold list for that fb_id
    per_q = {json.loads(l)["financebench_id"]: json.loads(l) for l in open(PER_Q) if l.strip()}
    negatives = []
    seen_negs = set()
    for p in pos_sample:
        fid = p["fb_id"]
        doc = p["doc_name"]
        # gold qids for this fid
        gold_set = set(fb_gold[fid]["qids"])
        # Find same-doc chunks NOT in gold
        flt = Filter(must=[FieldCondition(key="source_file", match=MatchValue(value=doc))])
        recs, _ = client.scroll(collection_name=COLLECTION, scroll_filter=flt, limit=60,
                                 with_payload=True, with_vectors=False)
        rng.shuffle(recs)
        for r in recs:
            qid = str(r.id)
            if qid in gold_set or qid in seen_negs:
                continue
            txt = r.payload.get("content", "")
            if not txt:
                continue
            negatives.append({"fb_id": fid, "qid": qid, "query": p["query"],
                              "chunk": txt[:3000], "doc_name": doc})
            seen_negs.add(qid)
            break  # one negative per positive Q

    sample = []
    for p in pos_sample:
        sample.append({**p, "label": 1})
    for n in negatives:
        sample.append({**n, "label": 0})
    rng.shuffle(sample)
    return sample


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def compute_balanced_metrics(results):
    """For 100-pair balanced sample: precision, recall, F1, accuracy."""
    tp = sum(1 for r in results if r["pred"] and r["label"] == 1)
    fp = sum(1 for r in results if r["pred"] and r["label"] == 0)
    fn = sum(1 for r in results if not r["pred"] and r["label"] == 1)
    tn = sum(1 for r in results if not r["pred"] and r["label"] == 0)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    acc = (tp + tn) / max(1, len(results))
    return {"precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4), "accuracy": round(acc, 4),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def compute_gold_chunk_metrics(results):
    """For 363-gold-chunk set: recall + per-question buckets."""
    accepted = sum(1 for r in results if r["pred"])
    per_fb = {}
    for r in results:
        per_fb.setdefault(r["fb_id"], []).append(r["pred"])
    full = sum(1 for v in per_fb.values() if all(v))
    zero = sum(1 for v in per_fb.values() if not any(v))
    partial = len(per_fb) - full - zero
    return {"gold_chunk_recall": round(accepted / max(1, len(results)), 4),
            "n_accepted": accepted, "n_total": len(results),
            "n_full_recall_qs": full, "n_partial_recall_qs": partial, "n_zero_recall_qs": zero}


def cost_estimate(pricing_key, n_calls, avg_in_tokens=1530, avg_out_tokens=30):
    p = PRICE[pricing_key]
    return round((n_calls * avg_in_tokens / 1e6) * p["in"] + (n_calls * avg_out_tokens / 1e6) * p["out"], 4)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def evaluate_backend(backend, gold_pairs, balanced_sample):
    """Run a backend on both benchmarks, return metrics + latencies + cost estimate.

    Tracks error count per benchmark (Bug 3 fix) so we can distinguish actual
    'irrelevant' verdicts from silent-fail exceptions.
    """
    name = backend.name
    print(f"\n--- {name} ---", flush=True)

    # 100-pair balanced
    print(f"  running on 100-pair balanced sample ({len(balanced_sample)} items)...", flush=True)
    t0 = time.time()
    bal_results = []
    bal_errors = []
    for i, item in enumerate(balanced_sample):
        out = backend.grade(item["query"], item["chunk"])
        if len(out) == 3:
            pred, score, err = out
        else:
            pred, score = out
            err = None
        bal_results.append({**item, "pred": pred, "score": score, "error": err})
        if err:
            bal_errors.append({"fb_id": item.get("fb_id"), "err": err})
        if (i + 1) % 25 == 0:
            print(f"    [{i+1}/{len(balanced_sample)}]  errors so far: {len(bal_errors)}", flush=True)
    bal_elapsed = time.time() - t0
    bal_metrics = compute_balanced_metrics(bal_results)
    bal_metrics["wall_s"] = round(bal_elapsed, 1)
    bal_metrics["mean_latency_ms"] = round(bal_elapsed * 1000 / len(balanced_sample), 1)
    bal_metrics["n_errors"] = len(bal_errors)
    print(f"  balanced: prec={bal_metrics['precision']} rec={bal_metrics['recall']} "
          f"F1={bal_metrics['f1']}  acc={bal_metrics['accuracy']}  "
          f"errors={bal_metrics['n_errors']}/{len(balanced_sample)}  "
          f"({bal_metrics['mean_latency_ms']:.0f}ms/call)", flush=True)

    # 363-gold-chunk
    print(f"  running on 363-gold-chunk recall set...", flush=True)
    t1 = time.time()
    gold_results = []
    gold_errors = []
    for i, item in enumerate(gold_pairs):
        out = backend.grade(item["query"], item["chunk"])
        if len(out) == 3:
            pred, score, err = out
        else:
            pred, score = out
            err = None
        gold_results.append({**item, "pred": pred, "score": score, "error": err})
        if err:
            gold_errors.append({"fb_id": item.get("fb_id"), "err": err})
        if (i + 1) % 100 == 0:
            print(f"    [{i+1}/{len(gold_pairs)}]  errors so far: {len(gold_errors)}", flush=True)
    gold_elapsed = time.time() - t1
    gold_metrics = compute_gold_chunk_metrics(gold_results)
    gold_metrics["wall_s"] = round(gold_elapsed, 1)
    gold_metrics["n_errors"] = len(gold_errors)
    print(f"  gold-chunk recall: {gold_metrics['gold_chunk_recall']} "
          f"full={gold_metrics['n_full_recall_qs']} "
          f"partial={gold_metrics['n_partial_recall_qs']} "
          f"zero={gold_metrics['n_zero_recall_qs']}  "
          f"errors={gold_metrics['n_errors']}/{len(gold_pairs)}", flush=True)

    # Cost estimate at 7500-call eval scale
    cost_per_eval = cost_estimate(backend.pricing_key, n_calls=7500)
    print(f"  est cost per full-150Q eval: ${cost_per_eval}", flush=True)

    return {
        "name": name,
        "pricing_key": backend.pricing_key,
        "balanced_100": bal_metrics,
        "gold_363": gold_metrics,
        "est_cost_per_eval_usd": cost_per_eval,
        "balanced_error_sample": bal_errors[:5],
        "gold_error_sample": gold_errors[:5],
    }


def main():
    client = QdrantClient(host=os.environ.get("QDRANT_HOST", "localhost"),
                          port=int(os.environ.get("QDRANT_PORT", 6333)))

    print("Loading benchmarks...", flush=True)
    gold_pairs, fb_gold = load_gold_chunk_pairs(client)
    print(f"  363-gold-chunk set: {len(gold_pairs)} pairs across {len(fb_gold)} questions", flush=True)
    balanced = build_balanced_sample(gold_pairs, fb_gold, client, n_per_class=50)
    n_pos = sum(1 for x in balanced if x["label"] == 1)
    n_neg = sum(1 for x in balanced if x["label"] == 0)
    print(f"  balanced 100-pair sample: {n_pos} pos + {n_neg} neg", flush=True)

    # Order: BGE (local, fast, free) → gpt-4o-mini (control) → Haiku → Groq LAST
    # (Groq paced at 6s/call to stay under 18K tokens/min free-tier limit; ~37 min on
    # 463 total calls. Run last so if free-tier daily quota exhausts, other backends
    # already have results.)
    backends = [
        BGEThresholdGrader(threshold=0.5),
        GPT4oMiniGrader(),
        HaikuGrader(),
        GroqLlamaGrader(),
    ]

    all_results = {}
    for bk in backends:
        all_results[bk.name] = evaluate_backend(bk, gold_pairs, balanced)

    # Print summary table
    print("\n" + "=" * 110, flush=True)
    print("=== GRADER MODEL COMPARISON SUMMARY ===", flush=True)
    print("=" * 110, flush=True)
    hdr = (f"{'backend':<48s} {'F1':>6s} {'prec':>6s} {'rec':>6s} {'gold_rec':>9s} "
           f"{'zero_Qs':>8s} {'b_err':>6s} {'g_err':>6s} {'ms/call':>9s} {'$/eval':>8s}")
    print(hdr, flush=True)
    print("-" * len(hdr), flush=True)
    for name, r in all_results.items():
        b, g = r["balanced_100"], r["gold_363"]
        print(f"  {name:<46s} {b['f1']:>6.3f} {b['precision']:>6.3f} {b['recall']:>6.3f} "
              f"{g['gold_chunk_recall']:>9.3f} {g['n_zero_recall_qs']:>8d} "
              f"{b.get('n_errors', 0):>6d} {g.get('n_errors', 0):>6d} "
              f"{b['mean_latency_ms']:>8.0f}m {r['est_cost_per_eval_usd']:>7.2f}", flush=True)

    OUT.write_text(json.dumps(all_results, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
