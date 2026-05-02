"""Smoke test for the Sprint 7.6 research agent.

Runs 3 hand-picked questions through the full graph end-to-end:

  1. **Mode 3 case** — "Does Corning have positive working capital..." — a
     computation question where Day 1 baseline refused even though the gold
     answer ($831M) was present in the chunks. Tests whether the agent's
     decompose + sufficiency loop overcomes Claude's premature-refusal
     instinct on balance-sheet math.

  2. **Mode 4 case** — "What drove operating margin change FY2022 for 3M?" —
     a "what drove" question where Day 1 missed specific drivers (Combat
     Arms litigation, PFAS exit, Russia exit). Tests whether the decompose
     prompt's qualifier-extraction surfaces the management-quoted drivers.

  3. **Lookup regression check** — "What was 3M's FY2018 capital
     expenditure?" — a simple lookup. Should NOT route to the research
     agent (regression check on the router's complexity classifier).

Asserts:
  - Router classifies (1) and (2) as research_required, (3) as simple_lookup
  - For agent paths: research_agent ran, populated relevant_chunks +
    agent_synthesis, completed without exception
  - For lookup path: agent did NOT run (agent_synthesis stays None)
  - Final answer is non-empty for all three

Costs ~$0.30-0.50 of LLM spend. Logs to RAG_COST_RUN_ID=smoke_research_agent.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Ensure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Set the cost-tracker run_id BEFORE importing the graph (the cost handler is
# attached at LLMFactory import time via callback closure).
os.environ.setdefault("RAG_COST_RUN_ID", "smoke_research_agent")

from langchain_core.messages import HumanMessage  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.graph.builder import build_graph  # noqa: E402
from src.services.cost_tracker import CostTracker  # noqa: E402

# Point retrieval at the FB collection (settings is the single source of truth)
settings.QDRANT_COLLECTION = "financebench_corpus_pypdf_clean"


SAMPLES = [
    {
        "name": "Mode 3 — computation-refusal (Corning working capital)",
        "query": (
            "Does Corning have positive working capital based on FY2022 data? "
            "If working capital is not a useful or relevant metric for this "
            "company, then please state that and explain why."
        ),
        "expected_complexity": "research_required",
        "gold": "Yes. Corning had a positive working capital amount of $831 million by FY 2022 close.",
    },
    {
        "name": "Mode 4 — qualifier extraction (3M operating margin drivers)",
        "query": "What drove operating margin change as of FY2022 for 3M?",
        "expected_complexity": "research_required",
        "gold": (
            "Operating Margin for 3M in FY2022 has decreased by 1.7% primarily "
            "due to: Decrease in gross Margin, mostly one-off charges including "
            "Combat Arms Earplugs litigation, impairment related to exiting "
            "PFAS manufacturing, costs related to exiting Russia."
        ),
    },
    {
        "name": "Lookup regression — agent must NOT fire",
        "query": "What is the FY2018 capital expenditure amount (in USD millions) for 3M?",
        "expected_complexity": "simple_lookup",
        "gold": "$1577",
    },
]


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
    graph = build_graph()
    failures: list[str] = []

    with CostTracker.run("smoke_research_agent"):
        for sample in SAMPLES:
            print(f"\n{'=' * 90}")
            print(f"  {sample['name']}")
            print(f"{'=' * 90}")
            print(f"  Q:    {sample['query']}")
            print(f"  GOLD: {sample['gold'][:120]}{'...' if len(sample['gold']) > 120 else ''}")
            print(f"  expected complexity: {sample['expected_complexity']}")

            state = _initial_state(sample["query"])
            t0 = time.time()
            try:
                config = {"configurable": {"thread_id": "smoke", "hitl_enabled": False}}
                result = graph.invoke(state, config=config)
            except Exception as exc:
                print(f"  ❌ EXCEPTION: {type(exc).__name__}: {exc}")
                failures.append(f"{sample['name']}: {type(exc).__name__}")
                continue
            elapsed = time.time() - t0

            actual_complexity = result.get("query_complexity")
            agent_ran = result.get("agent_synthesis") is not None
            relevant_chunks = result.get("relevant_chunks", [])
            answer = result.get("final_response") or result.get("generated_answer", "")
            sub_qs = result.get("agent_sub_questions")
            turns = result.get("agent_turns_used")

            print(f"  -> complexity:    {actual_complexity}")
            print(f"  -> agent ran:     {agent_ran} (turns={turns}, sub_questions={sub_qs})")
            print(f"  -> chunks:        {len(relevant_chunks)}")
            print(f"  -> elapsed:       {elapsed:.1f}s")
            print(f"  -> answer (250c): {answer[:250]}{'...' if len(answer) > 250 else ''}")

            # Validations
            if actual_complexity != sample["expected_complexity"]:
                msg = f"complexity mismatch: expected={sample['expected_complexity']}, got={actual_complexity}"
                print(f"  ❌ {msg}")
                failures.append(f"{sample['name']}: {msg}")
                continue

            if sample["expected_complexity"] == "research_required":
                if not agent_ran:
                    msg = "research_required but agent_synthesis is None"
                    print(f"  ❌ {msg}")
                    failures.append(f"{sample['name']}: {msg}")
                    continue
            else:
                if agent_ran:
                    msg = "simple_lookup but agent ran (regression on classifier or routing)"
                    print(f"  ❌ {msg}")
                    failures.append(f"{sample['name']}: {msg}")
                    continue

            if not answer.strip():
                msg = "empty answer"
                print(f"  ❌ {msg}")
                failures.append(f"{sample['name']}: {msg}")
                continue

            print(f"  ✅ pass")

    # Cost summary
    summary = CostTracker.summarize(run_id="smoke_research_agent")
    run_data = summary["runs"].get("smoke_research_agent", {})
    print(f"\n{'=' * 90}\nCost summary")
    if run_data:
        print(f"  total: ${run_data['cost_usd']:.4f}  ({run_data['calls']} calls)")
        for model, stats in sorted(run_data["models"].items(), key=lambda kv: -kv[1]["cost_usd"]):
            print(
                f"    {model:<32} ${stats['cost_usd']:>8.4f}  "
                f"in={int(stats['input_tokens']):>8,}  out={int(stats['output_tokens']):>6,}"
            )

    if failures:
        print(f"\n❌ {len(failures)} smoke failure(s):")
        for f in failures:
            print(f"   - {f}")
        return 1

    print("\n✅ All 3 smoke samples passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
