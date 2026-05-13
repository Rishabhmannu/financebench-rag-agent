"""Re-judge an existing FinanceBench correctness.json with the Sprint 7.14 judge.

Drop-in alternative to running the full pipeline again — reads any existing
correctness.json, applies the Sonnet 4.6 + v2 improved prompt judge to each
per-sample record, and writes a new correctness.json + per-Q diff.

Cost: ~$0.40 per 150-Q run. Wall time: ~3 min at 8-way parallel.

Usage:
  python tests/evaluation/rejudge.py --input <existing-correctness.json>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.evaluation.judge_eval import IMPROVED_PROMPT, _make_anthropic_judge

JUDGE_MODEL = "claude-sonnet-4-6"
JUDGE_PROMPT_VERSION = "improved_v2"
DEFAULT_PARALLELISM = 8


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True,
                        help="Path to existing correctness.json with per_sample records")
    parser.add_argument("--model", default=JUDGE_MODEL)
    parser.add_argument("--parallelism", type=int, default=DEFAULT_PARALLELISM)
    parser.add_argument("--limit", type=int, default=None,
                        help="Re-judge only first N records (smoke mode)")
    args = parser.parse_args()

    data = json.load(open(args.input))
    records = data["per_sample"]
    old_agg = data.get("aggregate", {})
    if args.limit:
        records = records[: args.limit]
    print(f"loaded {len(records)} records from {args.input}")
    print(f"  old judge: {old_agg.get('judge_model', '?')}  old pass rate: {old_agg.get('pass_rate', '?')}")

    judge = _make_anthropic_judge(args.model, IMPROVED_PROMPT)

    def grade(i: int) -> tuple[int, dict]:
        r = records[i]
        v = judge({
            "question": r["question"],
            "gold": r["gold"],
            "system_answer": r["answer"],
        })
        return i, v

    t0 = time.time()
    new_verdicts: list[dict | None] = [None] * len(records)
    with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
        futures = [pool.submit(grade, i) for i in range(len(records))]
        done = 0
        for fut in as_completed(futures):
            i, v = fut.result()
            new_verdicts[i] = v
            done += 1
            if done % 25 == 0:
                print(f"  [{done}/{len(records)}] done ({time.time() - t0:.0f}s)")

    # Build new correctness records + diff
    new_records: list[dict] = []
    rescues = 0
    regressions = 0
    unchanged_pass = 0
    unchanged_fail = 0
    judge_errors = 0
    for r, v in zip(records, new_verdicts):
        old_pass = bool(r["pass"])
        new_pass = bool(v["passed"])
        if v["reason"].startswith("judge_error"):
            judge_errors += 1
        if old_pass and new_pass:
            unchanged_pass += 1
        elif not old_pass and not new_pass:
            unchanged_fail += 1
        elif not old_pass and new_pass:
            rescues += 1
        else:
            regressions += 1
        new_records.append({
            "fb_id": r["fb_id"],
            "company": r.get("company"),
            "question": r["question"],
            "answer": r["answer"],
            "gold": r["gold"],
            "pass": new_pass,
            "reason": v["reason"],
        })

    n_pass_new = sum(1 for r in new_records if r["pass"])
    n = len(new_records)
    new_agg = {
        "judge_model": args.model,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "n_samples": n,
        "n_pass": n_pass_new,
        "n_errors": judge_errors,
        "pass_rate": round(n_pass_new / n, 4) if n else 0.0,
        "old_judge": old_agg.get("judge_model"),
        "old_pass_rate": old_agg.get("pass_rate"),
        "delta_pass_rate": round(
            (n_pass_new / n) - old_agg.get("pass_rate", 0), 4
        ) if n else 0.0,
        "rescues": rescues,
        "regressions": regressions,
        "unchanged_pass": unchanged_pass,
        "unchanged_fail": unchanged_fail,
        "wall_time_s": round(time.time() - t0, 1),
    }

    base = args.input.stem
    if base.endswith(".correctness"):
        base = base[: -len(".correctness")]
    out_path = args.input.parent / f"{base}.rejudged_sonnet_v2.correctness.json"
    diff_path = args.input.parent / f"{base}.rejudged_sonnet_v2.diff.json"

    with open(out_path, "w") as f:
        json.dump({"aggregate": new_agg, "per_sample": new_records}, f, indent=2)

    diff_records = [
        {
            "fb_id": r["fb_id"],
            "company": r.get("company"),
            "question": (r["question"] or "")[:150],
            "gold": (r["gold"] or "")[:120],
            "old_pass": bool(orig["pass"]),
            "new_pass": bool(r["pass"]),
            "delta": (
                "rescue" if (r["pass"] and not orig["pass"])
                else "regression" if (orig["pass"] and not r["pass"])
                else "unchanged_pass" if r["pass"]
                else "unchanged_fail"
            ),
            "old_reason": (orig["reason"] or "")[:250],
            "new_reason": (r["reason"] or "")[:250],
        }
        for r, orig in zip(new_records, records)
    ]
    with open(diff_path, "w") as f:
        json.dump({"aggregate": new_agg, "per_sample": diff_records}, f, indent=2)

    print(f"\nwrote {out_path}")
    print(f"wrote {diff_path}")
    print(f"\n=== Summary ===")
    print(f"  old judge: {old_agg.get('judge_model')}")
    print(f"  old: {old_agg.get('n_pass')}/{old_agg.get('n_samples')} = {old_agg.get('pass_rate', 0):.4f}")
    print(f"  new judge: {args.model} + {JUDGE_PROMPT_VERSION}")
    print(f"  new: {n_pass_new}/{n} = {new_agg['pass_rate']:.4f}  (Δ {new_agg['delta_pass_rate']:+.4f})")
    print(f"  rescues:        {rescues}  (old=FAIL → new=PASS)")
    print(f"  regressions:    {regressions}  (old=PASS → new=FAIL)")
    print(f"  unchanged pass: {unchanged_pass}")
    print(f"  unchanged fail: {unchanged_fail}")
    print(f"  judge_errors:   {judge_errors}")
    print(f"  wall time:      {new_agg['wall_time_s']}s")


if __name__ == "__main__":
    sys.exit(main())
