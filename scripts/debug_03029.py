"""Sprint 7.9 Day 1 investigation: trace 03029 through the graph under each candidate downgrade.

Captures the full final-state dict so we can pinpoint where the empty-answer
pattern originates (router classification? agent path? synthesis? generator?).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RAG_COST_RUN_ID", "sprint_7_9_day1_debug_03029")

from langchain_core.messages import HumanMessage  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.graph.builder import build_graph  # noqa: E402

QUESTION_03029 = (
    "What is the FY2018 capital expenditure amount (in USD millions) for 3M? "
    "Give a response to the question by relying on the details shown in the "
    "cash flow statement."
)

CONFIGS = [
    ("baseline", {}),
    ("decompose=gpt-4o-mini", {"RESEARCH_AGENT_DECOMPOSE_MODEL": "gpt-4o-mini"}),
    ("synthesize=claude-haiku-4-5", {"RESEARCH_AGENT_SYNTHESIZE_MODEL": "claude-haiku-4-5"}),
    ("hallucination=claude-haiku-4-5", {"HALLUCINATION_MODEL": "claude-haiku-4-5"}),
    ("sufficiency=gpt-4o-mini", {"RESEARCH_AGENT_SUFFICIENCY_MODEL": "gpt-4o-mini"}),
]


async def _trace(label: str, overrides: dict) -> dict:
    print(f"\n{'=' * 100}")
    print(f"CONFIG: {label}")
    for k, v in overrides.items():
        print(f"  {k} = {v}")
    print(f"{'=' * 100}")

    # Snapshot + apply overrides
    snapshots: dict = {}
    for k, v in overrides.items():
        snapshots[k] = getattr(settings, k)
        setattr(settings, k, v)
    settings.QDRANT_COLLECTION = "financebench_corpus_pypdf_voyage_finance2"

    try:
        graph = build_graph()
        config = {
            "configurable": {
                "thread_id": str(uuid.uuid4()),
                "user": {"user_id": "dbg", "username": "dbg", "role": "admin", "exp": 9999999999},
                "hitl_enabled": False,
            },
        }
        initial = {
            "messages": [HumanMessage(content=QUESTION_03029)],
            "user_query": QUESTION_03029,
            "user_role": "admin",
            "user_id": "dbg",
        }
        final = await graph.ainvoke(initial, config=config)
        # Pull the diagnostic fields we care about
        snap = {
            "label": label,
            "router_intent": final.get("query_intent"),
            "query_complexity": final.get("query_complexity"),
            "agent_ran": bool(final.get("agent_synthesis")),
            "agent_turns_used": final.get("agent_turns_used"),
            "agent_sub_questions": final.get("agent_sub_questions"),
            "n_relevant_chunks": len(final.get("relevant_chunks") or []),
            "retrieval_fallback_used": final.get("retrieval_fallback_used"),
            "agent_synthesis_len": len(final.get("agent_synthesis") or ""),
            "agent_synthesis_head": (final.get("agent_synthesis") or "")[:200],
            "generated_answer_len": len(final.get("generated_answer") or ""),
            "generated_answer_head": (final.get("generated_answer") or "")[:200],
            "calculator_invoked": final.get("calculator_invoked"),
            "hallucination_check_passed": final.get("hallucination_check_passed"),
            "hallucination_explanation": (final.get("hallucination_explanation") or "")[:200],
            "hallucination_retry_count": final.get("hallucination_retry_count"),
        }
        for k, v in snap.items():
            if k == "label":
                continue
            print(f"  {k}: {v}")
        return snap
    except Exception as exc:
        print(f"  EXCEPTION: {type(exc).__name__}: {exc}")
        return {"label": label, "error": str(exc)}
    finally:
        for k, v in snapshots.items():
            setattr(settings, k, v)


async def main() -> int:
    print(f"=== Debug 03029 trace ===\n")
    print(f"  Q: {QUESTION_03029}\n")
    print(f"  Expected gold: $1,577 million\n")

    results = []
    for label, overrides in CONFIGS:
        snap = await _trace(label, overrides)
        results.append(snap)

    print(f"\n{'=' * 100}")
    print("SIDE-BY-SIDE SUMMARY")
    print(f"{'=' * 100}")
    print(f"  {'config':<35} {'cmplx':<8} {'agent':<6} {'chunks':>6} {'synth_len':>10} {'ans_len':>8} {'hallu_pass':>10}")
    for r in results:
        print(f"  {r.get('label',''):<35} "
              f"{str(r.get('query_complexity',''))[:7]:<8} "
              f"{str(r.get('agent_ran',''))[:5]:<6} "
              f"{r.get('n_relevant_chunks','?'):>6} "
              f"{r.get('agent_synthesis_len','?'):>10} "
              f"{r.get('generated_answer_len','?'):>8} "
              f"{str(r.get('hallucination_check_passed',''))[:9]:>10}")

    out = Path("tests/evaluation/eval_results/sprint_7_9_day1_debug_03029.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"runs": results}, indent=2, default=str))
    print(f"\nWrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
