"""FinanceBench Phase B — full 150-question evaluation.

Two-phase runner (mirrors run_evaluation.py):
  Phase 1 (pipeline): run each FB question through our 17-node graph pointed
    at the isolated `financebench_corpus` Qdrant collection. Cache answers +
    contexts to disk so Phase 2 can resume/re-score without re-running.
  Phase 2 (scoring): score with up to three independent judges:
    - RAGAS    (internal comparability — same metrics as our custom eval)
    - DeepEval (second LLM-judge framework — different prompts, same OpenAI
                judge model; replaces Patronus when its credit cap is exhausted)
    - Patronus fuzzy-match (external leaderboard comparability — gated by
                PATRONUS_API_KEY; will hard-fail rows with `http_402` once the
                free tier is depleted, so default to skipping it now.)

All three judges produce 0-1 per-sample scores; aggregates land in the main
output JSON, full per-sample details land in `*.<framework>.json` siblings.

Usage:
    # full run (pipeline + RAGAS + DeepEval; Patronus skipped by default)
    python tests/evaluation/run_financebench.py \
        --output tests/evaluation/eval_results/financebench_baseline.json

    # re-score only (pipeline cache already exists, complete or partial)
    python tests/evaluation/run_financebench.py \
        --output tests/evaluation/eval_results/financebench_baseline.json \
        --skip-pipeline

    # resume an interrupted pipeline run (picks up at last flushed checkpoint;
    # default flush cadence is every 5 questions, so worst-case loss is 5)
    python tests/evaluation/run_financebench.py \
        --output tests/evaluation/eval_results/financebench_baseline.json \
        --resume-pipeline

    # limit to first N questions (smoke test)
    python tests/evaluation/run_financebench.py --limit 5 --skip-patronus

    # opt back into Patronus (if credits restored)
    python tests/evaluation/run_financebench.py \
        --output tests/evaluation/eval_results/financebench_baseline.json \
        --enable-patronus
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

from langchain_core.messages import HumanMessage
from tqdm import tqdm

from src.config.settings import settings
from src.graph.builder import build_graph
from src.services.company_registry import canonical_company_slug
from tests.evaluation.analysis_utils import build_slice_summary, extract_contamination_buckets, is_refusal
from tests.evaluation.eval_config import EVALUATOR_MODEL

# --- Mirror secrets into os.environ for libraries that read them there ---
if settings.OPENAI_API_KEY and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
if settings.PATRONUS_API_KEY and not os.environ.get("PATRONUS_API_KEY"):
    os.environ["PATRONUS_API_KEY"] = settings.PATRONUS_API_KEY

# --- Silence noisy loggers so the tqdm bar stays readable ---
_NOISY = [
    "httpx", "httpcore", "presidio-analyzer", "presidio-anonymizer",
    "openai", "langchain", "langchain_core", "langgraph",
    "qdrant_client", "py.warnings", "urllib3", "llm_guard", "patronus",
]
for _n in _NOISY:
    logging.getLogger(_n).setLevel(logging.WARNING)
for _m in ["src.graph.nodes", "src.services"]:
    logging.getLogger(_m).setLevel(logging.WARNING)
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="qdrant_client")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FB_DATA_DIR = Path("data/raw/financebench")
FB_QA_PATH = FB_DATA_DIR / "financebench_open_source.jsonl"
FB_COLLECTION_DEFAULT = "financebench_corpus"


def _git_state() -> dict:
    """Return current git SHA and dirty status (best-effort; non-fatal on failure)."""
    info: dict = {"sha": None, "dirty": None, "branch": None}
    try:
        info["sha"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        info["dirty"] = bool(out)
    except Exception:
        pass
    try:
        info["branch"] = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        pass
    return info


def _settings_snapshot() -> dict:
    """Capture eval-relevant settings for reproducibility. Read from `settings`
    (Pydantic) plus a few raw env-var lookups for the on-disk-only patches.

    These get embedded in every pipeline cache so two runs can be PROVEN to
    have used identical configuration post-hoc.
    """
    snap: dict = {
        "FORCE_OPENAI_ONLY": settings.FORCE_OPENAI_ONLY,
        "QDRANT_HOST": settings.QDRANT_HOST,
        "QDRANT_PORT": settings.QDRANT_PORT,
    }
    for k in (
        "RETRIEVAL_TOP_K",
        "RERANKER_TOP_K",
        "MAX_RETRIEVAL_RETRIES",
        "MAX_GENERATION_RETRIES",
        "GRADING_MIN_RELEVANT_CHUNKS",
        "ENABLE_DETERMINISTIC_VALIDATOR",
        "VALIDATOR_MIN_KEEP",
        "ENABLE_LTR_GATE",
        "ENABLE_SELECTIVE_RETRIEVAL_EVALUATOR",
        "RETRIEVAL_EVALUATOR_MIN_CONFIDENCE",
        "OPENAI_FALLBACK_MODEL",
        "GENERATOR_MODEL",
        "HALLUCINATION_MODEL",
        "ROUTER_MODEL",
        "GRADER_MODEL",
    ):
        snap[k] = getattr(settings, k, None)
    # Env-only knobs (the MPS-stability patches don't go through Settings).
    # Record the EFFECTIVE value — i.e. the value the code will actually see
    # after applying its built-in defaults — not the raw env lookup. This keeps
    # the cache unambiguous (e.g. unset RERANKER_DEVICE means the code uses
    # "cpu", not "null"). Defaults must mirror the ones in the source files:
    #   src/services/reranker_service.py: DEFAULT_DEVICE = "cpu"
    #   src/services/guardrails_service.py: LLM_GUARD_USE_ONNX defaults "1"
    #   src/services/guardrails_service.py: RAG_DISABLE_LLM_GUARD defaults unset → "0"
    snap["RERANKER_DEVICE"] = os.environ.get("RERANKER_DEVICE", "cpu")
    snap["LLM_GUARD_USE_ONNX"] = os.environ.get("LLM_GUARD_USE_ONNX", "1")
    snap["RAG_DISABLE_LLM_GUARD"] = os.environ.get("RAG_DISABLE_LLM_GUARD", "0")
    return snap


def _llm_guard_runtime_status() -> str:
    """Probe whether LLM Guard's PromptInjection scanner actually loads at runtime.

    Returns one of: "active" (loaded successfully), "disabled" (the scanner
    chose to disable itself, e.g. due to missing optimum/ONNX backend), or
    "error: <type>" if probing itself raised. Recorded in run_metadata so a
    silent disable (which is a real possibility on systems without optimum
    installed) is visible in the cache and not a hidden assumption.
    """
    try:
        from src.services.guardrails_service import _get_injection_scanner
        scanner = _get_injection_scanner()
        return "active" if scanner is not None else "disabled"
    except Exception as e:
        return f"error: {type(e).__name__}"


def _qdrant_collection_info(collection: str) -> dict:
    """Return point count + payload schema fingerprint for a Qdrant collection.

    Used both as a pre-flight check (collection exists, has data) and as
    reproducibility metadata embedded in the cache (so we can verify both
    runs queried identically-sized collections post-hoc).
    """
    try:
        from src.services.vector_store import get_qdrant_client
        client = get_qdrant_client()
        all_collections = {c.name for c in client.get_collections().collections}
        if collection not in all_collections:
            return {"exists": False, "point_count": 0, "available": sorted(all_collections)}
        info = client.get_collection(collection)
        return {
            "exists": True,
            "point_count": int(info.points_count or 0),
            "indexed_vectors": int(info.indexed_vectors_count or 0),
            "status": getattr(info, "status", None) and str(info.status),
        }
    except Exception as e:
        return {"exists": False, "point_count": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _preflight_checks(collection: str, judge_model: str) -> dict:
    """Pre-flight validation. Fail fast (in seconds) instead of failing 75 min in.

    Checks:
      - OPENAI_API_KEY is set
      - Qdrant collection exists and has > 0 points
      - FB ground-truth JSONL is readable
      - Probe OpenAI with a single 1-token completion to confirm the key works
        and the judge model is accessible (catches stale/expired keys, account
        billing issues, model deprecation early)

    Returns the qdrant collection info dict (used downstream for reproducibility
    metadata). Exits via SystemExit on failure.
    """
    failures: list[str] = []
    if not settings.OPENAI_API_KEY:
        failures.append("OPENAI_API_KEY not set in environment / .env")
    if not FB_QA_PATH.exists():
        failures.append(f"FinanceBench ground truth not found: {FB_QA_PATH}")

    qinfo = _qdrant_collection_info(collection)
    if not qinfo.get("exists"):
        avail = qinfo.get("available", "(unknown)")
        err = qinfo.get("error", "")
        failures.append(f"Qdrant collection '{collection}' not found. Available: {avail}. {err}")
    elif qinfo.get("point_count", 0) == 0:
        failures.append(f"Qdrant collection '{collection}' exists but is empty (0 points)")

    if not failures and settings.OPENAI_API_KEY:
        try:
            from openai import OpenAI
            probe_client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=15.0)
            # 1-token completion = ~zero cost (~$0.000001) but verifies the
            # whole stack: key valid, billing OK, model reachable.
            probe_client.chat.completions.create(
                model=judge_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
        except Exception as e:
            failures.append(f"OpenAI probe failed (model={judge_model}): {type(e).__name__}: {str(e)[:200]}")

    if failures:
        print()
        print("=" * 70)
        print("PRE-FLIGHT CHECK FAILURES — aborting before pipeline phase")
        print("=" * 70)
        for f in failures:
            print(f"  ✗ {f}")
        print()
        sys.exit(1)

    print(f"Pre-flight: OK  (Qdrant '{collection}' has {qinfo.get('point_count')} points)")
    return qinfo


def _maybe_empty_mps_cache() -> None:
    """Periodic MPS-cache eviction so PyTorch's accumulated allocations don't
    crowd out the unified memory pool on Apple Silicon. No-op on systems
    without MPS (Linux / Intel Mac / non-PyTorch environments).
    """
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def _load_dataset(limit: int | None = None) -> list[dict]:
    if not FB_QA_PATH.exists():
        print(f"ERROR: {FB_QA_PATH} missing. Run scripts/download_financebench.py first.")
        sys.exit(1)
    data = [json.loads(line) for line in open(FB_QA_PATH)]
    if limit:
        data = data[:limit]
    logger.info(f"Loaded {len(data)} FinanceBench questions")
    return data


def _build_initial_state(question: str) -> dict:
    """Admin role, no HITL. Matches run_evaluation.py pattern."""
    return {
        "messages": [HumanMessage(content=question)],
        "user_id": "financebench_runner",
        "user_role": "admin",
        "allowed_doc_types": [],
        "guardrail_status": "clean",
        "detected_pii_entities": [],
        "sanitized_query": "",
        "query_intent": "",
        "target_company": None,
        "target_fiscal_year": None,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "retrieval_query": "",
        "relevant_chunks": [],
        "grading_results": [],
        "generated_answer": "",
        "hallucination_status": "",
        "hallucination_score": 0.0,
        "requires_human_approval": False,
        "human_decision": None,
        "retrieval_retry_count": 0,
        "generation_retry_count": 0,
        "final_response": "",
        "response_metadata": {},
    }


def _pipeline_cache_path(output_path: Path) -> Path:
    return output_path.with_suffix(".pipeline.json")


def _write_cache_atomic(
    cache_path: Path,
    questions: list[str],
    answers: list[str],
    contexts: list[list[str]],
    pipeline_time_seconds: float,
    complete: bool,
    run_metadata: dict | None = None,
) -> None:
    """Atomic cache write via temp-file-then-rename.

    Used for both the periodic in-flight flush and the final post-run write.
    `complete=False` marks partial caches so `--resume-pipeline` knows it's safe
    to pick up from there; `complete=True` marks a full run that should not be
    resumed (re-running with `--resume-pipeline` against a complete cache is a
    no-op — it loads the full cache and skips the pipeline phase).

    `run_metadata` carries reproducibility fingerprints (git SHA, settings
    snapshot, qdrant collection state, judge/generator model names). Embedded
    in the cache so two runs can be PROVEN to have used identical config
    post-hoc — critical for the pypdf-vs-docling comparison fairness claim.
    """
    payload = {
        "questions": questions,
        "answers": answers,
        "contexts": contexts,
        "pipeline_time_seconds": round(pipeline_time_seconds, 1),
        "complete": complete,
        "n_done": len(answers),
        "n_total": len(questions),
    }
    if run_metadata is not None:
        payload["run_metadata"] = run_metadata
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(cache_path)  # atomic on POSIX


def run_pipeline_phase(
    dataset: list[dict],
    collection: str,
    cache_path: Path | None = None,
    flush_every: int = 5,
    seed_answers: list[str] | None = None,
    seed_contexts: list[list[str]] | None = None,
    seed_elapsed_seconds: float = 0.0,
    run_metadata: dict | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Run each FB question through the graph pointed at the given FB collection.

    We temporarily override the Qdrant collection name via settings (monkeypatch
    for the duration of the run). The graph is built once and reused; checkpointing
    is off (thread_id unique per question so no state leaks between questions).

    Resilience features:
      - Periodic atomic cache flush every `flush_every` questions (so a crash
        loses at most flush_every questions of work). Also flushes on Ctrl-C.
      - `seed_answers`/`seed_contexts` let `--resume-pipeline` pre-populate the
        first N entries; the loop skips those and continues from question N+1.
      - `seed_elapsed_seconds` accumulates wall-clock across resumed sessions so
        the reported pipeline_time reflects total work, not just this session.
    """
    questions = [r["question"] for r in dataset]
    answers = list(seed_answers or [])
    contexts = list(seed_contexts or [])
    start_idx = min(len(answers), len(contexts), len(dataset))
    answers = answers[:start_idx]
    contexts = contexts[:start_idx]
    if start_idx:
        logger.info(f"Resuming pipeline from question {start_idx + 1}/{len(dataset)}")

    original_collection = settings.QDRANT_COLLECTION
    settings.QDRANT_COLLECTION = collection
    session_start = time.time()
    try:
        graph = build_graph(checkpointer=None)
        failures = 0
        remaining = dataset[start_idx:]
        pbar = tqdm(
            remaining,
            desc="FB pipeline",
            unit="q",
            ncols=100,
            initial=start_idx,
            total=len(dataset),
        )
        for offset, rec in enumerate(pbar):
            i = start_idx + offset
            q = rec["question"]
            pbar.set_postfix_str(f"{rec['company'][:15]} | {q[:30]}")
            state = _build_initial_state(q)
            config = {
                "configurable": {"thread_id": f"fb_{rec['financebench_id']}"},
                "metadata": {"hitl_enabled": False},
            }
            try:
                result = graph.invoke(state, config=config)
                ans = result.get("final_response", "")
                chunks = [c["content"] for c in result.get("relevant_chunks", []) if "content" in c]
            except Exception as e:
                failures += 1
                tqdm.write(f"  [FAIL {failures}] Q{i + 1} {rec['financebench_id']}: {type(e).__name__}: {str(e)[:100]}")
                ans = ""
                chunks = []
            answers.append(ans)
            contexts.append(chunks if chunks else [""])

            # Periodic MPS cache flush — keeps Apple-Silicon unified-memory
            # pressure from accumulating across questions. Cheap (~ms), critical
            # for runs > ~50 questions.
            _maybe_empty_mps_cache()

            # Flush partial cache every `flush_every` questions so a crash never
            # loses more than that much work. Atomic-rename so the file on disk
            # is never half-written.
            if cache_path is not None and (i + 1) % flush_every == 0:
                _write_cache_atomic(
                    cache_path,
                    questions=questions,
                    answers=answers,
                    contexts=contexts,
                    pipeline_time_seconds=seed_elapsed_seconds + (time.time() - session_start),
                    complete=False,
                    run_metadata=run_metadata,
                )
        pbar.close()
        if failures:
            logger.warning(f"{failures}/{len(remaining)} questions failed during this session")
        return answers, contexts
    except KeyboardInterrupt:
        # Flush before bubbling so Ctrl-C never costs more than `flush_every`.
        if cache_path is not None:
            _write_cache_atomic(
                cache_path,
                questions=questions,
                answers=answers,
                contexts=contexts,
                pipeline_time_seconds=seed_elapsed_seconds + (time.time() - session_start),
                complete=False,
                run_metadata=run_metadata,
            )
            logger.warning(f"Interrupted; partial cache flushed ({len(answers)}/{len(dataset)} done): {cache_path}")
        raise
    finally:
        settings.QDRANT_COLLECTION = original_collection


def build_diagnostics(dataset: list[dict], answers: list[str], contexts: list[list[str]], pass_labels: list[bool] | None = None) -> dict:
    """Build phase-0 diagnostics for FinanceBench runs."""
    n = len(dataset)
    refusals = sum(1 for a in answers if is_refusal(a))
    refusal_rate = refusals / n if n else 0.0

    target_companies = [canonical_company_slug(r.get("company")) for r in dataset]
    target_years = []
    for rec in dataset:
        period = str(rec.get("doc_period", "")).strip()
        target_years.append(int(period) if period.isdigit() else None)

    contamination = extract_contamination_buckets(
        queries=[r["question"] for r in dataset],
        contexts=contexts,
        target_companies=target_companies,
        target_years=target_years,
    )
    slice_summary = build_slice_summary(
        questions=[r["question"] for r in dataset],
        answers=answers,
        pass_labels=pass_labels,
    )

    answered = n - refusals
    pass_when_answered = None
    if pass_labels is not None and answered > 0:
        answered_passes = sum(
            1
            for i, a in enumerate(answers)
            if not is_refusal(a) and i < len(pass_labels) and pass_labels[i]
        )
        pass_when_answered = answered_passes / answered

    return {
        "refusal_rate": refusal_rate,
        "refusal_count": refusals,
        "pass_when_answered": pass_when_answered,
        "slice_summary": slice_summary,
        "contamination": contamination,
    }


def score_with_ragas(
    questions: list[str],
    answers: list[str],
    contexts: list[list[str]],
    ground_truths: list[str],
    evaluator_model: str = EVALUATOR_MODEL,
) -> dict:
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.llms import llm_factory
    from ragas.metrics import (
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )

    evaluator_llm = llm_factory(evaluator_model)
    metrics = [
        Faithfulness(llm=evaluator_llm),
        AnswerRelevancy(llm=evaluator_llm),
        ContextPrecision(llm=evaluator_llm),
        ContextRecall(llm=evaluator_llm),
    ]
    samples = [
        SingleTurnSample(user_input=q, response=a, retrieved_contexts=c, reference=gt)
        for q, a, c, gt in zip(questions, answers, contexts, ground_truths)
    ]
    dataset = EvaluationDataset(samples=samples)
    logger.info(f"RAGAS scoring {len(samples)} samples ({evaluator_model} judge)...")
    results = evaluate(dataset=dataset, metrics=metrics, show_progress=True)
    df = results.to_pandas()
    return {
        "faithfulness": float(df["faithfulness"].mean()),
        "answer_relevancy": float(df["answer_relevancy"].mean()),
        "context_precision": float(df["context_precision"].mean()),
        "context_recall": float(df["context_recall"].mean()),
    }


def score_with_deepeval(
    cache_path: Path,
    deepeval_output_path: Path,
    limit: int | None = None,
    judge_model: str = EVALUATOR_MODEL,
    concurrency: int = 4,
) -> dict:
    """Run DeepEval's four RAG metrics via the standalone scorer script.

    Kept out-of-process for the same reason as Patronus: DeepEval's metrics own
    their own asyncio event loop + global posthog/sentry side-effects, and
    spawning them from inside a long-lived runner that already has its own
    asyncio context (LangGraph + httpx + qdrant) is fragile. Subprocess gives a
    clean event-loop boundary and lets us rerun/resume independently.
    """
    cmd = [
        sys.executable,
        "scripts/score_deepeval.py",
        "--cache",
        str(cache_path),
        "--output",
        str(deepeval_output_path),
        "--judge-model",
        judge_model,
        "--concurrency",
        str(concurrency),
        "--resume",
    ]
    if limit:
        cmd.extend(["--limit", str(limit)])
    logger.info(f"DeepEval scoring via standalone helper (judge={judge_model}, concurrency={concurrency})...")
    subprocess.run(cmd, check=True)

    try:
        payload = json.loads(deepeval_output_path.read_text())
        agg = payload.get("aggregate", {})
        return {
            "faithfulness": agg.get("faithfulness"),
            "answer_relevancy": agg.get("answer_relevancy"),
            "contextual_precision": agg.get("contextual_precision"),
            "contextual_recall": agg.get("contextual_recall"),
            "n_samples": int(payload.get("n_samples", 0)),
            "n_samples_with_metric_errors": int(payload.get("n_samples_with_metric_errors", 0)),
            "judge_model": payload.get("judge_model", judge_model),
            "output_file": str(deepeval_output_path),
        }
    except Exception as e:
        logger.warning(f"Could not parse DeepEval output: {e}")
        return {"deepeval_error": str(e), "n_samples": 0}


def score_with_patronus(
    cache_path: Path,
    patronus_output_path: Path,
    limit: int | None = None,
    patronus_evaluator: str = "judge",
    patronus_criteria: str = "patronus:fuzzy-match",
) -> dict:
    """Run Patronus fuzzy-match via direct REST helper script.

    We intentionally avoid Patronus SDK here due to observed OpenTelemetry
    incompatibility in our env; `scripts/score_patronus.py` uses `/v1/evaluate`
    directly and supports retries/resume.
    """
    if not settings.PATRONUS_API_KEY:
        logger.warning("PATRONUS_API_KEY not set — skipping Patronus scoring")
        return {"skipped": True}
    if patronus_evaluator != "judge" or patronus_criteria != "patronus:fuzzy-match":
        logger.warning(
            "REST helper currently supports judge=judge + criteria=patronus:fuzzy-match only; "
            "ignoring custom Patronus evaluator/criteria overrides."
        )

    cmd = [
        sys.executable,
        "scripts/score_patronus.py",
        "--cache",
        str(cache_path),
        "--output",
        str(patronus_output_path),
        "--resume",
    ]
    if limit:
        cmd.extend(["--limit", str(limit)])
    logger.info(
        f"Patronus scoring via REST helper "
        f"(evaluator={patronus_evaluator}, criteria={patronus_criteria})..."
    )
    subprocess.run(cmd, check=True)

    try:
        payload = json.loads(patronus_output_path.read_text())
        agg = payload.get("aggregate", {})
        pass_rate = agg.get("pass_rate")
        return {
            "patronus_fuzzy_match_pass_rate": float(pass_rate) if pass_rate is not None else float("nan"),
            "n_samples": int(payload.get("n_samples", 0)),
            "n_valid": int(payload.get("n_valid", 0)),
            "n_errors": int(payload.get("n_errors", 0)),
            "output_file": str(patronus_output_path),
        }
    except Exception as e:
        logger.warning(f"Could not parse Patronus REST output: {e}")
        return {"patronus_error": str(e), "n_samples": 0}


def main():
    parser = argparse.ArgumentParser(description="FinanceBench full 150-Q evaluation")
    parser.add_argument("--output", "-o", type=str, required=True, help="Output JSON path")
    parser.add_argument(
        "--collection",
        default=FB_COLLECTION_DEFAULT,
        help=f"Qdrant collection to query (default: {FB_COLLECTION_DEFAULT})",
    )
    parser.add_argument("--skip-pipeline", action="store_true", help="Reuse cached pipeline output (full skip)")
    parser.add_argument(
        "--resume-pipeline",
        action="store_true",
        help="If a partial pipeline cache exists (complete=false), continue from where it left off",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=5,
        help="Flush partial pipeline cache every N questions (default: 5; lower = safer, more disk I/O)",
    )
    parser.add_argument("--skip-ragas", action="store_true", help="Skip RAGAS scoring")
    parser.add_argument(
        "--skip-deepeval",
        action="store_true",
        help="Skip DeepEval scoring (default: run)",
    )
    # Patronus is OFF by default while the free-tier credits remain depleted.
    # Pass --enable-patronus to opt back in once credits are restored.
    parser.add_argument("--skip-patronus", action="store_true", default=True, help="Skip Patronus scoring (default: True)")
    parser.add_argument(
        "--enable-patronus",
        dest="skip_patronus",
        action="store_false",
        help="Enable Patronus fuzzy-match scoring (requires PATRONUS_API_KEY + credits)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Only run first N questions")
    parser.add_argument(
        "--ragas-judge-model",
        default=EVALUATOR_MODEL,
        help=f"RAGAS judge model id (default: {EVALUATOR_MODEL})",
    )
    parser.add_argument(
        "--deepeval-judge-model",
        default=EVALUATOR_MODEL,
        help=f"DeepEval judge model id (default: {EVALUATOR_MODEL})",
    )
    parser.add_argument(
        "--deepeval-concurrency",
        type=int,
        default=6,
        help="DeepEval concurrent samples (default: 6 — well under OpenAI tier-1 cap)",
    )
    parser.add_argument(
        "--patronus-evaluator",
        default="judge",
        help="Patronus evaluator alias (e.g., judge, judge-small, judge-large)",
    )
    parser.add_argument(
        "--patronus-criteria",
        default="patronus:fuzzy-match",
        help="Patronus criteria/evaluator id (default: patronus:fuzzy-match)",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    cache_path = _pipeline_cache_path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    dataset = _load_dataset(limit=args.limit)
    questions = [r["question"] for r in dataset]
    ground_truths = [r["answer"] for r in dataset]

    # --- Pre-flight checks (skip when reusing cache; we already know it ran). ---
    qdrant_info: dict | None = None
    if not args.skip_pipeline:
        qdrant_info = _preflight_checks(args.collection, args.ragas_judge_model)

    # --- Reproducibility metadata embedded in pipeline cache. Lets two runs
    #     be PROVEN to share identical config post-hoc.
    run_metadata = {
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git": _git_state(),
        "settings": _settings_snapshot(),
        "qdrant_collection": args.collection,
        "qdrant_collection_info": qdrant_info,
        "ragas_judge_model": args.ragas_judge_model,
        "deepeval_judge_model": args.deepeval_judge_model,
        "deepeval_concurrency": args.deepeval_concurrency,
        "flush_every": args.flush_every,
        "n_samples_requested": len(dataset),
        "limit": args.limit,
        # Probe whether LLM Guard actually loaded — env says "1" but runtime
        # may disable it if optimum/ONNX backend is missing. Record the truth.
        "llm_guard_runtime_status": _llm_guard_runtime_status() if not args.skip_pipeline else "skipped",
    }

    # --- Pipeline phase (or load from cache) ---
    if args.skip_pipeline:
        if not cache_path.exists():
            print(f"ERROR: --skip-pipeline set but no cache at {cache_path}")
            sys.exit(1)
        logger.info(f"Loading pipeline cache from {cache_path}")
        cached = json.loads(cache_path.read_text())
        answers = cached["answers"]
        contexts = cached["contexts"]
        pipeline_time = cached.get("pipeline_time_seconds", 0.0)
    else:
        logger.info(f"Pipeline targeting Qdrant collection: {args.collection}")
        # Resume support: if a partial cache exists (complete=false) and
        # --resume-pipeline is set, reuse the already-completed answers.
        seed_answers: list[str] = []
        seed_contexts: list[list[str]] = []
        seed_elapsed = 0.0
        if args.resume_pipeline and cache_path.exists():
            cached = json.loads(cache_path.read_text())
            if cached.get("complete"):
                logger.info(f"--resume-pipeline: cache at {cache_path} is already complete; using --skip-pipeline path")
                answers = cached["answers"]
                contexts = cached["contexts"]
                pipeline_time = cached.get("pipeline_time_seconds", 0.0)
                seed_answers = answers  # signal "skip pipeline" downstream
            else:
                seed_answers = cached.get("answers", [])
                seed_contexts = cached.get("contexts", [])
                seed_elapsed = cached.get("pipeline_time_seconds", 0.0)
                n_done = min(len(seed_answers), len(seed_contexts), len(dataset))
                logger.info(f"--resume-pipeline: partial cache has {n_done}/{len(dataset)} done; resuming")

        # Only invoke the pipeline if we don't already have a complete cache.
        if not (args.resume_pipeline and cache_path.exists() and json.loads(cache_path.read_text()).get("complete")):
            start = time.time()
            answers, contexts = run_pipeline_phase(
                dataset,
                collection=args.collection,
                cache_path=cache_path,
                flush_every=args.flush_every,
                seed_answers=seed_answers,
                seed_contexts=seed_contexts,
                seed_elapsed_seconds=seed_elapsed,
                run_metadata=run_metadata,
            )
            pipeline_time = seed_elapsed + (time.time() - start)
            _write_cache_atomic(
                cache_path,
                questions=questions,
                answers=answers,
                contexts=contexts,
                pipeline_time_seconds=pipeline_time,
                complete=True,
                run_metadata=run_metadata,
            )
            logger.info(f"Pipeline done in {pipeline_time:.1f}s (incl. resumed time); cache at {cache_path}")

    # --- Scoring ---
    scores: dict = {"pipeline_time_seconds": round(pipeline_time, 1), "num_samples": len(dataset)}
    if not args.skip_ragas:
        start = time.time()
        scores["ragas"] = score_with_ragas(
            questions,
            answers,
            contexts,
            ground_truths,
            evaluator_model=args.ragas_judge_model,
        )
        scores["ragas_time_seconds"] = round(time.time() - start, 1)
        scores["ragas_judge_model"] = args.ragas_judge_model
    if not args.skip_deepeval:
        start = time.time()
        deepeval_output_path = output_path.with_suffix(".deepeval.json")
        scores["deepeval"] = score_with_deepeval(
            cache_path=cache_path,
            deepeval_output_path=deepeval_output_path,
            limit=args.limit,
            judge_model=args.deepeval_judge_model,
            concurrency=args.deepeval_concurrency,
        )
        scores["deepeval_time_seconds"] = round(time.time() - start, 1)
        scores["deepeval_judge_model"] = args.deepeval_judge_model

    patronus_pass_labels: list[bool] | None = None
    if not args.skip_patronus:
        start = time.time()
        patronus_output_path = output_path.with_suffix(".patronus.json")
        scores["patronus"] = score_with_patronus(
            cache_path=cache_path,
            patronus_output_path=patronus_output_path,
            limit=args.limit,
            patronus_evaluator=args.patronus_evaluator,
            patronus_criteria=args.patronus_criteria,
        )
        scores["patronus_time_seconds"] = round(time.time() - start, 1)
        scores["patronus_evaluator"] = args.patronus_evaluator
        scores["patronus_criteria"] = args.patronus_criteria
        # Best-effort pass labels for diagnostics if pass_rate extraction worked.
        if "patronus_fuzzy_match_pass_rate" in scores["patronus"]:
            # run_experiment currently returns only aggregate; per-sample labels are
            # tracked separately by scripts/score_patronus.py.
            patronus_pass_labels = None

    # --- Summary + persist ---
    scores["diagnostics"] = build_diagnostics(
        dataset=dataset,
        answers=answers,
        contexts=contexts,
        pass_labels=patronus_pass_labels,
    )
    output_path.write_text(json.dumps(scores, indent=2))
    print()
    print("=== FinanceBench Evaluation Results ===")
    print(f"n_samples: {len(dataset)}")
    if "ragas" in scores:
        print("RAGAS:")
        for k, v in scores["ragas"].items():
            print(f"  {k:20s} {v:.4f}")
    if "deepeval" in scores:
        print("DeepEval:")
        for k, v in scores["deepeval"].items():
            if isinstance(v, float):
                print(f"  {k:30s} {v:.4f}")
            elif v is None:
                print(f"  {k:30s} N/A")
            else:
                print(f"  {k:30s} {v}")
    if "patronus" in scores:
        print("Patronus:")
        for k, v in scores["patronus"].items():
            if isinstance(v, float):
                print(f"  {k:40s} {v:.4f}")
            else:
                print(f"  {k:40s} {v}")
    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()
