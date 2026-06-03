"""Score a FinanceBench pipeline cache with Patronus fuzzy-match (REST direct).

We bypass the Patronus Python SDK because its OpenTelemetry version is
incompatible with our Python 3.11 OTel stack. The REST endpoint is stable
and works without any SDK install.

Reads a pipeline cache (questions/answers/contexts produced by
`tests/evaluation/run_financebench.py`) and the FinanceBench ground-truth
JSONL, calls Patronus `/v1/evaluate` per sample with the `judge` evaluator
and `patronus:fuzzy-match` criterion (the same evaluator FinanceBench
leaderboards use), and writes per-sample + aggregate scores.

Usage:
    python scripts/score_patronus.py \\
        --cache tests/evaluation/eval_results/financebench_baseline.pipeline.json \\
        --output tests/evaluation/eval_results/financebench_baseline.patronus.json

    # smoke-test on 5 samples first to verify wiring
    python scripts/score_patronus.py --cache <path> --output <path> --limit 5
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
from tqdm.asyncio import tqdm_asyncio

from src.config.settings import settings

PATRONUS_URL = "https://api.patronus.ai/v1/evaluate"
FB_QA_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")
EVALUATOR = "judge"
CRITERIA = "patronus:fuzzy-match"

# Concurrency cap — Patronus free-tier rate limits aren't publicly documented.
# Empirically, concurrency=5 → ~40% errors. Drop to 2 for headroom.
DEFAULT_CONCURRENCY = 2
# Per-request timeout. Fuzzy-match is fast (<5s typical), but allow headroom.
HTTP_TIMEOUT = 60.0
# Max retries on 429 / transient errors. Wider backoff to outlast bursty limits.
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0  # backoff = base * 2**attempt → 2, 4, 8, 16, 32s


async def _evaluate_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    api_key: str,
    question: str,
    answer: str,
    gold: str,
    context: list[str],
) -> dict:
    """Call /v1/evaluate for one sample. Retries on 429 with exponential backoff."""
    payload = {
        "evaluators": [
            {
                "evaluator": EVALUATOR,
                "criteria": CRITERIA,
                "explain_strategy": "on-fail",
            }
        ],
        "evaluated_model_input": question,
        "evaluated_model_output": answer or "",
        "evaluated_model_gold_answer": gold,
    }
    if context:
        # Send up to first 3 retrieved contexts (Patronus payload size cap is generous,
        # but fuzzy-match doesn't actually use context — keeping it small for speed).
        payload["evaluated_model_retrieved_context"] = context[:3]

    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    last_error: dict = {"error": "no_attempt_made"}
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.post(PATRONUS_URL, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
                if resp.status_code == 429:
                    last_error = {"error": "http_429", "detail": resp.text[:300]}
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                        continue
                    break
                if resp.status_code >= 500:
                    last_error = {"error": f"http_{resp.status_code}", "detail": resp.text[:300]}
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                        continue
                    break
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results") or []
                if not results:
                    return {"error": "empty_results", "raw": data}
                er = results[0].get("evaluation_result") or {}
                return {
                    "pass": er.get("pass"),
                    "score": er.get("score_raw"),
                    "explanation": er.get("explanation", ""),
                }
            except httpx.HTTPStatusError as e:
                last_error = {"error": f"http_{e.response.status_code}", "detail": e.response.text[:300]}
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                    continue
                break
            except (httpx.RequestError, asyncio.TimeoutError) as e:
                last_error = {"error": type(e).__name__, "detail": str(e)[:300]}
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                    continue
                break
        return last_error


async def _run_all(samples: list[dict], api_key: str, concurrency: int, prior: dict[str, dict] | None = None) -> list[dict]:
    """Score `samples` against Patronus. If `prior` maps fb_id -> existing patronus
    result, samples whose prior result has no `error` are returned untouched
    (resume mode — don't re-bill for already-completed samples).
    """
    prior = prior or {}
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        async def _one(s: dict) -> dict:
            existing = prior.get(s["fb_id"])
            if existing and "pass" in existing:
                return existing  # already scored; don't waste API call
            return await _evaluate_one(
                client, sem, api_key,
                question=s["question"],
                answer=s["answer"],
                gold=s["gold"],
                context=s["context"],
            )

        tasks = [_one(s) for s in samples]
        return await tqdm_asyncio.gather(*tasks, desc="Patronus", unit="q", ncols=100)


def main():
    parser = argparse.ArgumentParser(description="Score a FinanceBench pipeline cache with Patronus fuzzy-match.")
    parser.add_argument("--cache", required=True, help="Path to .pipeline.json cache from run_financebench.py")
    parser.add_argument("--output", required=True, help="Output path for per-sample + aggregate scores")
    parser.add_argument("--limit", type=int, default=None, help="Score only first N samples (smoke test)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help=f"Concurrent requests (default {DEFAULT_CONCURRENCY})")
    parser.add_argument("--resume", action="store_true", help="If output exists, only re-score samples whose prior result was an error.")
    args = parser.parse_args()

    api_key = settings.PATRONUS_API_KEY
    if not api_key:
        print("ERROR: PATRONUS_API_KEY not set. Add it to .env.")
        sys.exit(1)

    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"ERROR: cache not found: {cache_path}")
        sys.exit(1)
    if not FB_QA_PATH.exists():
        print(f"ERROR: ground truth not found: {FB_QA_PATH}")
        sys.exit(1)

    # Load cache + ground truths (cache is in original FB order; ground truth jsonl too)
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

    # Resume: load prior results so we don't re-score successful samples
    prior_by_id: dict[str, dict] = {}
    output_path = Path(args.output)
    if args.resume and output_path.exists():
        prior = json.loads(output_path.read_text())
        for s in prior.get("per_sample", []):
            pid = s.get("fb_id")
            pres = s.get("patronus") or {}
            if pid:
                prior_by_id[pid] = pres
        ok = sum(1 for r in prior_by_id.values() if "pass" in r)
        err = sum(1 for r in prior_by_id.values() if "error" in r)
        print(f"Resume mode: prior file has {ok} ok, {err} errors. Will re-score {err} errored samples only.")

    print(f"Cache:        {cache_path}")
    print(f"Output:       {output_path}")
    print(f"N samples:    {len(samples)}")
    print(f"Concurrency:  {args.concurrency}")
    print(f"Endpoint:     {PATRONUS_URL}")
    print(f"Criteria:     {CRITERIA}")
    print()

    start = time.time()
    results = asyncio.run(_run_all(samples, api_key, args.concurrency, prior=prior_by_id))
    elapsed = time.time() - start

    # Combine inputs + results
    per_sample = []
    for s, r in zip(samples, results):
        per_sample.append({**s, "patronus": r})

    # Aggregates (skip errors)
    valid = [r for r in results if "pass" in r]
    errors = [r for r in results if "error" in r]
    n_valid = len(valid)
    pass_rate = sum(1 for r in valid if r.get("pass")) / n_valid if n_valid else float("nan")
    scores = [r["score"] for r in valid if isinstance(r.get("score"), (int, float))]
    avg_score = sum(scores) / len(scores) if scores else float("nan")

    out = {
        "criteria": CRITERIA,
        "endpoint": PATRONUS_URL,
        "n_samples": len(samples),
        "n_valid": n_valid,
        "n_errors": len(errors),
        "elapsed_seconds": round(elapsed, 1),
        "aggregate": {
            "pass_rate": round(pass_rate, 4) if pass_rate == pass_rate else None,  # NaN check
            "avg_score": round(avg_score, 4) if avg_score == avg_score else None,
        },
        "per_sample": per_sample,
    }
    out_path = output_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))

    print()
    print("=" * 70)
    print("PATRONUS RESULTS")
    print("=" * 70)
    print(f"  n_samples:   {len(samples)}")
    print(f"  n_valid:     {n_valid}")
    print(f"  n_errors:    {len(errors)}")
    if errors:
        # Show first error type counts
        from collections import Counter
        types = Counter(r.get("error", "?") for r in errors)
        print(f"  error types: {dict(types)}")
    print(f"  pass_rate:   {pass_rate:.4f}" if n_valid else "  pass_rate:   N/A")
    print(f"  avg_score:   {avg_score:.4f}" if scores else "  avg_score:   N/A")
    print(f"  wall_clock:  {elapsed:.1f}s")
    print(f"  saved:       {out_path}")


if __name__ == "__main__":
    main()
