"""Build the Sprint 7.15 Phase 0 bundled pipeline diagnostic candidate set.

Stratified 75 Qs (default) covering all 48 still-failing cases plus 27
known-passing. For each Q, auto-prefills target_company + target_year from
FB metadata and pulls V1 system answer + retrieved sources (needed for the
hallu_grounded labeling pass).

Output: tests/evaluation/pipeline_diagnostic_v1.jsonl
"""

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.company_registry import canonical_company_slug

V1_CORRECTNESS = Path("tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_v1_grader.correctness.json")
PIPELINE = Path("tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_v1_grader.pipeline.json")
AUDIT = Path("tests/evaluation/eval_results/audit_failed_qs_v1_grader.json")
DIFF = Path("tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_v1_grader.rejudged_sonnet_v2.diff.json")
FB_JSONL = Path("data/raw/financebench/financebench_open_source.jsonl")
OUT = Path("tests/evaluation/pipeline_diagnostic_v1.jsonl")


def extract_fiscal_year(doc_name: str) -> int | None:
    m = re.search(r"_(\d{4})(?:Q\d+)?_", doc_name)
    return int(m.group(1)) if m else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-total", type=int, default=75,
                        help="Total number of records to produce")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-chunks-per-q", type=int, default=5,
                        help="How many top retrieved chunks to embed per record for hallu eval")
    args = parser.parse_args()

    rng = random.Random(args.seed)

    print("loading inputs...")
    v1 = json.load(open(V1_CORRECTNESS))
    pipeline = json.load(open(PIPELINE))
    audit = json.load(open(AUDIT))["audits"]
    diff = json.load(open(DIFF))["per_sample"]
    fb = {json.loads(line)["financebench_id"]: json.loads(line) for line in open(FB_JSONL)}

    v1_lookup = {r["fb_id"]: (i, r) for i, r in enumerate(v1["per_sample"])}
    contexts = pipeline["contexts"]
    audit_by_id = {a["fb_id"]: a for a in audit}

    still_failing = [d for d in diff if not d["new_pass"]]
    passing = [d for d in diff if d["new_pass"]]
    print(f"  still failing: {len(still_failing)}  passing: {len(passing)}")

    # All 48 failing + (target_total - 48) stratified passing
    n_passing_target = max(0, args.target_total - len(still_failing))
    print(f"  target: all {len(still_failing)} failing + {n_passing_target} passing = {len(still_failing) + n_passing_target}")

    # Stratify passing by question_type
    by_type: dict[str, list] = defaultdict(list)
    for p in passing:
        qt = fb[p["fb_id"]].get("question_type", "?")
        by_type[qt].append(p)
    for items in by_type.values():
        rng.shuffle(items)

    # Distribute n_passing_target across types proportionally
    type_counts = {qt: len(items) for qt, items in by_type.items()}
    total = sum(type_counts.values())
    per_type_target = {
        qt: max(1, round(n_passing_target * type_counts[qt] / total))
        for qt in by_type
    }
    # Adjust if rounding overshoots/undershoots
    while sum(per_type_target.values()) > n_passing_target:
        biggest = max(per_type_target, key=per_type_target.get)
        per_type_target[biggest] -= 1
    while sum(per_type_target.values()) < n_passing_target:
        smallest = min(per_type_target, key=per_type_target.get)
        per_type_target[smallest] += 1

    sampled_passing = []
    for qt, n in per_type_target.items():
        sampled_passing.extend(by_type[qt][:n])
    print(f"  passing breakdown: {dict(Counter(fb[p['fb_id']].get('question_type','?') for p in sampled_passing))}")

    selected = list(still_failing) + sampled_passing
    print(f"  total selected: {len(selected)}")

    # Build records
    records = []
    for d in selected:
        fb_id = d["fb_id"]
        idx, v1_rec = v1_lookup[fb_id]
        q = fb[fb_id]
        audit_cat = audit_by_id.get(fb_id, {}).get("category") if not d["new_pass"] else None

        ctx = contexts[idx][: args.max_chunks_per_q]

        records.append({
            "id": "TBD",
            "fb_id": fb_id,
            "company": q["company"],
            "doc_name": q["doc_name"],
            "question_type": q.get("question_type", "?"),
            "question": q["question"],
            "gold": q["answer"],
            # Current-state status under the new judge
            "v1_pass_status": "PASS" if d["new_pass"] else "FAIL",
            "v1_audit_category": audit_cat,
            # Auto-prefilled FB metadata (entity ground truth)
            "auto_target_company_slug": canonical_company_slug(q["company"]),
            "auto_target_company_display": q["company"],
            "auto_target_fiscal_year": extract_fiscal_year(q["doc_name"]),
            # System artifacts (for hallu_grounded labeling)
            "v1_system_answer": v1_rec["answer"],
            "v1_retrieved_chunks_top": [c[:1500] for c in ctx],
            "v1_n_chunks_retrieved": len(contexts[idx]),
            "v1_judge_reason": v1_rec.get("reason", ""),
            # Labels to fill — all None initially
            "human_intent": None,
            "human_complexity": None,
            "human_target_company_correct": None,
            "human_target_year_correct": None,
            "human_expected_sub_queries": None,
            "human_hallu_grounded": None,
            "human_notes": None,
        })

    # Shuffle so failing/passing aren't grouped
    rng.shuffle(records)
    for i, r in enumerate(records, 1):
        r["id"] = f"diag_{i:03d}"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"\nwrote {OUT}  ({len(records)} records)")

    # Quick summary
    print(f"\n=== Summary ===")
    print(f"  Total: {len(records)}")
    print(f"  By status: {dict(Counter(r['v1_pass_status'] for r in records))}")
    print(f"  By question_type: {dict(Counter(r['question_type'] for r in records))}")
    print(f"  By audit_category (failing only): "
          f"{dict(Counter(r['v1_audit_category'] for r in records if r['v1_audit_category']))}")


if __name__ == "__main__":
    sys.exit(main())
