"""Run a stratified dev set through the agentic graph; compare to a baseline.

Used by Sprint 7.7 to gate the full 150-Q eval. Reads a dev-set spec
(list of fb_ids), runs each question through the graph against a target
Qdrant collection, scores with the gpt-4o-mini correctness judge, and
prints a per-question comparison vs the cited baseline.

Decision-gate output:
  - Pass count delta vs baseline (positive → green-light)
  - Regression count (questions that passed in baseline but fail here)
  - Rescue count (questions that failed in baseline but pass here)

Usage:
    EMBEDDING_MODEL=text-embedding-3-large EMBEDDING_DIMENSIONS=3072 \
    RAG_COST_RUN_ID=sprint_7_7_day6_devset \
    python scripts/run_devset.py \
      --devset tests/evaluation/eval_results/sprint_7_7_day6_devset.json \
      --collection financebench_corpus_pypdf_emb_large \
      --baseline tests/evaluation/eval_results/financebench_pypdf_agent.review.json \
      --output tests/evaluation/eval_results/sprint_7_7_day6_devset_emb_large.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RAG_COST_RUN_ID", "sprint_7_7_day6_devset")

from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.graph.builder import build_graph  # noqa: E402
from src.services.cost_tracker import CostTracker, get_cost_handler  # noqa: E402

FB_DATASET_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")


class _Verdict(BaseModel):
    passed: bool = Field(description="True iff the answer conveys the same factual content as the gold.")
    reason: str = Field(description="One sentence: what matched or what was wrong.")


_JUDGE_PROMPT = (
    "You are evaluating whether a generated answer correctly answers a financial question, "
    "given a gold reference answer extracted from the source document.\n\n"
    "Be strict. Mark PASS only if the generated answer conveys the same factual content as the gold. Mark FAIL for:\n"
    "- Wrong numbers, wrong units, wrong direction, refusal\n"
    "- Vague/non-specific response when gold is specific\n\n"
    "Allow minor formatting differences (e.g. '$1,577' vs '1577 million USD').\n\n"
    "Question: {question}\nGold answer: {gold}\nGenerated answer: {generated}"
)


def _judge(question: str, gold: str, generated: str) -> tuple[bool, str]:
    judge = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.0,
        api_key=settings.OPENAI_API_KEY,
        callbacks=[get_cost_handler()],
    )
    structured = judge.with_structured_output(_Verdict)
    try:
        v: _Verdict = structured.invoke(_JUDGE_PROMPT.format(question=question, gold=gold, generated=generated))
        return bool(v.passed), v.reason
    except Exception as exc:  # noqa: BLE001
        return False, f"judge_error: {type(exc).__name__}"


def _initial_state(query: str) -> dict:
    return {
        "messages": [HumanMessage(content=query)],
        "user_id": "devset_user",
        "user_role": "admin",
        "allowed_doc_types": [],
        "guardrail_status": "clean",
        "detected_pii_entities": [],
        "sanitized_query": query,
        "query_intent": "",
        "query_complexity": None,
        "target_company": None,
        "target_fiscal_year": None,
        "retrieved_chunks": [],
        "retrieval_query": "",
        "reranked_chunks": [],
        "candidate_diagnostics": [],
        "retrieval_evaluator_confidence": None,
        "retrieval_evaluator_decision": None,
        "relevant_chunks": [],
        "grading_results": [],
        "agent_synthesis": None,
        "agent_turns_used": None,
        "agent_sub_questions": None,
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


def _load_fb_dataset() -> dict[str, dict]:
    """Map fb_id → {question, answer (gold), company, doc_period, ...}."""
    by_id: dict[str, dict] = {}
    with FB_DATASET_PATH.open() as f:
        for line in f:
            rec = json.loads(line)
            by_id[rec["financebench_id"]] = rec
    return by_id


def _load_baseline(baseline_path: Path) -> dict[str, dict]:
    """Map fb_id → {correctness_pass, generated_answer, slice}."""
    payload = json.loads(baseline_path.read_text())
    out: dict[str, dict] = {}
    for r in payload.get("records", []):
        out[r["fb_id"]] = {
            "correctness_pass": r.get("correctness_pass"),
            "generated_answer": r.get("generated_answer", ""),
            "slice": r.get("slice"),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a dev-set through the agentic graph; compare to a baseline.")
    parser.add_argument("--devset", type=Path, required=True, help="Path to dev-set spec JSON (with fb_ids list)")
    parser.add_argument("--collection", required=True, help="Qdrant collection to query against")
    parser.add_argument("--baseline", type=Path, required=True, help="Baseline review.json for comparison")
    parser.add_argument("--output", type=Path, required=True, help="Output JSON path for dev-set results")
    parser.add_argument("--cost-run-id", default=None, help="Override RAG_COST_RUN_ID (for cost attribution)")
    args = parser.parse_args()

    if args.cost_run_id:
        os.environ["RAG_COST_RUN_ID"] = args.cost_run_id

    # Point the runtime at the target collection
    settings.QDRANT_COLLECTION = args.collection
    print(f"Settings overridden:")
    print(f"  EMBEDDING_MODEL:      {settings.EMBEDDING_MODEL}")
    print(f"  EMBEDDING_DIMENSIONS: {settings.EMBEDDING_DIMENSIONS}")
    print(f"  QDRANT_COLLECTION:    {settings.QDRANT_COLLECTION}")
    print()

    devset = json.loads(args.devset.read_text())
    fb_ids = devset["fb_ids"]
    fb_by_id = _load_fb_dataset()
    baseline_by_id = _load_baseline(args.baseline)

    graph = build_graph()
    rows: list[dict] = []
    cost_run_id = os.environ.get("RAG_COST_RUN_ID", "sprint_7_7_day6_devset")

    with CostTracker.run(cost_run_id):
        for i, fb_id in enumerate(fb_ids, 1):
            if fb_id not in fb_by_id:
                print(f"  [{i}/{len(fb_ids)}] {fb_id}: MISSING in FB dataset, skipping")
                continue
            rec = fb_by_id[fb_id]
            base = baseline_by_id.get(fb_id, {})
            q = rec["question"]
            gold = rec.get("answer", "") or ""

            print(f"\n{'=' * 100}")
            print(f"  [{i}/{len(fb_ids)}] {fb_id} | slice={base.get('slice', '?')} | baseline_pass={base.get('correctness_pass')}")
            print(f"  Q: {q[:120]}")
            print(f"  GOLD: {gold[:120]}")

            t0 = time.time()
            try:
                config = {"configurable": {"thread_id": f"devset_{fb_id}", "hitl_enabled": False}}
                result = graph.invoke(_initial_state(q), config=config)
            except Exception as exc:  # noqa: BLE001
                print(f"  ❌ EXCEPTION: {type(exc).__name__}: {exc}")
                rows.append({
                    "fb_id": fb_id, "slice": base.get("slice"),
                    "baseline_pass": base.get("correctness_pass"),
                    "devset_pass": False,
                    "exception": str(exc),
                })
                continue
            elapsed = time.time() - t0

            answer = result.get("final_response") or result.get("generated_answer", "")
            agent_ran = result.get("agent_synthesis") is not None
            n_chunks = len(result.get("relevant_chunks", []))
            devset_pass, judge_reason = _judge(q, gold, answer)

            base_pass = bool(base.get("correctness_pass"))
            if base_pass and not devset_pass:
                delta = "REGRESSION ⚠"
            elif not base_pass and devset_pass:
                delta = "RESCUED ✅"
            elif base_pass:
                delta = "stable"
            else:
                delta = "still failing"

            print(f"  → agent_ran={agent_ran}  n_chunks={n_chunks}  elapsed={elapsed:.1f}s")
            print(f"  → answer (200c): {answer[:200]!r}")
            print(f"  → judge: {devset_pass} ({judge_reason})")
            print(f"  → DELTA: {delta}")

            rows.append({
                "fb_id": fb_id,
                "slice": base.get("slice"),
                "agent_ran": agent_ran,
                "n_chunks": n_chunks,
                "elapsed_s": round(elapsed, 1),
                "baseline_pass": base_pass,
                "baseline_answer": base.get("generated_answer", ""),
                "devset_pass": devset_pass,
                "devset_answer": answer,
                "judge_reason": judge_reason,
                "delta": delta,
            })

    # Aggregate
    n = len(rows)
    n_baseline_pass = sum(1 for r in rows if r["baseline_pass"])
    n_devset_pass = sum(1 for r in rows if r["devset_pass"])
    n_rescued = sum(1 for r in rows if not r["baseline_pass"] and r["devset_pass"])
    n_regressed = sum(1 for r in rows if r["baseline_pass"] and not r["devset_pass"])
    by_slice: dict[str, dict] = {}
    for r in rows:
        s = r["slice"] or "unknown"
        b = by_slice.setdefault(s, {"n": 0, "baseline_pass": 0, "devset_pass": 0})
        b["n"] += 1
        b["baseline_pass"] += int(r["baseline_pass"])
        b["devset_pass"] += int(r["devset_pass"])

    summary = {
        "devset": str(args.devset),
        "collection": args.collection,
        "baseline": str(args.baseline),
        "n_total": n,
        "n_baseline_pass": n_baseline_pass,
        "n_devset_pass": n_devset_pass,
        "n_rescued": n_rescued,
        "n_regressed": n_regressed,
        "delta_pass_count": n_devset_pass - n_baseline_pass,
        "by_slice": by_slice,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))

    # Print summary table
    print(f"\n{'=' * 100}")
    print("Dev-set summary")
    print(f"{'=' * 100}")
    print(f"  {'fb_id':<28s} {'slice':<10s} {'base':<5s} {'dev':<5s} {'delta':<18s} {'agent':<6s} {'sec':<5s}")
    print(f"  {'-' * 80}")
    for r in rows:
        b = "PASS" if r["baseline_pass"] else "fail"
        d = "PASS" if r["devset_pass"] else "fail"
        agent = "yes" if r.get("agent_ran") else "no"
        print(
            f"  {r['fb_id']:<28s} {(r['slice'] or '?'):<10s} {b:<5s} {d:<5s} {r['delta']:<18s} "
            f"{agent:<6s} {r.get('elapsed_s', 0):<5}"
        )

    print(f"\n  baseline pass: {n_baseline_pass}/{n}  ({n_baseline_pass / n:.1%})") if n else None
    print(f"  devset pass:   {n_devset_pass}/{n}  ({n_devset_pass / n:.1%})")
    print(f"  rescued:       {n_rescued}")
    print(f"  regressed:     {n_regressed}")
    print(f"  net delta:     {summary['delta_pass_count']:+d} passes")

    print(f"\n  per-slice baseline → devset:")
    for s, b in by_slice.items():
        print(f"    {s:<10s}  {b['baseline_pass']}/{b['n']} → {b['devset_pass']}/{b['n']}  (delta {b['devset_pass'] - b['baseline_pass']:+d})")

    # Cost
    summary_cost = CostTracker.summarize(run_id=cost_run_id)
    run_data = summary_cost["runs"].get(cost_run_id, {})
    print()
    if run_data:
        print(f"  Cost: ${run_data['cost_usd']:.4f}  ({run_data['calls']} LLM calls)")
        for model, stats in sorted(run_data["models"].items(), key=lambda kv: -kv[1]["cost_usd"]):
            print(f"    {model:<32} ${stats['cost_usd']:>8.4f}")

    # Decision gate
    print(f"\n{'=' * 100}")
    print("Decision gate")
    print(f"{'=' * 100}")
    if n_regressed >= 3:
        print(f"  ❌ ABORT: {n_regressed} regression(s) — embedding upgrade is hurting more than helping.")
        return 1
    if summary["delta_pass_count"] < 0:
        print(f"  ❌ ABORT: net delta {summary['delta_pass_count']:+d} (devset pass count below baseline).")
        return 1
    if summary["delta_pass_count"] == 0 and n_rescued == 0:
        print(f"  ⚠ FLAT: no rescues, no regressions. Embedding upgrade is a no-op. Skip full eval.")
        return 1
    if summary["delta_pass_count"] >= 2 and n_regressed <= 1:
        print(f"  ✅ GREEN: net +{summary['delta_pass_count']} passes ({n_rescued} rescues, {n_regressed} regressions). Proceed to full 150-Q eval.")
        return 0
    print(f"  ⚠ MARGINAL: net +{summary['delta_pass_count']} ({n_rescued} rescues, {n_regressed} regressions). Borderline; user judgment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
