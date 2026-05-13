"""Sprint 7.14 Phase 1 — judge evaluator.

Evaluates candidate correctness-judges against the hand-labeled calibration
set. Computes Cohen's Kappa, FPR (especially on adversarial cases), FNR, F1,
and test-retest reliability. Outputs per-judge scorecard + shipping-gate
verdict.

Candidates:
  baseline_gpt4omini    — current production judge (run_devset.py prompt)
  v2_gpt4omini_improved — gpt-4o-mini + improved prompt with 3 calibration rules
  v3_sonnet             — Sonnet 4.6 + improved prompt
  v4_opus               — Opus 4.7 + improved prompt
  v5_consensus_3judge   — Sonnet + gpt-4o-mini + Opus majority vote

Shipping gates:
  κ ≥ 0.75
  FPR_adversarial ≤ 5%
  FNR < baseline's FNR
  test-retest disagreement < 5%
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

CALIB_JSONL = Path("tests/evaluation/judge_calibration_v1.jsonl")
HOLDOUT_JSONL = Path("tests/evaluation/judge_calibration_v1_holdout.jsonl")
OUTPUT_JSON = Path("tests/evaluation/eval_results/judge_eval_v1.json")
TEST_RETEST_N = 20
TEST_RETEST_RUNS = 3
PARALLEL_PER_JUDGE = 8

# Baseline prompt — copied from scripts/run_devset.py (current production judge)
BASELINE_PROMPT = """You are evaluating whether a generated answer correctly answers a financial question, given a gold reference answer extracted from the source document.

Be strict. Mark PASS only if the generated answer conveys the same factual content as the gold. Mark FAIL for:
- Wrong numbers, wrong units, wrong direction, refusal
- Vague/non-specific response when gold is specific

Allow minor formatting differences (e.g. '$1,577' vs '1577 million USD').

Question: {question}
Gold answer: {gold}
Generated answer: {generated}"""


# Improved prompt v2 — encodes the 5 calibration rules surfaced by the v1 eval.
# Three of the rules come from Sprint 7.14 Phase 1 v1 results showing Sonnet was
# too lenient on (a) metric substitution, (b) partial coverage of multi-item gold,
# (c) gold-provides-both-metric-AND-value cases. Plus the bottom-line-wins rule
# to handle adversarial corruptions that leave supporting math intact.
IMPROVED_PROMPT = """You are a strict but fair grader for a financial Q&A system. Given a question, a gold reference answer, and a generated system answer, decide whether the system answer correctly answers the question.

Output PASS if the system answer conveys the same factual content as the gold answer. Allow:
- Rounding differences when values are numerically identical at different precision (5.43% ↔ 5.4%; 1.3315 ↔ 1.33; 22.9% ↔ 23%). Do NOT allow when values differ in the second-significant digit or by more than ~2% (e.g., $70M vs $77.78M is FAIL — that's not rounding, it's a different value).
- Different units expressing the same value (-1.53% ↔ -0.02 decimal; $1,577M ↔ 1577 million USD; 14.8% tax benefit ↔ -14.76% effective rate).
- Different phrasing for the same factual claim (Las Vegas Strip Resorts ↔ Las Vegas resorts; "approximately X" ↔ X).
- Extra context, computation steps, or caveats — judge based on the FINAL ASSERTED ANSWER (bottom line), even if other parts of the answer give a different value.
- Partial answers where the MAIN ASSERTED ANSWER matches gold AND the gold doesn't list multiple required items, even if minor subordinate details (e.g., prior ownership history) are omitted.

Output FAIL if any of:
- The asserted numeric value differs materially from gold (>2% or different significant digits beyond rounding).
- **DIFFERENT METRIC**: The system reports a different metric than gold asks for. **A coincidental number match does NOT pass.** Examples:
    - Gold asks "cash dividends paid" but system gives "dividends declared" with the same $ amount → FAIL.
    - Gold asks "highest by absolute revenue" but system gives "highest by growth rate" → FAIL.
    - Gold asks "Q2 net income" but system gives "six-month pre-tax income" → FAIL.
- **METRIC+VALUE BOTH REQUIRED**: If the gold answer provides BOTH a metric name AND a specific value (e.g., "Corporate & Investment Bank. Its net income was $3725 million"), the system must explicitly state BOTH. Naming the right entity but giving a wrong/different value is FAIL.
- **ALL ITEMS REQUIRED**: If the gold lists multiple specific items (legal cases, drivers, acquisitions, segments) and the question asks for them, the system must cover ALL items for PASS. Covering most-but-not-all is FAIL. Example: gold lists 3 legal-battle areas; system covers 2 of 3 → FAIL.
- The system refuses to answer ("I don't have enough information", "cannot determine") when gold provides a definite answer — including when the correct answer is "none" / "zero" / "no change".
- The system asserts the opposite yes/no, or opposite trend direction (increase vs decrease).
- The system substantively misses the main factual claim of the gold answer.

**BOTTOM-LINE RULE**: For numeric questions, the SYSTEM'S FINAL ASSERTED ANSWER (typically in a "Bottom line:" section or final summary sentence) is the value being judged. If the system's bottom-line numeric answer disagrees with its own supporting calculation (e.g., the calculation derives 0.80 but the bottom line says 4.57), the BOTTOM-LINE value is what gets judged against gold. Do not "rescue" a wrong bottom line by reading past it to the supporting math.

Question: {question}

Gold answer: {gold}

Generated answer: {generated}

Decide and explain your reasoning in one sentence.
"""


class JudgeVerdict(BaseModel):
    passed: bool = Field(description="True iff the system answer correctly answers the question per the gold.")
    reason: str = Field(description="One-sentence explanation of the verdict.")


@dataclass
class JudgeCandidate:
    name: str
    invoke: Callable[[dict], dict]  # record -> {"passed": bool, "reason": str}
    description: str
    cost_per_call_usd: float
    is_consensus: bool = False


# ---------------------------------------------------------------------------
# Judge invocation helpers
# ---------------------------------------------------------------------------

def _make_openai_judge(model: str, prompt_template: str, temperature: float = 0.0):
    llm = ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        max_retries=2,
    )
    structured = llm.with_structured_output(JudgeVerdict)
    def invoke(rec):
        prompt = prompt_template.format(
            question=rec["question"], gold=rec["gold"], generated=rec["system_answer"],
        )
        try:
            v: JudgeVerdict = structured.invoke([HumanMessage(content=prompt)])
            return {"passed": bool(v.passed), "reason": v.reason}
        except Exception as e:
            return {"passed": False, "reason": f"judge_error: {type(e).__name__}: {str(e)[:200]}"}
    return invoke


# Opus 4.7 rejects the `temperature` param (deprecated for that model).
# Mirrors src/services/llm_factory.py:_ANTHROPIC_NO_TEMPERATURE_MODELS.
_ANTHROPIC_NO_TEMPERATURE_MODELS = {"claude-opus-4-7"}


def _make_anthropic_judge(model: str, prompt_template: str, temperature: float = 0.0):
    kwargs = {
        "model": model,
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "max_tokens": 512,
    }
    if model not in _ANTHROPIC_NO_TEMPERATURE_MODELS:
        kwargs["temperature"] = temperature
    llm = ChatAnthropic(**kwargs)
    structured = llm.with_structured_output(JudgeVerdict)
    def invoke(rec):
        prompt = prompt_template.format(
            question=rec["question"], gold=rec["gold"], generated=rec["system_answer"],
        )
        try:
            v: JudgeVerdict = structured.invoke([HumanMessage(content=prompt)])
            return {"passed": bool(v.passed), "reason": v.reason}
        except Exception as e:
            return {"passed": False, "reason": f"judge_error: {type(e).__name__}: {str(e)[:200]}"}
    return invoke


def _make_consensus_judge(member_invokes: list[Callable]):
    """Majority vote across N judges (N should be odd). PASS iff > N/2 say PASS."""
    def invoke(rec):
        votes = []
        reasons = []
        for inv in member_invokes:
            r = inv(rec)
            votes.append(r["passed"])
            reasons.append(r["reason"])
        n_pass = sum(votes)
        passed = n_pass > len(votes) // 2
        reason = f"consensus {n_pass}/{len(votes)} PASS | " + " ; ".join(f"({i}) {r[:80]}" for i, r in enumerate(reasons))
        return {"passed": passed, "reason": reason[:600]}
    return invoke


# ---------------------------------------------------------------------------
# Per-judge runner
# ---------------------------------------------------------------------------

def run_judge(candidate: JudgeCandidate, records: list[dict],
              parallelism: int = PARALLEL_PER_JUDGE) -> list[dict]:
    """Run candidate on every record. Returns list of {id, passed, reason}."""
    out: list[dict | None] = [None] * len(records)
    print(f"  [{candidate.name}] running on {len(records)} records (parallel={parallelism})...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(candidate.invoke, rec): i for i, rec in enumerate(records)}
        n_done = 0
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                out[i] = fut.result()
            except Exception as e:
                out[i] = {"passed": False, "reason": f"future_error: {type(e).__name__}: {e}"}
            n_done += 1
            if n_done % 20 == 0:
                print(f"    [{candidate.name}] {n_done}/{len(records)} done ({time.time() - t0:.0f}s)")
    return [{"id": rec["id"], "passed": v["passed"], "reason": v["reason"]}
            for rec, v in zip(records, out)]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def cohen_kappa(human: list[int], judge: list[int]) -> float:
    """Binary Cohen's κ. Inputs are 0/1 lists (PASS=1, FAIL=0)."""
    n = len(human)
    if n == 0:
        return 0.0
    obs = sum(1 for h, j in zip(human, judge) if h == j) / n
    p_h = sum(human) / n
    p_j = sum(judge) / n
    p_e = p_h * p_j + (1 - p_h) * (1 - p_j)
    if p_e >= 0.999:
        return 1.0 if obs >= 0.999 else 0.0
    return (obs - p_e) / (1 - p_e)


def compute_metrics(records: list[dict], verdicts: list[dict]) -> dict:
    """Compute all metrics. records[i].human_label is ground truth; verdicts[i].passed is judge's call."""
    assert len(records) == len(verdicts)
    human = [1 if r["human_label"] == "PASS" else 0 for r in records]
    judge = [1 if v["passed"] else 0 for v in verdicts]

    tp = sum(1 for h, j in zip(human, judge) if h == 1 and j == 1)
    tn = sum(1 for h, j in zip(human, judge) if h == 0 and j == 0)
    fp = sum(1 for h, j in zip(human, judge) if h == 0 and j == 1)
    fn = sum(1 for h, j in zip(human, judge) if h == 1 and j == 0)
    n = tp + tn + fp + fn

    accuracy = (tp + tn) / n if n else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    fnr = fn / (fn + tp) if (fn + tp) else 0.0

    kappa = cohen_kappa(human, judge)

    # Adversarial-specific FPR — judges must not pass adversarial corruptions
    adv_indices = [i for i, r in enumerate(records) if r.get("is_adversarial")]
    if adv_indices:
        n_adv = len(adv_indices)
        n_adv_passed = sum(1 for i in adv_indices if verdicts[i]["passed"])
        fpr_adv = n_adv_passed / n_adv
    else:
        n_adv = 0
        n_adv_passed = 0
        fpr_adv = 0.0

    return {
        "n": n,
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "fpr": round(fpr, 4),
        "fnr": round(fnr, 4),
        "kappa": round(kappa, 4),
        "fpr_adversarial": round(fpr_adv, 4),
        "n_adversarial": n_adv,
        "n_adversarial_passed": n_adv_passed,
    }


def compute_per_stratum(records: list[dict], verdicts: list[dict]) -> dict:
    """Per-stratum confusion matrix."""
    by_stratum: dict[str, dict] = {}
    for r, v in zip(records, verdicts):
        s = r["stratum"]
        d = by_stratum.setdefault(s, {"n": 0, "tp": 0, "tn": 0, "fp": 0, "fn": 0})
        d["n"] += 1
        human_pass = r["human_label"] == "PASS"
        judge_pass = v["passed"]
        if human_pass and judge_pass: d["tp"] += 1
        elif not human_pass and not judge_pass: d["tn"] += 1
        elif not human_pass and judge_pass: d["fp"] += 1
        else: d["fn"] += 1
    return by_stratum


# ---------------------------------------------------------------------------
# Test-retest
# ---------------------------------------------------------------------------

def test_retest(candidate: JudgeCandidate, records: list[dict],
                n_subset: int = TEST_RETEST_N, n_runs: int = TEST_RETEST_RUNS,
                seed: int = 42) -> dict:
    """Run candidate on a random subset N_runs times. Return disagreement rate."""
    rng = random.Random(seed)
    subset = rng.sample(records, min(n_subset, len(records)))
    print(f"  [{candidate.name}] test-retest: {len(subset)} records × {n_runs} runs...")
    all_runs: list[list[bool]] = []
    for run_idx in range(n_runs):
        verdicts = run_judge(candidate, subset, parallelism=PARALLEL_PER_JUDGE)
        all_runs.append([v["passed"] for v in verdicts])
    # Disagreement: fraction of records where verdicts disagree across runs
    n_disagree = 0
    for i in range(len(subset)):
        votes = {run[i] for run in all_runs}
        if len(votes) > 1:
            n_disagree += 1
    return {
        "n_subset": len(subset),
        "n_runs": n_runs,
        "n_disagree": n_disagree,
        "disagreement_rate": round(n_disagree / len(subset), 4),
    }


# ---------------------------------------------------------------------------
# Shipping gates
# ---------------------------------------------------------------------------

def check_gates(metrics: dict, test_retest: dict, baseline_fnr: float) -> dict:
    gates = {
        "kappa_ge_0.75": metrics["kappa"] >= 0.75,
        "fpr_adversarial_le_0.05": metrics["fpr_adversarial"] <= 0.05,
        "fnr_lt_baseline": metrics["fnr"] < baseline_fnr,
        "test_retest_lt_0.05": test_retest["disagreement_rate"] < 0.05,
    }
    gates["all_pass"] = all(v for k, v in gates.items() if k != "all_pass")
    return gates


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def build_candidates() -> list[JudgeCandidate]:
    base_gpt = _make_openai_judge("gpt-4o-mini", BASELINE_PROMPT)
    v2_gpt = _make_openai_judge("gpt-4o-mini", IMPROVED_PROMPT)
    v3_sonnet = _make_anthropic_judge("claude-sonnet-4-6", IMPROVED_PROMPT)
    v4_opus = _make_anthropic_judge("claude-opus-4-7", IMPROVED_PROMPT)
    v5_consensus = _make_consensus_judge([v2_gpt, v3_sonnet, v4_opus])

    return [
        JudgeCandidate("baseline_gpt4omini", base_gpt,
                       "Production baseline: gpt-4o-mini + current correctness prompt",
                       cost_per_call_usd=0.0001),
        JudgeCandidate("v2_gpt4omini_improved", v2_gpt,
                       "gpt-4o-mini + improved prompt (3 calibration rules + numeric/refusal/sign handling)",
                       cost_per_call_usd=0.0002),
        JudgeCandidate("v3_sonnet", v3_sonnet,
                       "Sonnet 4.6 + improved prompt",
                       cost_per_call_usd=0.003),
        JudgeCandidate("v4_opus", v4_opus,
                       "Opus 4.7 + improved prompt",
                       cost_per_call_usd=0.015),
        JudgeCandidate("v5_consensus_3judge", v5_consensus,
                       "Majority vote: gpt-4o-mini-improved + Sonnet + Opus",
                       cost_per_call_usd=0.018, is_consensus=True),
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--calib", type=Path, default=CALIB_JSONL)
    parser.add_argument("--holdout", type=Path, default=HOLDOUT_JSONL)
    parser.add_argument("--output", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--candidates", nargs="+", default=None,
                        help="Subset of candidate names to run. Default: all.")
    parser.add_argument("--skip-test-retest", action="store_true",
                        help="Skip the 20×3 reliability runs (saves ~3× cost on stable judges).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap calibration records (smoke mode).")
    args = parser.parse_args()

    print("loading calibration + holdout...")
    calib = [json.loads(l) for l in open(args.calib) if l.strip()]
    holdout = [json.loads(l) for l in open(args.holdout) if l.strip()]
    if args.limit:
        calib = calib[: args.limit]
    print(f"  calibration: {len(calib)} records  (PASS={sum(1 for r in calib if r['human_label']=='PASS')}, FAIL={sum(1 for r in calib if r['human_label']=='FAIL')})")
    print(f"  holdout: {len(holdout)} records")

    all_candidates = build_candidates()
    if args.candidates:
        all_candidates = [c for c in all_candidates if c.name in args.candidates]
        print(f"  filtered to {len(all_candidates)} candidates: {[c.name for c in all_candidates]}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    t_start = time.time()
    baseline_fnr = None  # set after baseline runs

    for candidate in all_candidates:
        print(f"\n=== {candidate.name} — {candidate.description} ===")
        t0 = time.time()
        verdicts = run_judge(candidate, calib)
        elapsed = time.time() - t0
        metrics = compute_metrics(calib, verdicts)
        per_stratum = compute_per_stratum(calib, verdicts)

        if not args.skip_test_retest:
            tr = test_retest(candidate, calib, n_subset=TEST_RETEST_N, n_runs=TEST_RETEST_RUNS)
        else:
            tr = {"n_subset": 0, "n_runs": 0, "n_disagree": 0, "disagreement_rate": 0.0, "skipped": True}

        if candidate.name == "baseline_gpt4omini":
            baseline_fnr = metrics["fnr"]
        gates = check_gates(metrics, tr, baseline_fnr if baseline_fnr is not None else 1.0)

        # Holdout — only for winning candidates (run later)
        result = {
            "name": candidate.name,
            "description": candidate.description,
            "elapsed_s": round(elapsed, 1),
            "estimated_cost_usd": round(candidate.cost_per_call_usd * len(calib), 3),
            "metrics": metrics,
            "per_stratum": per_stratum,
            "test_retest": tr,
            "gates": gates,
            "verdicts": verdicts,
        }
        results.append(result)

        print(f"  metrics: κ={metrics['kappa']:.3f}  FPR={metrics['fpr']:.3f}  FNR={metrics['fnr']:.3f}  "
              f"F1={metrics['f1']:.3f}  FPR_adv={metrics['fpr_adversarial']:.3f} ({metrics['n_adversarial_passed']}/{metrics['n_adversarial']})")
        print(f"  test-retest: disagreement={tr['disagreement_rate']:.3f}")
        print(f"  gates: {gates}")

    # Run winner on holdout
    winner = None
    for r in results:
        if r["gates"]["all_pass"]:
            if winner is None or r["metrics"]["kappa"] > winner["metrics"]["kappa"]:
                winner = r

    if winner:
        winner_name = winner["name"]
        winner_candidate = next(c for c in all_candidates if c.name == winner_name)
        print(f"\n=== Winner candidate (clears all gates): {winner_name} ===")
        print(f"Running on holdout ({len(holdout)} records)...")
        h_verdicts = run_judge(winner_candidate, holdout)
        h_metrics = compute_metrics(holdout, h_verdicts)
        h_per_stratum = compute_per_stratum(holdout, h_verdicts)
        winner["holdout"] = {
            "metrics": h_metrics,
            "per_stratum": h_per_stratum,
            "verdicts": h_verdicts,
            "kappa_drop_from_calibration": round(winner["metrics"]["kappa"] - h_metrics["kappa"], 4),
            "ships": abs(winner["metrics"]["kappa"] - h_metrics["kappa"]) <= 0.05,
        }
        print(f"  holdout κ={h_metrics['kappa']:.3f}  (calib κ={winner['metrics']['kappa']:.3f}, drop={winner['holdout']['kappa_drop_from_calibration']:+.3f})")
        print(f"  holdout ships: {winner['holdout']['ships']}")
    else:
        print("\n=== No candidate cleared all gates. No holdout run. ===")

    out = {
        "manifest": {
            "calibration_file": str(args.calib),
            "holdout_file": str(args.holdout),
            "n_calibration": len(calib),
            "n_holdout": len(holdout),
            "n_candidates": len(all_candidates),
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "wall_time_s": round(time.time() - t_start, 1),
            "shipping_gates": {
                "kappa_ge_0.75": 0.75,
                "fpr_adversarial_le_0.05": 0.05,
                "fnr_lt_baseline": "baseline-dependent",
                "test_retest_lt_0.05": 0.05,
            },
            "baseline_fnr_for_gating": baseline_fnr,
        },
        "results": results,
        "winner": winner["name"] if winner else None,
    }
    with open(args.output, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {args.output}  (wall={time.time() - t_start:.0f}s)")

    # Summary table
    print(f"\n{'='*100}")
    print(f"{'Candidate':<28s} {'κ':>6s} {'FPR':>6s} {'FNR':>6s} {'F1':>6s} {'FPR_adv':>8s} {'TestRetest':>11s} {'Gates':>6s}")
    print(f"{'-'*100}")
    for r in results:
        m = r["metrics"]
        t = r["test_retest"]
        gates_str = "PASS" if r["gates"]["all_pass"] else "fail"
        print(f"{r['name']:<28s} {m['kappa']:>6.3f} {m['fpr']:>6.3f} {m['fnr']:>6.3f} "
              f"{m['f1']:>6.3f} {m['fpr_adversarial']:>8.3f} {t['disagreement_rate']:>11.3f} {gates_str:>6s}")


if __name__ == "__main__":
    sys.exit(main())
