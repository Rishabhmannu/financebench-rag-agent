"""Sprint 7.9 Day 1 smoke: per-task model-tier downgrades.

For each candidate downgrade (4 total):
  1. Hallucination checker:    Sonnet 4.6 → Haiku 4.5
  2. Research-agent decompose: Sonnet 4.6 → gpt-4o-mini
  3. Research-agent sufficiency: Sonnet 4.6 → gpt-4o-mini
  4. Research-agent synthesize: Sonnet 4.6 → Haiku 4.5

Run 5 representative FB questions (mixed slice, mixed pass/fail) through the
graph with that one downgrade enabled. Compare to Day 16 voyage canonical
baseline (`financebench_pypdf_voyage_finance2.correctness.json`):
  - Did pass/fail flip?
  - Coarse answer-shape similarity (length, first 100 chars)
  - Per-question elapsed time

This is a smoke (n=5), not a decision gate. Day 2 runs n=30 dev-sets per
candidate to make the actual ship/no-ship call.

Run:
    EMBEDDING_PROVIDER=voyage \\
    EMBEDDING_MODEL=voyage-finance-2 \\
    EMBEDDING_DIMENSIONS=1024 \\
    python scripts/smoke_tier_downgrade.py --candidate all

Available --candidate values: hallucination, decompose, sufficiency, synthesize, all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RAG_COST_RUN_ID", "sprint_7_9_day1_tier_smoke")

from langchain_core.messages import HumanMessage  # noqa: E402

from src.config.settings import settings  # noqa: E402
from src.graph.builder import build_graph  # noqa: E402
from src.services.cost_tracker import CostTracker  # noqa: E402

FB_DATASET_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")
FB_COLLECTION = "financebench_corpus_pypdf_voyage_finance2"
BASELINE_PATH = Path(
    "tests/evaluation/eval_results/financebench_pypdf_voyage_finance2.correctness.json"
)

# 5 representative smoke fb_ids: mixed slice + mixed pass/fail vs Day 16 voyage
SMOKE_IDS = [
    "financebench_id_03029",  # lookup, baseline=PASS (voyage rescue)
    "financebench_id_00499",  # lookup, baseline=FAIL
    "financebench_id_01865",  # multi-hop, baseline=PASS
    "financebench_id_08135",  # calc, baseline=PASS
    "financebench_id_00807",  # calc, baseline=FAIL
]

# Each candidate downgrade: (settings attribute, target model)
CANDIDATES: dict[str, tuple[str, str]] = {
    "hallucination": ("HALLUCINATION_MODEL", "claude-haiku-4-5"),
    "decompose":     ("RESEARCH_AGENT_DECOMPOSE_MODEL", "gpt-4o-mini"),
    "sufficiency":   ("RESEARCH_AGENT_SUFFICIENCY_MODEL", "gpt-4o-mini"),
    "synthesize":    ("RESEARCH_AGENT_SYNTHESIZE_MODEL", "claude-haiku-4-5"),
}


def _load_baseline() -> dict[str, dict]:
    """Index Day 16 voyage canonical correctness output by fb_id."""
    data = json.loads(BASELINE_PATH.read_text())["per_sample"]
    return {r["fb_id"]: r for r in data}


def _load_gold() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(FB_DATASET_PATH) as f:
        for line in f:
            rec = json.loads(line)
            out[rec["financebench_id"]] = rec
    return out


async def _run_question(graph, question: str) -> dict:
    """Run one question through the full graph; return diagnostic dict."""
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
    return {
        "answer": final.get("generated_answer", ""),
        "elapsed_s": time.time() - t0,
    }


def _shape_similar(a: str, b: str, char_window: int = 80) -> bool:
    """Coarse heuristic: are the two answers structurally similar?
    Used as a quick eyeball — not a real semantic comparison.
    """
    if not a or not b:
        return False
    a_norm = a.strip().lower()
    b_norm = b.strip().lower()
    # Length within 30%
    len_close = (
        max(len(a_norm), len(b_norm)) > 0
        and abs(len(a_norm) - len(b_norm)) / max(len(a_norm), len(b_norm)) < 0.30
    )
    # Same opening
    head_match = a_norm[:char_window] == b_norm[:char_window]
    return len_close or head_match


async def _smoke_one_candidate(
    name: str, setting_attr: str, new_model: str, gold: dict, baseline: dict
) -> dict:
    """Run one candidate downgrade against the 5 smoke questions."""
    print(f"\n{'='*100}")
    print(f"CANDIDATE: {name}  ({setting_attr}: {getattr(settings, setting_attr)} → {new_model})")
    print(f"{'='*100}\n")

    # Override the setting *before* building the graph so the LLMFactory
    # picks up the new model on first instantiation. Settings is a Pydantic
    # singleton, so direct attribute assignment works (same pattern as
    # run_financebench.py for QDRANT_COLLECTION).
    original = getattr(settings, setting_attr)
    setattr(settings, setting_attr, new_model)
    settings.QDRANT_COLLECTION = FB_COLLECTION

    try:
        graph = build_graph()
        rows: list[dict] = []
        for fb_id in SMOKE_IDS:
            base = baseline.get(fb_id, {})
            g = gold[fb_id]
            print(f"  [{fb_id}] {g.get('company','?')} ({_classify(g['question'])}) — baseline={'PASS' if base.get('pass') else 'fail'}")
            try:
                r = await _run_question(graph, g["question"])
            except Exception as exc:
                print(f"    ERROR: {type(exc).__name__}: {str(exc)[:200]}")
                rows.append({"fb_id": fb_id, "error": str(exc)[:300]})
                continue
            new_answer = r["answer"]
            base_answer = base.get("answer", "")
            shape_match = _shape_similar(new_answer, base_answer)
            print(f"    new len={len(new_answer)}  base len={len(base_answer)}  shape_match={shape_match}  elapsed={r['elapsed_s']:.1f}s")
            print(f"    new: {new_answer[:120]!r}")
            rows.append({
                "fb_id": fb_id,
                "baseline_pass": base.get("pass"),
                "new_answer_head": new_answer[:200],
                "base_answer_head": base_answer[:200],
                "shape_match": shape_match,
                "len_delta_pct": (len(new_answer) - len(base_answer)) / max(len(base_answer), 1) * 100,
                "elapsed_s": r["elapsed_s"],
            })
        return {"candidate": name, "model_change": f"{original} → {new_model}", "rows": rows}
    finally:
        setattr(settings, setting_attr, original)


def _classify(question: str) -> str:
    from tests.evaluation.analysis_utils import classify_question_type
    return classify_question_type(question)


async def _amain(args) -> int:
    print(f"=== Sprint 7.9 Day 1 — tier-downgrade smoke ===")
    print(f"  EMBEDDING_PROVIDER:   {settings.EMBEDDING_PROVIDER}")
    print(f"  EMBEDDING_MODEL:      {settings.EMBEDDING_MODEL}")
    print(f"  EMBEDDING_DIMENSIONS: {settings.EMBEDDING_DIMENSIONS}")
    print(f"  Collection:           {FB_COLLECTION}")
    print(f"  Smoke set (n={len(SMOKE_IDS)}): {SMOKE_IDS}")

    if args.candidate == "all":
        targets = list(CANDIDATES.items())
    elif args.candidate in CANDIDATES:
        targets = [(args.candidate, CANDIDATES[args.candidate])]
    else:
        print(f"\nUnknown candidate: {args.candidate}. Choices: {list(CANDIDATES) + ['all']}")
        return 1

    if not BASELINE_PATH.exists():
        print(f"\nABORT: baseline not found at {BASELINE_PATH}")
        return 1
    baseline = _load_baseline()
    gold = _load_gold()

    results: list[dict] = []
    for name, (attr, model) in targets:
        result = await _smoke_one_candidate(name, attr, model, gold, baseline)
        results.append(result)

    # Summary
    print(f"\n{'='*100}")
    print("SMOKE SUMMARY")
    print(f"{'='*100}\n")
    print(f"  {'candidate':<14} {'model change':<55} {'errors':>7} {'shape_match':>12} {'avg_elapsed':>12}")
    for r in results:
        ok_rows = [row for row in r["rows"] if "error" not in row]
        n_err = len(r["rows"]) - len(ok_rows)
        n_shape = sum(1 for row in ok_rows if row.get("shape_match"))
        avg_elapsed = sum(row["elapsed_s"] for row in ok_rows) / max(len(ok_rows), 1)
        print(f"  {r['candidate']:<14} {r['model_change']:<55} {n_err:>7} {n_shape}/{len(ok_rows):<10} {avg_elapsed:>10.1f}s")

    out_path = Path("tests/evaluation/eval_results/sprint_7_9_day1_tier_smoke.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": results}, indent=2, default=str))
    print(f"\nWrote: {out_path}")
    print()
    print("Read: any candidate with non-zero `errors` is broken — DO NOT proceed to dev-set.")
    print("       `shape_match` is a coarse eyeball, not a quality gate. Day 2 dev-set is the gate.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sprint 7.9 Day 1 tier-downgrade smoke")
    parser.add_argument("--candidate", default="all",
                        help=f"Which candidate to smoke. Choices: {list(CANDIDATES) + ['all']}")
    args = parser.parse_args()
    with CostTracker.run(os.environ["RAG_COST_RUN_ID"]):
        return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
