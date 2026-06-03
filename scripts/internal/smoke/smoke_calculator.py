"""Sprint 7.8 Day 18 smoke: calculator-tool integration on 5 known-failing calc questions.

Runs the full agentic graph against the voyage collection on 5 calc questions
that failed in Day 16 (voyage-only baseline). Prints per-question:
  - Gold answer
  - Whether the synthesizer emitted an arithmetic expression
  - The expression and the calculator-evaluated value
  - The agent's final answer
  - A coarse "looks right" eyeball hint by checking if the gold number appears
    in the final answer string (substring match — not a real judge, just a
    quick signal)

Run:
    EMBEDDING_PROVIDER=voyage \\
    EMBEDDING_MODEL=voyage-finance-2 \\
    EMBEDDING_DIMENSIONS=1024 \\
    python scripts/smoke_calculator.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RAG_COST_RUN_ID", "sprint_7_8_day18_calc_smoke")

from langchain_core.messages import HumanMessage  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.graph.builder import build_graph  # noqa: E402
from src.services.cost_tracker import CostTracker  # noqa: E402

FB_DATASET_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")
FB_COLLECTION = "financebench_corpus_pypdf_voyage_finance2"

SMOKE_IDS = [
    "financebench_id_03856",  # Adobe FY2017 op-cash-flow ratio (gold 0.83)
    "financebench_id_04103",  # General Mills FY2019 CCC (gold -3.7)
    "financebench_id_05915",  # CVS FY2018 fixed-asset turnover (gold 17.98)
    "financebench_id_01930",  # Amcor real sales change FY23 vs FY22 (narrative)
    "financebench_id_00216",  # Verizon FY22 quick ratio (gold ~0.54, yes/no)
]


def _load_gold() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(FB_DATASET_PATH) as f:
        for line in f:
            rec = json.loads(line)
            out[rec["financebench_id"]] = rec
    return out


def _extract_numbers(text: str) -> list[float]:
    """Pull plausible numbers from a freeform string. Only used for eyeball hints."""
    nums: list[float] = []
    for m in re.finditer(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text):
        s = m.group(0).replace(",", "")
        try:
            nums.append(float(s))
        except ValueError:
            pass
    return nums


def _looks_right(gold_answer: str, generated_answer: str) -> str:
    """Coarse eyeball — does the gold's primary number appear in the answer?"""
    gold_nums = _extract_numbers(gold_answer)
    if not gold_nums:
        return "(narrative — manual check)"
    gen_nums = _extract_numbers(generated_answer)
    for g in gold_nums:
        for h in gen_nums:
            if abs(g) > 1e-9 and abs((h - g) / g) < 0.02:  # 2% relative match
                return f"YES (gold {g} ~ gen {h})"
            if g == 0 and abs(h) < 0.05:
                return f"YES (gold 0, gen {h:.3f})"
    return f"NO (gold nums={gold_nums[:3]}, gen nums={gen_nums[:5]})"


async def _run_question(graph, gold_record: dict) -> dict:
    """Run a single question through the graph; return diagnostic dict."""
    question = gold_record["question"]
    config = {
        "configurable": {
            "thread_id": str(uuid.uuid4()),
            "user": {
                "user_id": "smoke",
                "username": "smoke",
                "role": "admin",
                "exp": 9999999999,
            },
            "hitl_enabled": False,
        },
    }
    initial = {
        "messages": [HumanMessage(content=question)],
        "user_query": question,
        "user_role": "admin",
        "user_id": "smoke",
    }
    t0 = time.time()
    final = await graph.ainvoke(initial, config=config)
    elapsed = time.time() - t0
    return {
        "fb_id": gold_record["financebench_id"],
        "company": gold_record.get("company"),
        "question": question,
        "gold": gold_record["answer"],
        "generated_answer": final.get("generated_answer", ""),
        "agent_ran": bool(final.get("agent_synthesis")),
        "calc_invoked": bool(final.get("calculator_invoked")),
        "calc_expression": final.get("calculator_expression"),
        "calc_value": final.get("calculator_value"),
        "calc_error": final.get("calculator_error"),
        "elapsed_s": elapsed,
    }


async def _amain() -> int:
    print("=" * 100)
    print("Sprint 7.8 Day 18 — calculator smoke")
    print("=" * 100)
    print(f"  EMBEDDING_PROVIDER:   {settings.EMBEDDING_PROVIDER}")
    print(f"  EMBEDDING_MODEL:      {settings.EMBEDDING_MODEL}")
    print(f"  EMBEDDING_DIMENSIONS: {settings.EMBEDDING_DIMENSIONS}")
    print(f"  Collection:           {FB_COLLECTION}")
    print()

    settings.QDRANT_COLLECTION = FB_COLLECTION  # see run_financebench.py:385
    gold = _load_gold()
    missing = [fb_id for fb_id in SMOKE_IDS if fb_id not in gold]
    if missing:
        print(f"ABORT: missing fb_ids: {missing}")
        return 1

    graph = build_graph()

    rows: list[dict] = []
    for fb_id in SMOKE_IDS:
        print(f"\n{'─' * 100}")
        print(f"[{fb_id}] {gold[fb_id].get('company')}")
        print(f"  Q: {gold[fb_id]['question'][:100]}")
        try:
            r = await _run_question(graph, gold[fb_id])
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            continue
        rows.append(r)
        print(f"  GOLD:    {r['gold'][:120]}")
        print(f"  AGENT:   ran={r['agent_ran']}, calc_invoked={r['calc_invoked']}, elapsed={r['elapsed_s']:.1f}s")
        if r["calc_invoked"]:
            print(f"  CALC:    `{r['calc_expression']}` = {r['calc_value']:.6g}")
        elif r["calc_expression"] is not None:
            print(f"  CALC:    REJECTED `{r['calc_expression']}` ({r['calc_error']})")
        else:
            print(f"  CALC:    not emitted by synthesizer")
        print(f"  ANSWER:  {r['generated_answer'][:200]}")
        print(f"  EYEBALL: {_looks_right(r['gold'], r['generated_answer'])}")

    print()
    print("=" * 100)
    print("Summary")
    print("=" * 100)
    n_calc = sum(1 for r in rows if r["calc_invoked"])
    n_calc_emitted = sum(1 for r in rows if r["calc_expression"])
    n_calc_rejected = sum(1 for r in rows if r["calc_expression"] and not r["calc_invoked"])
    print(f"  Questions run:           {len(rows)}/{len(SMOKE_IDS)}")
    print(f"  Synthesizer emitted expr: {n_calc_emitted}/{len(rows)}")
    print(f"  Calculator accepted:      {n_calc}/{len(rows)}")
    print(f"  Calculator rejected:      {n_calc_rejected}/{len(rows)}")

    out_path = Path("tests/evaluation/eval_results/sprint_7_8_day18_calc_smoke.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"rows": rows}, f, indent=2, default=str)
    print(f"\nWrote: {out_path}")
    return 0


def main() -> int:
    with CostTracker.run(os.environ["RAG_COST_RUN_ID"]):
        return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
