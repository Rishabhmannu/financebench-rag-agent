"""Day 3 wider smoke for the Sprint 7.6 research agent.

Runs 8 hand-picked questions through the full graph, scores each with the
same gpt-4o-mini correctness judge Day 1 used, and compares pass/fail
against the Day 1 baseline. The 8 samples are chosen from
financebench_pypdf_clean.review.json:

  - 3 Mode 3 cases (Day 1 refused but chunks were present — calc/multi-hop)
  - 3 Mode 4 cases (Day 1 attempted but failed — multi-hop / lookup)
  - 2 lookup regression checks (Day 1 passed — must NOT regress)

Cost ~$0.50–0.80, ~10–15 min. Decides whether to green-light the full
$10–15 Day 4 eval.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RAG_COST_RUN_ID", "smoke_research_agent_day3")

from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.graph.builder import build_graph  # noqa: E402
from src.services.cost_tracker import CostTracker, get_cost_handler  # noqa: E402

settings.QDRANT_COLLECTION = "financebench_corpus_pypdf_clean"

REVIEW_PATH = Path("tests/evaluation/eval_results/financebench_pypdf_clean.review.json")
TARGET_FB_IDS = [
    # Mode 3 — refused with chunks present (computation refusals on calc/multi-hop)
    "financebench_id_02987",   # Activision FY2019 fixed-asset turnover (calc)
    "financebench_id_04735",   # investment-banker calc question
    "financebench_id_03856",   # Adobe FY2017 operating cash flow ratio (calc)
    # Mode 4 — failed-with-chunks (synthesis errors)
    "financebench_id_00499",   # 3M capital-intensive lookup (qualifier)
    "financebench_id_01226",   # 3M operating margin drivers (multi-hop)
    "financebench_id_01865",   # 3M segment-drag (exclude M&A qualifier)
    # Lookup regression — must keep passing
    "financebench_id_04672",   # 3M FY2018 net PP&E (passed Day 1)
    "financebench_id_01858",   # 3M dividend stability (passed Day 1)
]


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
        "user_id": "smoke_user",
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


def main() -> int:
    review = json.loads(REVIEW_PATH.read_text())
    by_id = {r["fb_id"]: r for r in review["records"]}
    samples = [by_id[fb_id] for fb_id in TARGET_FB_IDS if fb_id in by_id]
    if len(samples) != len(TARGET_FB_IDS):
        missing = set(TARGET_FB_IDS) - set(by_id.keys())
        print(f"WARNING: {len(missing)} fb_ids not found in review: {missing}")

    graph = build_graph()
    rows: list[dict] = []

    with CostTracker.run("smoke_research_agent_day3"):
        for s in samples:
            fb_id = s["fb_id"]
            q = s["question"]
            gold = s["gold"] or ""
            day1_pass = bool(s.get("correctness_pass"))
            day1_answer = s.get("generated_answer") or ""

            print(f"\n{'=' * 100}")
            print(f"  {fb_id}  |  slice={s['slice']}  |  Day 1 pass={day1_pass}")
            print(f"  Q: {q[:120]}")
            print(f"  GOLD: {gold[:120]}")

            t0 = time.time()
            try:
                config = {"configurable": {"thread_id": f"smoke_{fb_id}", "hitl_enabled": False}}
                result = graph.invoke(_initial_state(q), config=config)
            except Exception as exc:
                print(f"  ❌ EXCEPTION: {type(exc).__name__}: {exc}")
                rows.append({"fb_id": fb_id, "slice": s["slice"], "day1_pass": day1_pass,
                             "day2_pass": False, "agent_ran": False, "exception": str(exc)})
                continue
            elapsed = time.time() - t0

            agent_ran = result.get("agent_synthesis") is not None
            answer = result.get("final_response") or result.get("generated_answer", "")
            complexity = result.get("query_complexity")

            day2_pass, judge_reason = _judge(q, gold, answer)

            delta = ""
            if day1_pass and not day2_pass:
                delta = "REGRESSION ⚠"
            elif not day1_pass and day2_pass:
                delta = "RESCUED ✅"
            elif day1_pass and day2_pass:
                delta = "stable"
            else:
                delta = "still failing"

            print(f"  → complexity={complexity}  agent_ran={agent_ran}  elapsed={elapsed:.1f}s")
            print(f"  → Day1 answer (200c): {day1_answer[:200]!r}")
            print(f"  → Day2 answer (200c): {answer[:200]!r}")
            print(f"  → judge: pass={day2_pass}  reason={judge_reason}")
            print(f"  → DELTA: {delta}")

            rows.append({
                "fb_id": fb_id,
                "slice": s["slice"],
                "complexity": complexity,
                "agent_ran": agent_ran,
                "day1_pass": day1_pass,
                "day2_pass": day2_pass,
                "delta": delta,
                "elapsed_s": round(elapsed, 1),
            })

    # Summary
    print(f"\n{'=' * 100}")
    print("Day 3 smoke summary")
    print(f"{'=' * 100}")
    print(f"  {'fb_id':<28s} {'slice':<10s} {'D1':<5s} {'D2':<5s} {'delta':<18s} {'agent':<6s} {'sec':<5s}")
    print(f"  {'-' * 80}")
    for r in rows:
        d1 = "PASS" if r["day1_pass"] else "fail"
        d2 = "PASS" if r["day2_pass"] else "fail"
        agent = "yes" if r["agent_ran"] else "no"
        print(
            f"  {r['fb_id']:<28s} {r['slice']:<10s} {d1:<5s} {d2:<5s} {r['delta']:<18s} "
            f"{agent:<6s} {r['elapsed_s']:<5}"
        )

    n_total = len(rows)
    n_d1_pass = sum(1 for r in rows if r["day1_pass"])
    n_d2_pass = sum(1 for r in rows if r["day2_pass"])
    n_rescued = sum(1 for r in rows if not r["day1_pass"] and r["day2_pass"])
    n_regressed = sum(1 for r in rows if r["day1_pass"] and not r["day2_pass"])

    print(f"\n  Day 1 pass rate on this sample: {n_d1_pass}/{n_total}  ({n_d1_pass / n_total:.0%})")
    print(f"  Day 2 pass rate on this sample: {n_d2_pass}/{n_total}  ({n_d2_pass / n_total:.0%})")
    print(f"  Rescued (D1 fail → D2 pass):    {n_rescued}")
    print(f"  Regressed (D1 pass → D2 fail):  {n_regressed}")

    # Cost
    summary = CostTracker.summarize(run_id="smoke_research_agent_day3")
    run_data = summary["runs"].get("smoke_research_agent_day3", {})
    print()
    if run_data:
        print(f"  Cost: ${run_data['cost_usd']:.4f}  ({run_data['calls']} LLM calls)")
        for model, stats in sorted(run_data["models"].items(), key=lambda kv: -kv[1]["cost_usd"]):
            print(f"    {model:<32} ${stats['cost_usd']:>8.4f}")

    if n_regressed > 0:
        print(f"\n  ⚠ {n_regressed} regression(s) — investigate before Day 4 full eval.")
        return 1
    if n_rescued >= 2:
        print(f"\n  ✅ {n_rescued} rescues, 0 regressions — Day 4 full eval is green-lit.")
        return 0
    print(f"\n  ⚠ Only {n_rescued} rescue(s) on Mode 3/4 cases. Iterate prompts before Day 4?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
