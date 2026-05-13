"""Phase evaluation harness for FinanceBench-150.

Five metrics that decompose the 47% pass-rate ceiling into pipeline stages,
scored against the Day 1 gold-chunk labels:

  1. Chunk-preservation IoU — does the chunker preserve evidence intact?
  2. Retrieval Recall@k for k in {5,10,20,50} — does retrieval find gold?
  3. Reranker NDCG@8 + Precision@8 — does the reranker order gold first?
  4. Grader precision/recall on 100-pair sample — does the grader judge correctly?
  5. Per-operation p50/p95 latency captured inline + Langfuse best-effort.

Inputs:
  data/raw/financebench/financebench_open_source.jsonl
  tests/evaluation/phase_eval_data/v1/gold_chunks.jsonl
  tests/evaluation/phase_eval_data/v1/_audit.jsonl

Output:
  tests/evaluation/phase_eval_results/financebench_phase_eval_v1.json
  tests/evaluation/phase_eval_results/financebench_phase_eval_v1_per_question.jsonl
"""

import argparse
import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import time
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

for _n in ("httpx", "httpcore", "urllib3", "qdrant_client", "transformers", "sentence_transformers"):
    logging.getLogger(_n).setLevel(logging.WARNING)
for _m in ("src.graph.nodes", "src.services"):
    logging.getLogger(_m).setLevel(logging.WARNING)
warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("phase_eval")

from qdrant_client import QdrantClient

from src.config.settings import settings
from src.services.company_registry import canonical_company_slug
from src.services.embeddings import embed_text
from src.services.vector_store import build_retrieval_filter, hybrid_search

FB_JSONL = Path("data/raw/financebench/financebench_open_source.jsonl")
GOLD_JSONL = Path("tests/evaluation/phase_eval_data/v1/gold_chunks.jsonl")
AUDIT_JSONL = Path("tests/evaluation/phase_eval_data/v1/_audit.jsonl")
OUTPUT_DIR = Path("tests/evaluation/phase_eval_results")
OUTPUT_JSON = OUTPUT_DIR / "financebench_phase_eval_v1.json"
PER_Q_JSONL = OUTPUT_DIR / "financebench_phase_eval_v1_per_question.jsonl"
COLLECTION = "financebench_corpus_pypdf_voyage_finance2"

RECALL_KS = (5, 10, 20, 50)
TOP_K_RETRIEVAL = 50
RERANK_TOP_K = 8
GRADER_SAMPLE_SIZE = 100
GRADER_SAMPLE_SEED = 42

# Sprint 7.13 Day 1 — grader prompt A/B variants. The baseline asks for
# "relevance" but Llama-3.3-70b interprets it as "self-sufficiency," rejecting
# chunks that contain ONE component of a multi-source metric question. The
# three variants test different fix hypotheses (full reframing / few-shot /
# minimal-addition).
_BASELINE_GRADER_PROMPT = """You are a relevance grader for a financial document Q&A system.
Given a user question and a retrieved document chunk, determine if the chunk
is relevant to answering the question.

Question: {query}

Document chunk:
{chunk}

Grade the relevance of this chunk to the question."""

_V1_GRADER_PROMPT = """You are a relevance grader for a multi-stage financial document Q&A system.
Your role is to FILTER OUT chunks that are unrelated to the question. The
downstream generator will combine multiple relevant chunks to compose the
final answer — your role is NOT to verify that a single chunk alone is
sufficient to answer.

Mark a chunk RELEVANT if it contains ANY information that contributes to
answering the question, including:
- one component of a multi-source metric (e.g., the income statement when
  the question needs income statement + cash flow data)
- a table, line item, paragraph, or footnote tied to the question's subject
- supporting context for the company, period, or topic the question asks about

Mark a chunk IRRELEVANT only when it discusses a different topic, entity, or
time period from what the question asks about.

Question: {query}

Document chunk:
{chunk}

Grade the relevance of this chunk to the question."""

_V2_GRADER_PROMPT = """You are a relevance grader for a multi-stage financial document Q&A system.
Your role is to FILTER OUT chunks that are unrelated to the question. The
downstream generator will combine multiple relevant chunks to compose the
final answer — your role is NOT to verify that a single chunk alone is
sufficient to answer.

Mark a chunk RELEVANT if it contains ANY information that contributes to
answering the question, including partial answers and supporting data.

Examples:
- Q: "What is Apple's FY2023 retention ratio (1 minus dividends/net income)?"
  Chunk: Apple's FY2023 income statement (has net income, no dividends section)
  Verdict: RELEVANT — provides net income, one of the two components needed.

- Q: "What is Nike's FY2021 inventory turnover (COGS / avg inventory)?"
  Chunk: Nike's FY2021 income statement (has COGS, no inventory data)
  Verdict: RELEVANT — provides COGS, one of the two components needed.

- Q: "What is Apple's FY2023 revenue?"
  Chunk: Microsoft's FY2023 income statement
  Verdict: IRRELEVANT — wrong entity.

Question: {query}

Document chunk:
{chunk}

Grade the relevance of this chunk to the question."""

_V3_GRADER_PROMPT = """You are a relevance grader for a financial document Q&A system.
Given a user question and a retrieved document chunk, determine if the chunk
is relevant to answering the question.

IMPORTANT: Do NOT mark a chunk as irrelevant just because it alone cannot
fully answer the question. Multi-source questions are common — a chunk that
contains ONE piece of the needed information is RELEVANT. The downstream
generator combines multiple chunks.

Question: {query}

Document chunk:
{chunk}

Grade the relevance of this chunk to the question."""

GRADER_PROMPT_VARIANTS = {
    "baseline": _BASELINE_GRADER_PROMPT,
    "v1": _V1_GRADER_PROMPT,
    "v2": _V2_GRADER_PROMPT,
    "v3": _V3_GRADER_PROMPT,
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PREFIX_RE = re.compile(r"^\s*\[[^\]]*\]\s*")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower()) if text else []


def _strip_prefix(content: str) -> str:
    return _PREFIX_RE.sub("", content or "", count=1)


def _extract_fiscal_year(doc_name: str) -> int | None:
    m = re.search(r"_(\d{4})(?:Q\d+)?_", doc_name)
    return int(m.group(1)) if m else None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def _load_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _git_state() -> dict:
    info = {"sha": None, "branch": None, "dirty": None}
    try:
        info["sha"] = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
        info["branch"] = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
        info["dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True).strip())
    except Exception:
        pass
    return info


def _settings_snapshot() -> dict:
    keys = [
        "EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS",
        "QDRANT_HOST", "QDRANT_PORT", "RETRIEVAL_TOP_K", "RERANKER_TOP_K",
        "GRADER_MODEL", "ENABLE_MULTI_HYDE",
    ]
    snap = {k: getattr(settings, k, None) for k in keys}
    snap["RERANKER_ADAPTER_PATH"] = os.environ.get("RERANKER_ADAPTER_PATH", "")
    snap["LITELLM_URL"] = os.environ.get("LITELLM_URL", "")
    return snap


# ---------------------------------------------------------------------------
# Metric 1 — Chunk-preservation IoU (post-process Day 1 audit log)
# ---------------------------------------------------------------------------

def compute_iou_metric(audit_records: list[dict]) -> dict:
    """Max trigram IoU per (Q, evidence_span) — Chroma-style chunk preservation."""
    by_span: dict[tuple, float] = {}
    for a in audit_records:
        if a.get("phase") != "trigram":
            continue
        key = (a["financebench_id"], a["evidence_idx"])
        iou = a["iou"]
        if key not in by_span or iou > by_span[key]:
            by_span[key] = iou

    if not by_span:
        return {"error": "no trigram audit records found"}

    vals = list(by_span.values())
    histogram = [0] * 11
    for v in vals:
        histogram[min(int(v * 10), 10)] += 1

    return {
        "n_evidence_spans": len(vals),
        "mean_max_iou": round(sum(vals) / len(vals), 4),
        "median_max_iou": round(median(vals), 4),
        "preserved_pct_ge_0.5": round(sum(1 for v in vals if v >= 0.5) / len(vals), 4),
        "preserved_pct_ge_0.7": round(sum(1 for v in vals if v >= 0.7) / len(vals), 4),
        "preserved_pct_ge_0.9": round(sum(1 for v in vals if v >= 0.9) / len(vals), 4),
        "p50": round(_percentile(vals, 0.5), 4),
        "p95": round(_percentile(vals, 0.95), 4),
        "histogram_bins_0.0_to_1.0_by_0.1": histogram,
        "note": "Token-trigram IoU from Day 1 audit log (phase=trigram). For evidence spans where the chunker emitted pipe-tables that misalign with FB's row-prose evidence_text, IoU correctly reads low — that's a preservation finding, not a metric artifact.",
    }


# ---------------------------------------------------------------------------
# Retrieval helpers
# ---------------------------------------------------------------------------

def _build_doc_filter(target_company: str | None, target_fiscal_year: int | None, doc_name: str | None):
    """Mimic production retrieval scoping: company + fiscal year (RBAC permissive).

    We bypass the entity_extractor — perfect entity scoping isolates pure
    retrieval signal. Day 4 (optional) would add entity-extractor accuracy.
    """
    # Match run_financebench.py's "admin" RBAC posture
    return build_retrieval_filter(
        allowed_doc_types=["*"],
        allowed_confidentiality=["*"],
        target_company=target_company,
        target_fiscal_year=target_fiscal_year,
    )


def retrieve_top_n(client: QdrantClient, question: str, target_company: str | None,
                   target_fiscal_year: int | None, top_n: int = TOP_K_RETRIEVAL) -> list[dict]:
    """Hybrid retrieval — voyage dense + BM25 sparse + RRF. Mirrors production."""
    # Override the collection at the settings level for the phase eval
    settings.QDRANT_COLLECTION = COLLECTION
    flt = _build_doc_filter(target_company, target_fiscal_year, None)
    qvec = embed_text(question, input_type="query")
    return hybrid_search(client, question, qvec, rbac_filter=flt, top_k=top_n)


def chunk_logical_id(chunk: dict) -> tuple[str, int]:
    """(source_file, chunk_index) — stable logical key across re-ingestions."""
    md = chunk.get("metadata") or chunk.get("payload") or {}
    return (md.get("source_file") or "", md.get("chunk_index") or -1)


# ---------------------------------------------------------------------------
# Metrics 2 + 3 — Recall@k + Reranker NDCG@8 + Precision@8
# ---------------------------------------------------------------------------

def compute_retrieval_and_rerank(
    gold_records: list[dict],
    fb_lookup: dict[str, dict],
    client: QdrantClient,
    limit: int | None,
    per_q_out,
) -> tuple[dict, dict, dict]:
    """Run retrieval + reranker per Q. Returns (recall_summary, rerank_summary, latency_inline)."""
    from src.services.reranker_service import rerank

    recall_hits = {k: 0 for k in RECALL_KS}
    rerank_ndcgs: list[float] = []
    rerank_precisions: list[float] = []
    retrieval_lat_ms: list[float] = []
    rerank_lat_ms: list[float] = []
    n_evaluated = 0
    n_skipped_no_gold = 0
    retrieval_cache: dict[str, list[dict]] = {}

    iterable = gold_records[:limit] if limit else gold_records
    for i, g in enumerate(iterable):
        fb_id = g["financebench_id"]
        if not g["gold_chunks"]:
            n_skipped_no_gold += 1
            continue
        fb = fb_lookup[fb_id]
        question = fb["question"]
        target_company = canonical_company_slug(fb["company"])
        target_year = _extract_fiscal_year(g["doc_name"])

        gold_ids: set[tuple[str, int]] = {(c["source_file"], c["chunk_index"]) for c in g["gold_chunks"]}

        # Retrieval
        t0 = time.time()
        try:
            top_n = retrieve_top_n(client, question, target_company, target_year, top_n=TOP_K_RETRIEVAL)
        except Exception as e:
            logger.warning(f"retrieval failed for {fb_id}: {type(e).__name__}: {e}")
            top_n = []
        retrieval_lat_ms.append((time.time() - t0) * 1000)
        retrieval_cache[fb_id] = top_n

        top_ids = [chunk_logical_id(c) for c in top_n]
        per_q_recall = {}
        for k in RECALL_KS:
            hit = bool(gold_ids & set(top_ids[:k]))
            if hit:
                recall_hits[k] += 1
            per_q_recall[f"recall_at_{k}"] = hit

        # Reranker (skip if retrieval produced nothing)
        ndcg = 0.0
        precision = 0.0
        reranked_top_k_ids: list[tuple[str, int]] = []
        if top_n:
            t0 = time.time()
            try:
                reranked = rerank(question, top_n, top_k=RERANK_TOP_K)
            except Exception as e:
                logger.warning(f"rerank failed for {fb_id}: {type(e).__name__}: {e}")
                reranked = []
            rerank_lat_ms.append((time.time() - t0) * 1000)
            reranked_top_k_ids = [chunk_logical_id(c) for c in reranked[:RERANK_TOP_K]]
            rels = [1 if cid in gold_ids else 0 for cid in reranked_top_k_ids]
            dcg = sum(rel / math.log2(idx + 2) for idx, rel in enumerate(rels))
            n_gold_capped = min(len(gold_ids), RERANK_TOP_K)
            idcg = sum(1 / math.log2(idx + 2) for idx in range(n_gold_capped))
            ndcg = dcg / idcg if idcg > 0 else 0.0
            precision = sum(rels) / RERANK_TOP_K
        rerank_ndcgs.append(ndcg)
        rerank_precisions.append(precision)

        n_evaluated += 1

        per_q_out.write(json.dumps({
            "financebench_id": fb_id,
            "company": fb["company"],
            "question_type": fb.get("question_type", ""),
            "target_company_slug": target_company,
            "target_fiscal_year": target_year,
            "n_gold_chunks": len(gold_ids),
            "retrieval_lat_ms": round(retrieval_lat_ms[-1], 1),
            "rerank_lat_ms": round(rerank_lat_ms[-1], 1) if rerank_lat_ms and top_n else None,
            **per_q_recall,
            "ndcg_at_8": round(ndcg, 4),
            "precision_at_8": round(precision, 4),
            "retrieved_top_50_ids": [list(t) for t in top_ids],
            "reranked_top_8_ids": [list(t) for t in reranked_top_k_ids],
            "gold_ids": [list(t) for t in gold_ids],
        }) + "\n")
        per_q_out.flush()

        if (i + 1) % 25 == 0:
            logger.info(
                f"  [{i + 1}/{len(iterable)}] last retrieval={retrieval_lat_ms[-1]:.0f}ms "
                f"running Recall@10={recall_hits[10] / n_evaluated:.3f} "
                f"NDCG@8 mean={sum(rerank_ndcgs)/len(rerank_ndcgs):.3f}"
            )

    recall_summary = {
        f"recall_at_{k}": round(recall_hits[k] / n_evaluated, 4) if n_evaluated else 0.0
        for k in RECALL_KS
    }
    recall_summary["n_questions_evaluated"] = n_evaluated
    recall_summary["n_skipped_no_gold"] = n_skipped_no_gold

    rerank_summary = {
        "ndcg_at_8_mean": round(sum(rerank_ndcgs) / len(rerank_ndcgs), 4) if rerank_ndcgs else 0.0,
        "ndcg_at_8_median": round(median(rerank_ndcgs), 4) if rerank_ndcgs else 0.0,
        "ndcg_at_8_p25": round(_percentile(rerank_ndcgs, 0.25), 4) if rerank_ndcgs else 0.0,
        "ndcg_at_8_p75": round(_percentile(rerank_ndcgs, 0.75), 4) if rerank_ndcgs else 0.0,
        "precision_at_8_mean": round(sum(rerank_precisions) / len(rerank_precisions), 4) if rerank_precisions else 0.0,
        "precision_at_8_median": round(median(rerank_precisions), 4) if rerank_precisions else 0.0,
        "n_questions_evaluated": len(rerank_ndcgs),
    }

    latency_inline = {
        "retrieval_ms": {
            "n": len(retrieval_lat_ms),
            "p50": round(_percentile(retrieval_lat_ms, 0.5), 1),
            "p95": round(_percentile(retrieval_lat_ms, 0.95), 1),
            "mean": round(sum(retrieval_lat_ms) / len(retrieval_lat_ms), 1) if retrieval_lat_ms else 0,
        },
        "rerank_ms": {
            "n": len(rerank_lat_ms),
            "p50": round(_percentile(rerank_lat_ms, 0.5), 1),
            "p95": round(_percentile(rerank_lat_ms, 0.95), 1),
            "mean": round(sum(rerank_lat_ms) / len(rerank_lat_ms), 1) if rerank_lat_ms else 0,
        },
    }

    return recall_summary, rerank_summary, latency_inline, retrieval_cache


# ---------------------------------------------------------------------------
# Metric 4 — Grader precision/recall on 100-pair sample
# ---------------------------------------------------------------------------

def _grade_one(query: str, chunk_text: str, prompt_template: str) -> bool | None:
    """Single grader call. Returns True/False or None on hard failure."""
    from langchain_core.messages import HumanMessage

    from src.models.schemas import GradeResult
    from src.services.llm_factory import LLMFactory

    llm = LLMFactory.get_grader_llm()
    structured_llm = llm.with_structured_output(GradeResult)
    prompt = prompt_template.format(query=query, chunk=chunk_text)
    try:
        result = structured_llm.invoke([HumanMessage(content=prompt)])
        return bool(result.relevant)
    except Exception as e:
        logger.warning(f"grader call failed: {type(e).__name__}: {e}")
        return None


def _build_grader_sample(
    gold_records: list[dict],
    fb_lookup: dict[str, dict],
    client: QdrantClient,
    retrieval_cache: dict[str, list[dict]],
    sample_size: int,
    seed: int,
) -> list[dict]:
    """Build N pairs (50 positive from gold, 50 negative from doc-scoped non-retrieved).

    Returns dicts with question + chunk content + gold_label, but no grader_verdict.
    A/B mode reuses this single sample across all prompt variants.
    """
    from qdrant_client.http import models as qmodels

    rng = random.Random(seed)
    pos_target = sample_size // 2
    neg_target = sample_size - pos_target

    eligible = [g for g in gold_records if g["gold_chunks"] and g["financebench_id"] in retrieval_cache]
    rng.shuffle(eligible)

    pairs: list[dict] = []
    pos_count = 0
    neg_count = 0

    for g in eligible:
        if pos_count >= pos_target and neg_count >= neg_target:
            break
        fb_id = g["financebench_id"]
        fb = fb_lookup[fb_id]
        question = fb["question"]
        retrieved = retrieval_cache[fb_id]
        retrieved_ids = {chunk_logical_id(c) for c in retrieved}
        gold_ids = {(c["source_file"], c["chunk_index"]) for c in g["gold_chunks"]}

        if pos_count < pos_target:
            gold_chunk = g["gold_chunks"][0]
            gid = (gold_chunk["source_file"], gold_chunk["chunk_index"])
            content = None
            for c in retrieved:
                if chunk_logical_id(c) == gid:
                    content = c.get("content", "")
                    break
            if content is None:
                try:
                    pts = client.retrieve(collection_name=COLLECTION, ids=[gold_chunk["qdrant_id"]],
                                          with_payload=True, with_vectors=False)
                    content = pts[0].payload.get("content", "") if pts else ""
                except Exception:
                    content = ""
            if content:
                pairs.append({
                    "financebench_id": fb_id,
                    "question": question,
                    "chunk_logical_id": list(gid),
                    "chunk_content": content,
                    "gold_label": "relevant",
                })
                pos_count += 1

        if neg_count < neg_target:
            doc_name = g["doc_name"]
            try:
                flt = qmodels.Filter(must=[
                    qmodels.FieldCondition(key="financebench_doc_name",
                                           match=qmodels.MatchValue(value=doc_name))
                ])
                points, _ = client.scroll(collection_name=COLLECTION, scroll_filter=flt,
                                          limit=200, with_payload=True, with_vectors=False)
                candidates = []
                for p in points:
                    cid = (p.payload.get("source_file") or "", p.payload.get("chunk_index") or -1)
                    if cid in gold_ids or cid in retrieved_ids:
                        continue
                    candidates.append((cid, p.payload.get("content", "")))
                if candidates:
                    cid, content = rng.choice(candidates)
                    if content:
                        pairs.append({
                            "financebench_id": fb_id,
                            "question": question,
                            "chunk_logical_id": list(cid),
                            "chunk_content": content,
                            "gold_label": "irrelevant",
                        })
                        neg_count += 1
            except Exception as e:
                logger.warning(f"neg-sample failed for {fb_id}: {type(e).__name__}: {e}")

    return pairs


def _run_grader_on_pairs(pairs: list[dict], prompt_template: str, variant_name: str) -> dict:
    """Score each pair with the grader; compute prec/rec/F1; return metrics dict."""
    graded: list[dict] = []
    grader_t: list[float] = []

    for i, p in enumerate(pairs):
        t0 = time.time()
        verdict = _grade_one(p["question"], p["chunk_content"], prompt_template)
        grader_t.append((time.time() - t0) * 1000)
        graded.append({
            "financebench_id": p["financebench_id"],
            "chunk_logical_id": p["chunk_logical_id"],
            "gold_label": p["gold_label"],
            "grader_verdict": (
                "relevant" if verdict is True else
                "irrelevant" if verdict is False else
                "error"
            ),
        })
        if (i + 1) % 25 == 0:
            logger.info(f"    [{i + 1}/{len(pairs)}] graded")

    tp = sum(1 for p in graded if p["gold_label"] == "relevant" and p["grader_verdict"] == "relevant")
    fn = sum(1 for p in graded if p["gold_label"] == "relevant" and p["grader_verdict"] == "irrelevant")
    fp = sum(1 for p in graded if p["gold_label"] == "irrelevant" and p["grader_verdict"] == "relevant")
    tn = sum(1 for p in graded if p["gold_label"] == "irrelevant" and p["grader_verdict"] == "irrelevant")
    errs = sum(1 for p in graded if p["grader_verdict"] == "error")

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return {
        "variant": variant_name,
        "n_pairs": len(graded),
        "n_positive": sum(1 for p in graded if p["gold_label"] == "relevant"),
        "n_negative": sum(1 for p in graded if p["gold_label"] == "irrelevant"),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "n_errors": errs,
        "grader_call_ms_p50": round(_percentile(grader_t, 0.5), 1) if grader_t else 0,
        "grader_call_ms_p95": round(_percentile(grader_t, 0.95), 1) if grader_t else 0,
        "grader_model": settings.GRADER_MODEL,
        "pairs": graded,
    }


def compute_grader_metric(
    gold_records: list[dict],
    fb_lookup: dict[str, dict],
    client: QdrantClient,
    retrieval_cache: dict[str, list[dict]],
    sample_size: int,
    seed: int,
    prompt_template: str = _BASELINE_GRADER_PROMPT,
    variant_name: str = "baseline",
) -> dict:
    pairs = _build_grader_sample(gold_records, fb_lookup, client, retrieval_cache, sample_size, seed)
    return _run_grader_on_pairs(pairs, prompt_template, variant_name)


def compute_grader_ab(
    gold_records: list[dict],
    fb_lookup: dict[str, dict],
    client: QdrantClient,
    retrieval_cache: dict[str, list[dict]],
    sample_size: int,
    seed: int,
) -> dict:
    """A/B test all 4 grader-prompt variants on the same 100-pair sample."""
    logger.info(f"  building shared {sample_size}-pair sample...")
    pairs = _build_grader_sample(gold_records, fb_lookup, client, retrieval_cache, sample_size, seed)
    logger.info(f"  sample ready: {sum(1 for p in pairs if p['gold_label']=='relevant')} pos + "
                f"{sum(1 for p in pairs if p['gold_label']=='irrelevant')} neg = {len(pairs)} pairs")
    results: dict[str, dict] = {}
    for variant_name, prompt in GRADER_PROMPT_VARIANTS.items():
        logger.info(f"  variant '{variant_name}': running grader on {len(pairs)} pairs...")
        results[variant_name] = _run_grader_on_pairs(pairs, prompt, variant_name)
        m = results[variant_name]
        logger.info(f"    {variant_name}: prec={m['precision']} rec={m['recall']} f1={m['f1']} "
                    f"(tp={m['tp']} fp={m['fp']} tn={m['tn']} fn={m['fn']})")
    return {
        "sample_size": len(pairs),
        "sample_seed": seed,
        "results_by_variant": results,
    }


# ---------------------------------------------------------------------------
# Metric 5 — Langfuse latency (best-effort)
# ---------------------------------------------------------------------------

def fetch_langfuse_latency(window_hours: int = 48) -> dict:
    """Best-effort: pull per-model latency from Langfuse for the most recent window."""
    try:
        import httpx
    except ImportError:
        return {"status": "skipped", "reason": "httpx not available"}

    now = datetime.now(timezone.utc)
    start = now.timestamp() - window_hours * 3600
    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    auth = (settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY)
    url = f"{settings.LANGFUSE_HOST.rstrip('/')}/api/public/observations"
    rows: list[dict] = []
    try:
        with httpx.Client(timeout=10.0) as client:
            for page in range(1, 11):  # cap at 10 pages = ~1000 rows
                resp = client.get(url, auth=auth, params={
                    "type": "GENERATION", "fromStartTime": start_iso,
                    "toStartTime": end_iso, "limit": 100, "page": page,
                })
                if resp.status_code != 200:
                    return {"status": "skipped", "reason": f"langfuse returned {resp.status_code}"}
                data = resp.json().get("data", [])
                rows.extend(data)
                if len(data) < 100:
                    break
    except Exception as e:
        return {"status": "skipped", "reason": f"{type(e).__name__}: {e}"}

    if not rows:
        return {"status": "no_data", "reason": f"0 traces in last {window_hours}h"}

    by_model: dict[str, list[float]] = defaultdict(list)
    for o in rows:
        model = o.get("model") or "unknown"
        lat = o.get("latency")  # Langfuse returns latency in ms or seconds depending on version
        if isinstance(lat, (int, float)) and lat > 0:
            # Heuristic: Langfuse v3 returns seconds; convert to ms if value looks like seconds
            ms = lat * 1000 if lat < 100 else lat
            by_model[model].append(ms)

    return {
        "status": "ok",
        "n_observations": len(rows),
        "window_hours": window_hours,
        "by_model": {
            m: {
                "n": len(latencies),
                "p50": round(_percentile(latencies, 0.5), 1),
                "p95": round(_percentile(latencies, 0.95), 1),
                "mean": round(sum(latencies) / len(latencies), 1),
            }
            for m, latencies in by_model.items()
        },
    }


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def preflight(client: QdrantClient) -> None:
    info = client.get_collection(COLLECTION)
    vec = embed_text("preflight", input_type="query")
    if len(vec) != 1024:
        raise SystemExit(f"FAIL: embedder dim {len(vec)} ≠ 1024. Set EMBEDDING_MODEL=voyage-finance-2.")
    adapter = os.environ.get("RERANKER_ADAPTER_PATH", "")
    if not adapter or not Path(adapter).exists():
        logger.warning(f"RERANKER_ADAPTER_PATH unset or missing ({adapter!r}); reranker will be vanilla BGE-v2-m3, NOT LoRA-FT")
    logger.info(
        f"preflight ok: collection={COLLECTION} points={info.points_count} "
        f"embedder={settings.EMBEDDING_PROVIDER}/{settings.EMBEDDING_MODEL} dim=1024 "
        f"reranker_adapter={adapter or '<none>'}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metric", choices=["all", "iou", "recall", "rerank", "grader", "latency", "grader_ab"],
                        default="all")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap retrieval+rerank loop at N questions (smoke mode).")
    parser.add_argument("--grader-sample-size", type=int, default=GRADER_SAMPLE_SIZE)
    parser.add_argument("--grader-prompt-variant", choices=list(GRADER_PROMPT_VARIANTS.keys()),
                        default="baseline",
                        help="Single-variant mode for --metric grader. Ignored by --metric grader_ab.")
    parser.add_argument("--output", type=Path, default=OUTPUT_JSON)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("loading inputs...")
    gold_records = _load_jsonl(GOLD_JSONL)
    audit_records = _load_jsonl(AUDIT_JSONL)
    fb_lookup = {r["financebench_id"]: r for r in _load_jsonl(FB_JSONL)}
    logger.info(f"  gold={len(gold_records)} audit={len(audit_records)} fb={len(fb_lookup)}")

    client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=60.0)
    preflight(client)

    out: dict = {
        "manifest": {
            "harness_version": "v1",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "git": _git_state(),
            "settings_snapshot": _settings_snapshot(),
            "collection": COLLECTION,
            "n_gold_records": len(gold_records),
            "n_gold_labeled": sum(1 for g in gold_records if g["gold_chunks"]),
            "limit": args.limit,
        }
    }

    t_start = time.time()

    # Metric 1
    if args.metric in ("all", "iou"):
        logger.info("metric 1 — chunk-preservation IoU...")
        out["iou"] = compute_iou_metric(audit_records)
        logger.info(f"  done: mean_max_iou={out['iou'].get('mean_max_iou')} "
                    f"preserved≥0.5={out['iou'].get('preserved_pct_ge_0.5')}")

    # Metrics 2 + 3 (shared retrieval run)
    retrieval_cache: dict[str, list[dict]] = {}
    if args.metric in ("all", "recall", "rerank", "grader", "grader_ab"):
        logger.info("metrics 2+3 — retrieval Recall@k + rerank NDCG@8...")
        per_q_out = open(PER_Q_JSONL, "w")
        try:
            recall, rerank_summary, latency_inline, retrieval_cache = compute_retrieval_and_rerank(
                gold_records, fb_lookup, client, args.limit, per_q_out,
            )
        finally:
            per_q_out.close()
        out["recall"] = recall
        out["rerank"] = rerank_summary
        out.setdefault("latency", {})["inline_per_operation"] = latency_inline
        logger.info(f"  recall: {recall}")
        logger.info(f"  rerank: NDCG@8 mean={rerank_summary['ndcg_at_8_mean']} "
                    f"P@8 mean={rerank_summary['precision_at_8_mean']}")

    # Metric 4 — single-variant
    if args.metric in ("all", "grader"):
        variant = args.grader_prompt_variant
        prompt = GRADER_PROMPT_VARIANTS[variant]
        logger.info(f"metric 4 — grader prec/rec on {args.grader_sample_size}-pair sample (variant={variant})...")
        if not retrieval_cache:
            logger.warning("  grader requires retrieval cache; rerunning retrieval for sample only")
            per_q_out = open(PER_Q_JSONL, "w")
            try:
                _, _, _, retrieval_cache = compute_retrieval_and_rerank(
                    gold_records, fb_lookup, client, limit=args.grader_sample_size, per_q_out=per_q_out,
                )
            finally:
                per_q_out.close()
        out["grader"] = compute_grader_metric(
            gold_records, fb_lookup, client, retrieval_cache,
            args.grader_sample_size, GRADER_SAMPLE_SEED, prompt, variant,
        )
        g = out["grader"]
        logger.info(f"  done: prec={g['precision']} rec={g['recall']} f1={g['f1']} "
                    f"(tp={g['tp']} fp={g['fp']} tn={g['tn']} fn={g['fn']})")

    # Metric 4 A/B — all 4 variants
    if args.metric == "grader_ab":
        logger.info(f"metric 4 A/B — running 4 grader-prompt variants on {args.grader_sample_size}-pair sample...")
        if not retrieval_cache:
            per_q_out = open(PER_Q_JSONL, "w")
            try:
                _, _, _, retrieval_cache = compute_retrieval_and_rerank(
                    gold_records, fb_lookup, client, limit=args.grader_sample_size, per_q_out=per_q_out,
                )
            finally:
                per_q_out.close()
        out["grader_ab"] = compute_grader_ab(
            gold_records, fb_lookup, client, retrieval_cache,
            args.grader_sample_size, GRADER_SAMPLE_SEED,
        )
        logger.info("")
        logger.info(f"  {'variant':12s} {'prec':>6s} {'rec':>6s} {'f1':>6s}  "
                    f"{'tp':>3s} {'fp':>3s} {'tn':>3s} {'fn':>3s}")
        for vname, m in out["grader_ab"]["results_by_variant"].items():
            logger.info(f"  {vname:12s} {m['precision']:6.3f} {m['recall']:6.3f} {m['f1']:6.3f}  "
                        f"{m['tp']:3d} {m['fp']:3d} {m['tn']:3d} {m['fn']:3d}")

    # Metric 5
    if args.metric in ("all", "latency"):
        logger.info("metric 5 — Langfuse latency (best-effort)...")
        out.setdefault("latency", {})["langfuse_per_model"] = fetch_langfuse_latency()
        logger.info(f"  langfuse: {out['latency']['langfuse_per_model'].get('status')}")

    out["manifest"]["wall_time_s"] = round(time.time() - t_start, 1)

    with open(args.output, "w") as f:
        json.dump(out, f, indent=2)
    logger.info(f"wrote {args.output}  (wall={out['manifest']['wall_time_s']}s)")


if __name__ == "__main__":
    main()
