"""Sprint 7.9 Day 1 investigation: trace 01865 (3M segment growth ex-M&A, multi-hop)
through the graph under each candidate downgrade.

This question routes through the research-agent (multi-hop), so the
decompose/sufficiency/synthesize model swaps SHOULD have visible effects.
The smoke flagged sufficiency=gpt-4o-mini as causing a refusal regression;
this script captures the full agent state to confirm whether that's real.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RAG_COST_RUN_ID", "sprint_7_9_day1_debug_01865")

from langchain_core.messages import HumanMessage  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.graph.builder import build_graph  # noqa: E402

QUESTION_01865 = (
    "If we exclude the impact of M&A, which segment has dragged down 3M's "
    "overall growth in 2022?"
)
GOLD = "The consumer segment shrunk by 0.9% organically."

CONFIGS = [
    ("baseline", {}),
    ("decompose=gpt-4o-mini", {"RESEARCH_AGENT_DECOMPOSE_MODEL": "gpt-4o-mini"}),
    ("sufficiency=gpt-4o-mini", {"RESEARCH_AGENT_SUFFICIENCY_MODEL": "gpt-4o-mini"}),
    ("synthesize=claude-haiku-4-5", {"RESEARCH_AGENT_SYNTHESIZE_MODEL": "claude-haiku-4-5"}),
    ("hallucination=claude-haiku-4-5", {"HALLUCINATION_MODEL": "claude-haiku-4-5"}),
]


async def _trace(label: str, overrides: dict) -> dict:
    print(f"\n{'=' * 100}")
    print(f"CONFIG: {label}")
    for k, v in overrides.items():
        print(f"  {k} = {v}")
    print(f"{'=' * 100}")

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
            "messages": [HumanMessage(content=QUESTION_01865)],
            "user_query": QUESTION_01865,
            "user_role": "admin",
            "user_id": "dbg",
        }
        final = await graph.ainvoke(initial, config=config)
        snap = {
            "label": label,
            "router_intent": final.get("query_intent"),
            "query_complexity": final.get("query_complexity"),
            "agent_ran": bool(final.get("agent_synthesis")),
            "agent_turns_used": final.get("agent_turns_used"),
            "agent_sub_questions": final.get("agent_sub_questions"),
            "n_relevant_chunks": len(final.get("relevant_chunks") or []),
            "agent_synthesis_len": len(final.get("agent_synthesis") or ""),
            "agent_synthesis_head": (final.get("agent_synthesis") or "")[:300],
            "generated_answer_len": len(final.get("generated_answer") or ""),
            "generated_answer_head": (final.get("generated_answer") or "")[:200],
            "hallucination_check_passed": final.get("hallucination_check_passed"),
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
    print(f"=== Debug 01865 trace ===\n")
    print(f"  Q: {QUESTION_01865}\n")
    print(f"  Gold: {GOLD}\n")
    print(f"  Day 16 baseline: PASS — answered 'Consumer segment, -0.9% organic'\n")
    print(f"  Day 1 smoke under sufficiency=gpt-4o-mini: refusal\n")

    results = []
    for label, overrides in CONFIGS:
        snap = await _trace(label, overrides)
        results.append(snap)

    print(f"\n{'=' * 100}")
    print("SIDE-BY-SIDE SUMMARY")
    print(f"{'=' * 100}")
    print(f"  {'config':<35} {'cmplx':<10} {'agent':<6} {'turns':>5} {'subQs':>5} {'chunks':>6} {'synth':>7} {'ans':>6}")
    for r in results:
        sub_q_count = len(r.get("agent_sub_questions") or [])
        print(f"  {r.get('label',''):<35} "
              f"{str(r.get('query_complexity',''))[:9]:<10} "
              f"{str(r.get('agent_ran',''))[:5]:<6} "
              f"{r.get('agent_turns_used','-')!s:>5} "
              f"{sub_q_count:>5} "
              f"{r.get('n_relevant_chunks','?'):>6} "
              f"{r.get('agent_synthesis_len','?'):>7} "
              f"{r.get('generated_answer_len','?'):>6}")

    out = Path("tests/evaluation/eval_results/sprint_7_9_day1_debug_01865.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"runs": results}, indent=2, default=str))
    print(f"\nWrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
