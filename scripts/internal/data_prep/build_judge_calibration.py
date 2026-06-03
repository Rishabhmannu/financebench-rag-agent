"""Build the Sprint 7.14 judge calibration set + holdout.

Stratified construction from V1 grader correctness + Sprint 7.13 audit:
  - 25 clear-pass (V1 grader passes, draft = PASS)
  - 11 wrong-number / wrong-direction (audit, draft = FAIL)
  - 12 numeric-rounding (audit PASS_NUMERIC_ROUNDING, draft = PASS)
  - 15 judge-bug recoveries (audit PASS_JUDGE_BUG, draft = PASS)
  - 10 refusals (audit REFUSAL, draft = FAIL)
  - 5 partial answers (audit PARTIAL_ANSWER, draft = FAIL)
  - 1 dataset-suspect (audit DATASET_SUSPECT, draft = FAIL — judge should match gold)
  - 10 adversarial (V1 passes with system answer corrupted by Sonnet, draft = FAIL)

Plus a 15-Q holdout from different Qs (over-fit prevention).

Drafts are transcribed from the audit where possible (no new LLM calls). Only
the 10 calibration + 5 holdout adversarial cases require Sonnet calls.

Output:
  tests/evaluation/judge_calibration_v1.jsonl
  tests/evaluation/judge_calibration_v1_holdout.jsonl
"""

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

V1_CORRECTNESS = Path("tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_v1_grader.correctness.json")
AUDIT = Path("tests/evaluation/eval_results/audit_failed_qs_v1_grader.json")
FB_JSONL = Path("data/raw/financebench/financebench_open_source.jsonl")
OUT_CALIB = Path("tests/evaluation/judge_calibration_v1.jsonl")
OUT_HOLDOUT = Path("tests/evaluation/judge_calibration_v1_holdout.jsonl")

# Stratum targets — must sum to 89 calibration + 15 holdout
CALIB_TARGETS = {
    "clear_pass": 25,
    "wrong_number_or_direction": 11,
    "numeric_rounding": 12,
    "judge_bug_recovery": 15,
    "refusal": 10,
    "partial_answer": 5,
    "dataset_suspect": 1,
    "adversarial": 10,
}
HOLDOUT_TARGETS = {
    "clear_pass": 5,
    "judge_bug_recovery": 5,
    "refusal": 3,
    "adversarial": 2,
}
SEED = 42

AUDIT_CAT_TO_DRAFT = {
    "PASS_JUDGE_BUG": "PASS",
    "PASS_NUMERIC_ROUNDING": "PASS",
    "PASS_OTHER": "PASS",
    "REFUSAL": "FAIL",
    "WRONG_NUMBER": "FAIL",
    "WRONG_DIRECTION": "FAIL",
    "PARTIAL_ANSWER": "FAIL",
    "DATASET_SUSPECT": "FAIL",
    "OTHER_FAIL": "FAIL",
}


class Corruption(BaseModel):
    corrupted_answer: str = Field(description="The system answer rewritten with the final asserted value clearly wrong.")
    corruption_type: str = Field(description="One of: numeric / sign_flip / direction_flip")
    what_changed: str = Field(description="One sentence describing what was changed.")


CORRUPTION_PROMPT = """You are constructing an adversarial test case for a financial-QA judge calibration set.

Given a financial question and a generated system answer (which is correct), produce a CORRUPTED version where the final asserted answer is CLEARLY WRONG. The corruption should be obvious enough that any competent judge should fail it.

Pick exactly ONE corruption type:
- "numeric": change the primary numeric answer to a clearly different value (e.g., 1.33 → 7.41, $1,577M → $9,200M)
- "sign_flip": flip the sign of the answer (negative → positive or vice versa)
- "direction_flip": flip the direction conclusion (Yes → No, increased → decreased)

Keep the structure / formatting / explanations otherwise; just change the FINAL ASSERTED VALUE so the answer is clearly wrong relative to the gold.

Question: {question}
Gold answer: {gold}

Original (correct) system answer:
{answer}

Construct the corrupted answer.
"""


def _load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _stratify_audit(audit_records):
    """Group audit records by category."""
    by_cat = {}
    for a in audit_records:
        by_cat.setdefault(a["category"], []).append(a)
    return by_cat


def _build_record(stratum, record_id, fb_id, question, gold, system_answer, original_pass,
                  original_judge_reason, audit_category, draft_label, draft_reason,
                  draft_source, is_adversarial=False, corruption_type=None,
                  original_system_answer=None):
    return {
        "id": record_id,
        "stratum": stratum,
        "fb_id": fb_id,
        "question": question,
        "gold": gold,
        "system_answer": system_answer,
        "original_judge_pass": original_pass,
        "original_judge_reason": original_judge_reason,
        "audit_category": audit_category,
        "draft_label": draft_label,
        "draft_reason": draft_reason,
        "draft_source": draft_source,
        "is_adversarial": is_adversarial,
        "corruption_type": corruption_type,
        "original_system_answer_if_adversarial": original_system_answer,
        "requires_careful_review": stratum in (
            "numeric_rounding", "judge_bug_recovery", "refusal",
            "partial_answer", "dataset_suspect", "adversarial",
        ),
        "human_label": None,
        "human_reason": None,
    }


def _build_from_audit(stratum, audit_pool, fb_lookup, used_ids, target_count, id_prefix):
    """Pull `target_count` records from audit_pool (excluding `used_ids`)."""
    available = [a for a in audit_pool if a["fb_id"] not in used_ids]
    random.shuffle(available)
    picked = available[:target_count]
    records = []
    for i, a in enumerate(picked, 1):
        fb_id = a["fb_id"]
        used_ids.add(fb_id)
        q = fb_lookup[fb_id]
        # Find the V1 correctness record for system_answer + judge reason
        v1_rec = v1_lookup[fb_id]
        records.append(_build_record(
            stratum=stratum,
            record_id=f"{id_prefix}_{stratum}_{i:03d}",
            fb_id=fb_id,
            question=q["question"],
            gold=q.get("answer", v1_rec["gold"]),
            system_answer=v1_rec["answer"],
            original_pass=v1_rec["pass"],
            original_judge_reason=v1_rec["reason"],
            audit_category=a["category"],
            draft_label=AUDIT_CAT_TO_DRAFT[a["category"]],
            draft_reason=a["reason"],
            draft_source="audit_transcription",
        ))
    return records


def _build_clear_pass(target_count, used_ids, id_prefix):
    """Sample V1 grader passes — these inherit draft=PASS from gpt-4o-mini judge (weaker draft)."""
    passes = [r for r in v1_per_sample if r["pass"] and r["fb_id"] not in used_ids]
    random.shuffle(passes)
    picked = passes[:target_count]
    records = []
    for i, r in enumerate(picked, 1):
        fb_id = r["fb_id"]
        used_ids.add(fb_id)
        q = fb_lookup[fb_id]
        records.append(_build_record(
            stratum="clear_pass",
            record_id=f"{id_prefix}_clear_pass_{i:03d}",
            fb_id=fb_id,
            question=q["question"],
            gold=r["gold"],
            system_answer=r["answer"],
            original_pass=True,
            original_judge_reason=r["reason"],
            audit_category=None,
            draft_label="PASS",
            draft_reason="V1 grader marked PASS (gpt-4o-mini judge — not independently re-verified)",
            draft_source="v1_grader_pass",
        ))
    return records


def _generate_adversarial(llm, base_record, record_id, id_prefix, stratum_label):
    """Use Sonnet to corrupt a passing system answer into a clearly wrong one."""
    structured = llm.with_structured_output(Corruption)
    prompt = CORRUPTION_PROMPT.format(
        question=base_record["question"],
        gold=base_record["gold"],
        answer=base_record["system_answer"],
    )
    try:
        result: Corruption = structured.invoke([HumanMessage(content=prompt)])
    except Exception as e:
        print(f"  ! corruption failed for {base_record['fb_id']}: {e}")
        return None
    return _build_record(
        stratum="adversarial",
        record_id=f"{id_prefix}_adversarial_{record_id:03d}",
        fb_id=base_record["fb_id"],
        question=base_record["question"],
        gold=base_record["gold"],
        system_answer=result.corrupted_answer,
        original_pass=True,
        original_judge_reason=base_record["original_judge_reason"],
        audit_category=None,
        draft_label="FAIL",
        draft_reason=f"Adversarial corruption ({result.corruption_type}): {result.what_changed}",
        draft_source="sonnet_corruption",
        is_adversarial=True,
        corruption_type=result.corruption_type,
        original_system_answer=base_record["system_answer"],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    random.seed(args.seed)

    print("loading inputs...")
    global v1_per_sample, v1_lookup, fb_lookup, audit_records
    v1 = json.load(open(V1_CORRECTNESS))
    v1_per_sample = v1["per_sample"]
    v1_lookup = {r["fb_id"]: r for r in v1_per_sample}
    audit_records = json.load(open(AUDIT))["audits"]
    fb_lookup = {json.loads(line)["financebench_id"]: json.loads(line) for line in open(FB_JSONL)}
    print(f"  v1 per_sample={len(v1_per_sample)}  audit={len(audit_records)}  fb={len(fb_lookup)}")

    by_cat = _stratify_audit(audit_records)
    print(f"  audit by category: { {k: len(v) for k, v in by_cat.items()} }")

    # ---- Build CALIBRATION set ----
    used_ids: set = set()
    calib: list = []

    print("\nbuilding calibration set (89 Qs)...")

    # Audit-sourced strata
    audit_strata = [
        ("judge_bug_recovery", by_cat.get("PASS_JUDGE_BUG", []), CALIB_TARGETS["judge_bug_recovery"]),
        ("numeric_rounding", by_cat.get("PASS_NUMERIC_ROUNDING", []), CALIB_TARGETS["numeric_rounding"]),
        ("refusal", by_cat.get("REFUSAL", []), CALIB_TARGETS["refusal"]),
        ("partial_answer", by_cat.get("PARTIAL_ANSWER", []), CALIB_TARGETS["partial_answer"]),
        ("dataset_suspect", by_cat.get("DATASET_SUSPECT", []), CALIB_TARGETS["dataset_suspect"]),
    ]
    for stratum, pool, n in audit_strata:
        recs = _build_from_audit(stratum, pool, fb_lookup, used_ids, n, "calib")
        calib.extend(recs)
        print(f"  {stratum:<25s} {len(recs)}")

    # Wrong-number + wrong-direction combined
    wrong_pool = by_cat.get("WRONG_NUMBER", []) + by_cat.get("WRONG_DIRECTION", [])
    recs = _build_from_audit("wrong_number_or_direction", wrong_pool, fb_lookup, used_ids,
                             CALIB_TARGETS["wrong_number_or_direction"], "calib")
    calib.extend(recs)
    print(f"  wrong_number_or_direction {len(recs)}")

    # V1 passes
    recs = _build_clear_pass(CALIB_TARGETS["clear_pass"], used_ids, "calib")
    calib.extend(recs)
    print(f"  clear_pass                {len(recs)}")

    # Adversarial — pick base passes, corrupt with Sonnet
    print(f"\ngenerating {CALIB_TARGETS['adversarial']} adversarial corruptions (Sonnet)...")
    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        temperature=0.2,  # slight non-zero so corruption variety is OK
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        max_tokens=2048,
    )
    base_passes_for_adv = [r for r in v1_per_sample if r["pass"] and r["fb_id"] not in used_ids]
    random.shuffle(base_passes_for_adv)
    adv_calib = []
    for i, r in enumerate(base_passes_for_adv[: CALIB_TARGETS["adversarial"] + 4], 1):  # +4 buffer
        if len(adv_calib) >= CALIB_TARGETS["adversarial"]:
            break
        fb_id = r["fb_id"]
        q = fb_lookup[fb_id]
        base = _build_record(
            stratum="_temp", record_id="_temp", fb_id=fb_id,
            question=q["question"], gold=r["gold"], system_answer=r["answer"],
            original_pass=True, original_judge_reason=r["reason"],
            audit_category=None, draft_label="PASS",
            draft_reason="(base for adversarial corruption)",
            draft_source="v1_grader_pass",
        )
        rec = _generate_adversarial(llm, base, len(adv_calib) + 1, "calib", "adversarial")
        if rec is not None:
            adv_calib.append(rec)
            used_ids.add(fb_id)
            print(f"  [{len(adv_calib)}/{CALIB_TARGETS['adversarial']}] {fb_id} corruption={rec['corruption_type']}")
    calib.extend(adv_calib)

    # Shuffle so strata aren't grouped during labeling (avoids monotony bias)
    random.shuffle(calib)
    # Renumber IDs after shuffle
    for i, rec in enumerate(calib, 1):
        rec["id"] = f"calib_{i:03d}"

    OUT_CALIB.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CALIB, "w") as f:
        for rec in calib:
            f.write(json.dumps(rec) + "\n")
    print(f"\nwrote {OUT_CALIB}  ({len(calib)} records)")

    # ---- Build HOLDOUT set ----
    holdout: list = []
    print("\nbuilding holdout set (15 Qs, no overlap with calibration)...")

    holdout_audit_strata = [
        ("judge_bug_recovery", by_cat.get("PASS_JUDGE_BUG", []), HOLDOUT_TARGETS["judge_bug_recovery"]),
        ("refusal", by_cat.get("REFUSAL", []), HOLDOUT_TARGETS["refusal"]),
    ]
    for stratum, pool, n in holdout_audit_strata:
        recs = _build_from_audit(stratum, pool, fb_lookup, used_ids, n, "holdout")
        holdout.extend(recs)
        print(f"  {stratum:<25s} {len(recs)}")

    recs = _build_clear_pass(HOLDOUT_TARGETS["clear_pass"], used_ids, "holdout")
    holdout.extend(recs)
    print(f"  clear_pass                {len(recs)}")

    print(f"\ngenerating {HOLDOUT_TARGETS['adversarial']} adversarial corruptions for holdout...")
    base_passes_h = [r for r in v1_per_sample if r["pass"] and r["fb_id"] not in used_ids]
    random.shuffle(base_passes_h)
    adv_h = []
    for r in base_passes_h[: HOLDOUT_TARGETS["adversarial"] + 2]:
        if len(adv_h) >= HOLDOUT_TARGETS["adversarial"]:
            break
        fb_id = r["fb_id"]
        q = fb_lookup[fb_id]
        base = _build_record(
            stratum="_temp", record_id="_temp", fb_id=fb_id,
            question=q["question"], gold=r["gold"], system_answer=r["answer"],
            original_pass=True, original_judge_reason=r["reason"],
            audit_category=None, draft_label="PASS",
            draft_reason="(base for adversarial corruption)",
            draft_source="v1_grader_pass",
        )
        rec = _generate_adversarial(llm, base, len(adv_h) + 1, "holdout", "adversarial")
        if rec is not None:
            adv_h.append(rec)
            used_ids.add(fb_id)
            print(f"  [{len(adv_h)}/{HOLDOUT_TARGETS['adversarial']}] {fb_id} corruption={rec['corruption_type']}")
    holdout.extend(adv_h)

    random.shuffle(holdout)
    for i, rec in enumerate(holdout, 1):
        rec["id"] = f"holdout_{i:03d}"

    with open(OUT_HOLDOUT, "w") as f:
        for rec in holdout:
            f.write(json.dumps(rec) + "\n")
    print(f"\nwrote {OUT_HOLDOUT}  ({len(holdout)} records)")

    # Summary
    def _summarize(records):
        from collections import Counter
        s = Counter()
        d = Counter()
        for r in records:
            s[r["stratum"]] += 1
            d[r["draft_label"]] += 1
        return dict(s), dict(d)

    cs, cd = _summarize(calib)
    hs, hd = _summarize(holdout)
    print(f"\n=== Calibration ({len(calib)} Qs) ===")
    print(f"  by stratum: {cs}")
    print(f"  by draft label: {cd}")
    print(f"\n=== Holdout ({len(holdout)} Qs) ===")
    print(f"  by stratum: {hs}")
    print(f"  by draft label: {hd}")
    print(f"\nAll records have human_label=null. Run scripts/label_judge_calibration.py to fill.")


if __name__ == "__main__":
    sys.exit(main())
