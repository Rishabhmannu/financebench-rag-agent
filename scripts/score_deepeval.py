"""Score a FinanceBench pipeline cache with DeepEval's four RAG metrics.

Replaces Patronus when its credit cap is exhausted. DeepEval is a self-hosted
LLM-as-judge framework (Confident AI), so the only ongoing cost is OpenAI judge
tokens — same cost model as RAGAS. Using the same `gpt-4o-mini` judge as RAGAS
keeps cost and reproducibility consistent across both frameworks; the prompt
templates differ enough to give us a real second-judge cross-check rather than
a redundant signal.

Metrics computed (all on a 0-1 scale, higher is better):

  - FaithfulnessMetric        — does the answer factually align with the
                                 retrieved context? (analog of RAGAS faithfulness)
  - AnswerRelevancyMetric     — does the answer address the input question?
                                 (analog of RAGAS answer_relevancy)
  - ContextualPrecisionMetric — are the relevant chunks ranked at the top of
                                 the retrieved context list? (analog of RAGAS
                                 context_precision; requires expected_output)
  - ContextualRecallMetric    — does the retrieved context cover everything
                                 the expected answer references? (analog of
                                 RAGAS context_recall; requires expected_output)

Reads a pipeline cache (questions/answers/contexts produced by
`tests/evaluation/run_financebench.py`) and the FinanceBench ground-truth JSONL,
runs the four metrics per sample with bounded concurrency, then writes per-
sample + aggregate scores in the same shape `score_patronus.py` produces so
downstream comparison tooling can treat them interchangeably.

Usage:
    python scripts/score_deepeval.py \\
        --cache tests/evaluation/eval_results/financebench_pypdf_clean.pipeline.json \\
        --output tests/evaluation/eval_results/financebench_pypdf_clean.deepeval.json

    # smoke-test on 3 samples first to verify wiring
    python scripts/score_deepeval.py --cache <path> --output <path> --limit 3

    # resume after an interruption — only re-scores samples whose prior result
    # had any error or was missing
    python scripts/score_deepeval.py --cache <path> --output <path> --resume
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path

from tqdm.asyncio import tqdm_asyncio

from src.config.settings import settings

# --- Mirror OPENAI key into env so DeepEval (which reads os.environ) sees it ---
if settings.OPENAI_API_KEY and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY

# Suppress DeepEval's banner / posthog / sentry noise so tqdm stays clean.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("DEEPEVAL_DISABLE_PROGRESS_BAR", "YES")
os.environ.setdefault("ERROR_REPORTING", "NO")

# Silence noisy loggers that would otherwise corrupt the tqdm bar.
for _n in ["httpx", "httpcore", "openai", "deepeval", "urllib3", "sentry_sdk"]:
    logging.getLogger(_n).setLevel(logging.WARNING)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FB_QA_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")
DEFAULT_JUDGE_MODEL = "gpt-4o-mini"
# Per-sample concurrency. Each sample triggers ~12-20 judge LLM calls (4 metrics
# × 3-5 internal calls per metric). 6 concurrent samples ≈ 90-120 in-flight reqs,
# comfortably under OpenAI tier-1 limits (10k RPM). Bumped from 4 → 6 after the
# pypdf preliminary run showed scoring took 14 min for 150 samples — concurrency
# 6 cuts that to ~10 min with no observed throttling.
DEFAULT_CONCURRENCY = 6
# Hard timeout per (sample × metric) so a stuck judge call doesn't hang the run.
# Bumped 180s → 300s → 450s across the dual-pipeline campaign. DeepEval has its
# own internal 88s per-attempt timeout with one retry baked in, so 450s outer
# gives enough headroom for the most pathological multi-table samples (which
# trigger long claim-extraction chains) without making the tqdm bar visibly
# stall. The 10 timeouts seen in the preliminary pypdf run were on samples
# with > 5 long table chunks; 450s should cover the upper tail.
PER_METRIC_TIMEOUT_SECONDS = 450.0


def _build_metrics(model: str):
    """Construct the four RAG metrics fresh for each sample.

    DeepEval metric instances are stateful (they hold the latest .score / .reason
    on the instance), so to safely run metrics in parallel across samples we
    instantiate per-sample rather than reusing one shared metric object.
    """
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        FaithfulnessMetric,
    )

    common_kwargs = dict(model=model, async_mode=True, include_reason=True, verbose_mode=False)
    return {
        "faithfulness": FaithfulnessMetric(**common_kwargs),
        "answer_relevancy": AnswerRelevancyMetric(**common_kwargs),
        "contextual_precision": ContextualPrecisionMetric(**common_kwargs),
        "contextual_recall": ContextualRecallMetric(**common_kwargs),
    }


async def _measure_one_metric(metric, test_case, name: str) -> dict:
    """Run a single metric with timeout. Returns {score, reason} or {error, detail}."""
    try:
        await asyncio.wait_for(metric.a_measure(test_case), timeout=PER_METRIC_TIMEOUT_SECONDS)
        # DeepEval scores can be None if the LLM judge couldn't produce a verdict
        # (e.g. empty retrieval_context for ContextualRecall). Surface as null.
        score = metric.score
        return {
            "score": float(score) if score is not None else None,
            "reason": (metric.reason or "")[:500] if hasattr(metric, "reason") else "",
        }
    except asyncio.TimeoutError:
        return {"error": "timeout", "detail": f"metric {name} exceeded {PER_METRIC_TIMEOUT_SECONDS}s"}
    except Exception as e:  # broad catch: judge JSON parse errors, rate limits, etc.
        return {"error": type(e).__name__, "detail": str(e)[:300]}


async def _score_one_sample(sample: dict, model: str, sem: asyncio.Semaphore) -> dict:
    """Score one sample across all four RAG metrics. Concurrent within sample."""
    from deepeval.test_case import LLMTestCase

    async with sem:
        metrics = _build_metrics(model)
        # Empty context is valid — it just means recall/precision will likely be
        # 0 or null. We still pass the rest of the metrics; faithfulness on an
        # empty context tends to return null which we record as such.
        retrieval_context = sample["context"] if sample["context"] else [""]
        test_case = LLMTestCase(
            input=sample["question"],
            actual_output=sample["answer"] or "",
            expected_output=sample["gold"] or "",
            retrieval_context=retrieval_context,
        )
        # Run all four metrics concurrently for this sample (DeepEval's a_measure
        # is awaitable; gather lets the four metric LLM call chains overlap).
        names = list(metrics.keys())
        results = await asyncio.gather(
            *[_measure_one_metric(metrics[n], test_case, n) for n in names],
            return_exceptions=False,
        )
        return {name: res for name, res in zip(names, results)}


async def _run_all(samples: list[dict], model: str, concurrency: int, prior: dict[str, dict] | None = None) -> list[dict]:
    """Score all samples concurrently. If `prior` has a clean prior result for a
    sample (no errors, all four metrics scored), reuse it — saves judge tokens.
    """
    prior = prior or {}
    sem = asyncio.Semaphore(concurrency)

    async def _one(s: dict) -> dict:
        existing = prior.get(s["fb_id"])
        if existing and _is_clean_prior(existing):
            return existing
        return await _score_one_sample(s, model, sem)

    return await tqdm_asyncio.gather(
        *[_one(s) for s in samples],
        desc="DeepEval",
        unit="q",
        ncols=100,
    )


def _is_clean_prior(metrics_result: dict) -> bool:
    """True iff prior result has all four metrics with a numeric score and no error."""
    expected = {"faithfulness", "answer_relevancy", "contextual_precision", "contextual_recall"}
    if set(metrics_result.keys()) != expected:
        return False
    for v in metrics_result.values():
        if not isinstance(v, dict):
            return False
        if "error" in v:
            return False
        if v.get("score") is None:
            # Allow None scores from prior runs (genuine null verdicts) — don't
            # waste tokens re-rolling them; aggregation already excludes nulls.
            continue
    return True


def _aggregate(per_sample_metrics: list[dict]) -> dict:
    """Compute mean per metric, ignoring null/errored samples."""
    metric_names = ["faithfulness", "answer_relevancy", "contextual_precision", "contextual_recall"]
    agg = {}
    for m in metric_names:
        scores = []
        for sample in per_sample_metrics:
            v = sample.get(m, {})
            if isinstance(v, dict) and isinstance(v.get("score"), (int, float)):
                scores.append(float(v["score"]))
        agg[m] = round(sum(scores) / len(scores), 4) if scores else None
        agg[f"{m}_n_valid"] = len(scores)
    return agg


def main():
    parser = argparse.ArgumentParser(description="Score a FinanceBench pipeline cache with DeepEval.")
    parser.add_argument("--cache", required=True, help="Path to .pipeline.json cache from run_financebench.py")
    parser.add_argument("--output", required=True, help="Output path for per-sample + aggregate scores")
    parser.add_argument("--limit", type=int, default=None, help="Score only first N samples (smoke test)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help=f"Concurrent samples (default {DEFAULT_CONCURRENCY})")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help=f"Judge LLM (default {DEFAULT_JUDGE_MODEL})")
    parser.add_argument("--resume", action="store_true", help="If output exists, only re-score samples whose prior result was an error.")
    args = parser.parse_args()

    if not settings.OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY not set. DeepEval needs it for the judge LLM.")
        sys.exit(1)

    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"ERROR: cache not found: {cache_path}")
        sys.exit(1)
    if not FB_QA_PATH.exists():
        print(f"ERROR: ground truth not found: {FB_QA_PATH}")
        sys.exit(1)

    cache = json.loads(cache_path.read_text())
    questions = cache["questions"]
    answers = cache["answers"]
    contexts = cache["contexts"]
    fb_qa = [json.loads(line) for line in open(FB_QA_PATH)][: len(questions)]

    if cache["questions"][0] != fb_qa[0]["question"]:
        print("ERROR: cache questions don't match FB jsonl order. Did you regenerate?")
        sys.exit(1)

    samples = [
        {
            "fb_id": fb_qa[i]["financebench_id"],
            "company": fb_qa[i].get("company", ""),
            "question": questions[i],
            "answer": answers[i],
            "gold": fb_qa[i]["answer"],
            "context": contexts[i] if contexts[i] else [],
        }
        for i in range(len(questions))
    ]
    if args.limit:
        samples = samples[: args.limit]

    # Resume: load prior results so we don't re-score successful samples.
    prior_by_id: dict[str, dict] = {}
    output_path = Path(args.output)
    if args.resume and output_path.exists():
        prior = json.loads(output_path.read_text())
        for s in prior.get("per_sample", []):
            pid = s.get("fb_id")
            metrics_blob = s.get("deepeval") or {}
            if pid:
                prior_by_id[pid] = metrics_blob
        ok = sum(1 for r in prior_by_id.values() if _is_clean_prior(r))
        bad = len(prior_by_id) - ok
        print(f"Resume mode: prior file has {ok} clean, {bad} dirty/incomplete. Will only re-score the dirty ones.")

    print(f"Cache:        {cache_path}")
    print(f"Output:       {output_path}")
    print(f"N samples:    {len(samples)}")
    print(f"Judge model:  {args.judge_model}")
    print(f"Concurrency:  {args.concurrency}")
    print()

    start = time.time()
    results = asyncio.run(_run_all(samples, args.judge_model, args.concurrency, prior=prior_by_id))
    elapsed = time.time() - start

    per_sample = [{**s, "deepeval": r} for s, r in zip(samples, results)]
    aggregate = _aggregate(results)

    # Count samples with at least one metric error (useful diagnostic).
    n_with_errors = sum(1 for r in results if any(isinstance(v, dict) and "error" in v for v in r.values()))

    out = {
        "framework": "deepeval",
        "judge_model": args.judge_model,
        "n_samples": len(samples),
        "n_samples_with_metric_errors": n_with_errors,
        "elapsed_seconds": round(elapsed, 1),
        "aggregate": aggregate,
        "per_sample": per_sample,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(out, indent=2))

    print()
    print("=" * 70)
    print("DEEPEVAL RESULTS")
    print("=" * 70)
    print(f"  n_samples:        {len(samples)}")
    print(f"  errors:           {n_with_errors} sample(s) had >=1 metric error")
    print(f"  wall_clock:       {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print()
    for k in ("faithfulness", "answer_relevancy", "contextual_precision", "contextual_recall"):
        v = aggregate.get(k)
        n = aggregate.get(f"{k}_n_valid")
        if v is None:
            print(f"  {k:25s} N/A          (n_valid={n})")
        else:
            print(f"  {k:25s} {v:.4f}       (n_valid={n})")
    print(f"\n  saved:            {output_path}")


if __name__ == "__main__":
    main()
