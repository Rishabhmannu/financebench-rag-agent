"""Audit failed FinanceBench eval questions to separate real system failures
from eval-framework artifacts (judge bugs, numeric rounding, dataset errors).

Re-judges each failed question with Sonnet 4.6 + a structured-output prompt
that explicitly handles numeric rounding and refusals. Output: per-Q
classification into one of 8 categories.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

CATEGORIES = [
    "PASS_JUDGE_BUG",
    "PASS_NUMERIC_ROUNDING",
    "PASS_OTHER",
    "REFUSAL",
    "WRONG_NUMBER",
    "WRONG_DIRECTION",
    "PARTIAL_ANSWER",
    "DATASET_SUSPECT",
    "OTHER_FAIL",
]

CATEGORY_DESCRIPTIONS = """
1. PASS_JUDGE_BUG — Generated answer clearly states the correct value but the original judge missed it. Example: judge says "no number provided" but the answer contains "X = 1.33".
2. PASS_NUMERIC_ROUNDING — Generated numeric value rounds to gold within standard tolerance. Examples: 5.43% rounds to 5.4%; 1.3315 rounds to 1.33; $1,577.0M matches $1577M.
3. PASS_OTHER — Generated answer conveys the same factual content as gold but the judge missed it for some other reason (wording, units like millions vs thousands when both unambiguous).
4. REFUSAL — Generated answer declined to answer ("I don't have enough information", "Cannot determine"). A real failure mode but a calibration issue, not a knowledge issue.
5. WRONG_NUMBER — Generated answer asserts a clearly different numeric value from gold (not a rounding match).
6. WRONG_DIRECTION — Generated answer says yes/no opposite of gold, or trend direction (increased/decreased) opposite, or implies opposite causal direction.
7. PARTIAL_ANSWER — Generated answer covers part of the gold but misses substantively (e.g., names 2 of 3 items, gives one component but not the requested derived metric).
8. DATASET_SUSPECT — Generated answer is detailed and substantively contradicts gold in a way that suggests gold may be wrong or outdated (e.g., gold says event is current when filing date implies it was historical).
9. OTHER_FAIL — Doesn't fit above categories.
"""


class AuditVerdict(BaseModel):
    category: str = Field(
        description=f"One of: {', '.join(CATEGORIES)}"
    )
    one_sentence_reason: str = Field(description="One sentence explaining the classification.")
    extracted_system_value: str | None = Field(
        default=None,
        description="If the question expects a numeric answer, extract the final numeric value from the generated answer.",
    )
    extracted_gold_value: str | None = Field(
        default=None,
        description="If the question expects a numeric answer, extract the value from the gold.",
    )


AUDIT_PROMPT = f"""You are auditing a financial Q&A system's failed responses to determine if each failure is a REAL system failure or an ARTIFACT of the eval framework (judge bug, numeric rounding intolerance, or wrong gold label).

Categories:
{CATEGORY_DESCRIPTIONS}

Important guidance:
- For numeric questions, EXTRACT the final numeric value from each answer before comparing.
- A rounded match (5.43% vs 5.4%) is PASS_NUMERIC_ROUNDING.
- A refusal ("I don't have enough information") when gold is a definite answer is REFUSAL.
- If the generated answer cites the doc in detail and reaches a different conclusion than gold, AND the citation appears internally consistent, mark DATASET_SUSPECT.
- Allow minor formatting differences (e.g. "$1,577M" vs "1577 million USD").
- The generated answer may include disclaimers — judge based on the FINAL ASSERTED ANSWER.

Question: {{question}}

Gold answer: {{gold}}

Generated answer: {{generated}}

Original judge's failure reason: {{judge_reason}}

Classify and explain.
"""


def audit_one(llm, q, gold, generated, judge_reason):
    structured = llm.with_structured_output(AuditVerdict)
    prompt = AUDIT_PROMPT.format(question=q, gold=gold, generated=generated, judge_reason=judge_reason)
    try:
        v = structured.invoke([HumanMessage(content=prompt)])
        return v
    except Exception as e:
        return AuditVerdict(
            category="OTHER_FAIL",
            one_sentence_reason=f"audit_error: {type(e).__name__}: {str(e)[:200]}",
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_v1_grader.correctness.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/evaluation/eval_results/audit_failed_qs_v1_grader.json"),
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--model", type=str, default="claude-sonnet-4-6")
    args = parser.parse_args()

    data = json.load(open(args.input))
    failed = [r for r in data["per_sample"] if not r["pass"]]
    if args.limit:
        failed = failed[: args.limit]

    fb_lookup = {
        json.loads(l)["financebench_id"]: json.loads(l)
        for l in open("data/raw/financebench/financebench_open_source.jsonl")
    }

    llm = ChatAnthropic(
        model=args.model,
        temperature=0.0,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        max_tokens=1024,
    )

    audits = []
    by_cat = {c: 0 for c in CATEGORIES}
    t_start = time.time()

    for i, r in enumerate(failed, 1):
        fb_id = r["fb_id"]
        q_meta = fb_lookup.get(fb_id, {})
        question = r.get("question") or q_meta.get("question", "")
        v = audit_one(llm, question, r["gold"], r["answer"], r["reason"])

        cat = v.category if v.category in CATEGORIES else "OTHER_FAIL"
        by_cat[cat] += 1

        audit_rec = {
            "fb_id": fb_id,
            "question_type": q_meta.get("question_type", ""),
            "company": r.get("company"),
            "category": cat,
            "reason": v.one_sentence_reason,
            "extracted_system_value": v.extracted_system_value,
            "extracted_gold_value": v.extracted_gold_value,
            "original_judge_reason": r["reason"],
        }
        audits.append(audit_rec)

        elapsed = time.time() - t_start
        rate = i / elapsed if elapsed else 0
        eta = (len(failed) - i) / rate if rate else 0
        print(
            f"  [{i:>3d}/{len(failed)}] {fb_id}  {cat:<23s}  ({elapsed:.0f}s, ~{eta:.0f}s left)\n"
            f"        {v.one_sentence_reason[:140]}"
        )

    by_type: dict[str, dict[str, int]] = {}
    for a in audits:
        qt = a["question_type"] or "?"
        by_type.setdefault(qt, {c: 0 for c in CATEGORIES})
        by_type[qt][a["category"]] = by_type[qt].get(a["category"], 0) + 1

    pass_categories = ("PASS_JUDGE_BUG", "PASS_NUMERIC_ROUNDING", "PASS_OTHER")
    n_revealed_pass = sum(by_cat[c] for c in pass_categories)
    n_dataset_suspect = by_cat["DATASET_SUSPECT"]

    out = {
        "manifest": {
            "input": str(args.input),
            "n_failed_audited": len(audits),
            "audit_model": args.model,
            "wall_time_s": round(time.time() - t_start, 1),
        },
        "summary": {
            "n_total_failed": len(audits),
            "n_pass_revealed (judge_was_wrong)": n_revealed_pass,
            "n_dataset_suspect (gold_may_be_wrong)": n_dataset_suspect,
            "n_real_failure": len(audits) - n_revealed_pass - n_dataset_suspect,
        },
        "by_category": by_cat,
        "by_question_type": by_type,
        "audits": audits,
    }
    args.output.write_text(json.dumps(out, indent=2))

    print(f"\n{'=' * 78}")
    print(f"Wrote {args.output}  (wall={time.time() - t_start:.1f}s)")
    print(f"\nBy category:")
    for c in CATEGORIES:
        n = by_cat[c]
        pct = n / len(audits) * 100 if audits else 0
        bar = "#" * int(pct)
        print(f"  {c:<26s} {n:>3d}  {pct:>5.1f}%  {bar}")
    print(f"\nHeadline:")
    print(f"  Of {len(audits)} 'failed' Qs, {n_revealed_pass} ({n_revealed_pass/len(audits)*100:.1f}%) "
          f"actually appear to PASS — judge missed them.")
    print(f"  {n_dataset_suspect} ({n_dataset_suspect/len(audits)*100:.1f}%) flagged as DATASET_SUSPECT.")
    real_n = len(audits) - n_revealed_pass - n_dataset_suspect
    print(f"  {real_n} ({real_n/len(audits)*100:.1f}%) appear to be real system failures.")


if __name__ == "__main__":
    sys.exit(main())
