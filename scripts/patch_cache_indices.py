"""Surgical re-run of specific question indices in a FinanceBench pipeline cache.

Use cases:
  - A handful of questions failed mid-run due to a transient issue (connection
    error, MPS OOM, judge timeout) and were recorded as empty/refusal answers.
  - You want to fix them without re-running the entire 150-question pipeline.

This script:
  1. Loads the existing pipeline cache (`*.pipeline.json`).
  2. Builds the LangGraph pointed at the requested Qdrant collection.
  3. Re-runs the graph for the requested 0-based indices only.
  4. Atomically patches `answers[i]` and `contexts[i]` for those indices.
  5. Preserves the rest of the cache untouched.

After patching the cache, re-run the scoring scripts to refresh metrics:
    python scripts/score_deepeval.py --cache <cache> --output <…deepeval.json> --resume
    # NOTE: RAGAS scoring has no per-sample resume; if RAGAS scores need
    # refreshing for changed indices, re-run with `--skip-pipeline --skip-deepeval`
    # against the patched cache (RAGAS runs on the full set every time).

Usage:
    # Fix 3 specific indices (0-based)
    python scripts/patch_cache_indices.py \\
        --cache tests/evaluation/eval_results/financebench_docling_clean.pipeline.json \\
        --collection financebench_corpus_docling_clean \\
        --indices 45,46,47

    # Or by financebench_id (often easier than counting indices)
    python scripts/patch_cache_indices.py \\
        --cache tests/evaluation/eval_results/financebench_pypdf_clean.pipeline.json \\
        --collection financebench_corpus_pypdf_clean \\
        --fb-ids financebench_id_03029,financebench_id_04254

    # Auto-detect bad samples (refusal answer + empty context) and re-run them
    python scripts/patch_cache_indices.py \\
        --cache tests/evaluation/eval_results/financebench_docling_clean.pipeline.json \\
        --collection financebench_corpus_docling_clean \\
        --auto-detect-failures
"""

import argparse
import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path

from langchain_core.messages import HumanMessage
from tqdm import tqdm

from src.config.settings import settings
from src.graph.builder import build_graph

if settings.OPENAI_API_KEY and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY

# Same noise suppression as run_financebench.py so the tqdm bar stays readable.
for _n in [
    "httpx", "httpcore", "presidio-analyzer", "presidio-anonymizer",
    "openai", "langchain", "langchain_core", "langgraph",
    "qdrant_client", "py.warnings", "urllib3", "llm_guard",
]:
    logging.getLogger(_n).setLevel(logging.WARNING)
for _m in ["src.graph.nodes", "src.services"]:
    logging.getLogger(_m).setLevel(logging.WARNING)
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="qdrant_client")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FB_QA_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")

# Refusal phrases — must match the canonical text in
# `src/graph/nodes/terminal_nodes.py` (no-info / out-of-scope responses).
REFUSAL_MARKERS = [
    "couldn't find relevant information",
    "i couldn't find",
    "outside my knowledge",
    "out of scope",
    "unable to answer",
]


def _is_refusal_or_empty(answer: str) -> bool:
    if not answer or not answer.strip():
        return True
    al = answer.lower()
    return any(m in al for m in REFUSAL_MARKERS)


def _is_empty_context(ctx: list[str]) -> bool:
    if not ctx:
        return True
    return all(not c.strip() for c in ctx)


def _build_initial_state(question: str) -> dict:
    """Same initial state as run_financebench.py — admin role, no HITL."""
    return {
        "messages": [HumanMessage(content=question)],
        "user_id": "patch_cache",
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


def _atomic_write(cache_path: Path, payload: dict) -> None:
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(cache_path)


def _maybe_empty_mps_cache() -> None:
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Surgical re-run of specific cache indices.")
    parser.add_argument("--cache", required=True, help="Path to .pipeline.json cache to patch")
    parser.add_argument("--collection", required=True, help="Qdrant collection to query")
    parser.add_argument("--indices", default=None, help="Comma-separated 0-based indices to re-run (e.g. '45,46,47')")
    parser.add_argument("--fb-ids", default=None, help="Comma-separated financebench_id values to re-run")
    parser.add_argument(
        "--auto-detect-failures",
        action="store_true",
        help="Auto-detect indices where answer is a refusal AND context is empty (likely OOM/connection victims)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Just list the indices that would be re-run; don't actually run anything")
    args = parser.parse_args()

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

    # Resolve target indices (mutually-exclusive selectors, prefer the explicit ones).
    indices: list[int] = []
    if args.indices:
        indices = [int(s) for s in args.indices.split(",") if s.strip()]
    elif args.fb_ids:
        wanted = {s.strip() for s in args.fb_ids.split(",") if s.strip()}
        for i, rec in enumerate(fb_qa):
            if rec["financebench_id"] in wanted:
                indices.append(i)
        missing = wanted - {fb_qa[i]["financebench_id"] for i in indices}
        if missing:
            print(f"WARN: {len(missing)} fb_id(s) not found in cache: {sorted(missing)[:5]}...")
    elif args.auto_detect_failures:
        for i, (a, c) in enumerate(zip(answers, contexts)):
            if _is_refusal_or_empty(a) and _is_empty_context(c):
                indices.append(i)
        print(f"Auto-detected {len(indices)} likely-failed samples (refusal + empty context).")
    else:
        print("ERROR: must pass --indices, --fb-ids, or --auto-detect-failures")
        sys.exit(1)

    if not indices:
        print("Nothing to do; no indices selected.")
        return
    indices = sorted(set(indices))
    if any(i < 0 or i >= len(questions) for i in indices):
        print(f"ERROR: indices out of range [0, {len(questions)})")
        sys.exit(1)

    print(f"Cache:        {cache_path}")
    print(f"Collection:   {args.collection}")
    print(f"Indices:      {indices}")
    for i in indices:
        print(f"  [{i:3d}] {fb_qa[i]['financebench_id']} ({fb_qa[i]['company']}): {questions[i][:80]}")
    if args.dry_run:
        print("\n--dry-run set; exiting without running.")
        return
    print()

    # Override Qdrant collection for the duration of this run.
    original_collection = settings.QDRANT_COLLECTION
    settings.QDRANT_COLLECTION = args.collection
    n_patched = 0
    n_failed = 0
    try:
        graph = build_graph(checkpointer=None)
        pbar = tqdm(indices, desc="Patch", unit="q", ncols=100)
        for i in pbar:
            rec = fb_qa[i]
            pbar.set_postfix_str(f"[{i}] {rec['company'][:15]} | {questions[i][:30]}")
            state = _build_initial_state(questions[i])
            config = {
                "configurable": {"thread_id": f"fb_patch_{rec['financebench_id']}_{int(time.time())}"},
                "metadata": {"hitl_enabled": False},
            }
            try:
                result = graph.invoke(state, config=config)
                new_ans = result.get("final_response", "")
                new_chunks = [c["content"] for c in result.get("relevant_chunks", []) if "content" in c]
                # Only patch if the new run produced a non-empty result; otherwise
                # leave the prior cache entry alone (don't overwrite a useful prior
                # value with a fresh failure).
                if new_ans.strip() or new_chunks:
                    answers[i] = new_ans
                    contexts[i] = new_chunks if new_chunks else [""]
                    n_patched += 1
                    tqdm.write(f"  [PATCH {i}] {rec['financebench_id']}: {new_ans[:80]}")
                else:
                    tqdm.write(f"  [KEEP  {i}] {rec['financebench_id']}: re-run also returned empty; left cache unchanged")
            except Exception as e:
                n_failed += 1
                tqdm.write(f"  [FAIL  {i}] {rec['financebench_id']}: {type(e).__name__}: {str(e)[:100]}")
            _maybe_empty_mps_cache()
            # Atomic flush after every patch so partial progress is preserved.
            cache["answers"] = answers
            cache["contexts"] = contexts
            cache["last_patched_at"] = time.time()
            _atomic_write(cache_path, cache)
        pbar.close()
    finally:
        settings.QDRANT_COLLECTION = original_collection

    print()
    print(f"Patched: {n_patched}/{len(indices)} ({n_failed} re-run failures, rest no-op or unchanged)")
    print(f"Cache:   {cache_path}")
    if n_patched:
        print()
        print("Next steps to refresh scoring:")
        print(f"  python scripts/score_deepeval.py --cache {cache_path} \\")
        print(f"      --output {cache_path.with_suffix('.deepeval.json').as_posix().replace('.pipeline', '')} \\")
        print(f"      --limit {len(answers)}")
        print("  # DeepEval --resume mode skips clean priors but re-scores the patched indices")
        print("  # whose prior result was a refusal/error (since refusals score as errors anyway).")
        print("  #")
        print("  # If you want to refresh RAGAS too (no per-sample resume), re-run:")
        print(f"  python tests/evaluation/run_financebench.py \\")
        print(f"      --output {cache_path.as_posix().replace('.pipeline.json', '.json')} \\")
        print(f"      --collection {args.collection} \\")
        print("      --skip-pipeline --skip-deepeval")


if __name__ == "__main__":
    main()
