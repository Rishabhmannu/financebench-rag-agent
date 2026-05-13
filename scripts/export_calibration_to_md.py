"""Export the judge calibration set to a markdown file for offline labeling.

Generates one markdown file per JSONL input (calibration + holdout). The file
opens cleanly in Word / Google Docs / any markdown reader. Each record has a
`MY VERDICT:` placeholder you replace with PASS / FAIL / SKIP.

After labeling, run `scripts/parse_calibration_md.py` to feed verdicts back
into the JSONL.
"""

import argparse
import json
from pathlib import Path

DEFAULT_INPUTS = [
    (Path("tests/evaluation/judge_calibration_v1.jsonl"),
     Path("tests/evaluation/judge_calibration_v1.md"),
     "Calibration set (89 questions)"),
    (Path("tests/evaluation/judge_calibration_v1_holdout.jsonl"),
     Path("tests/evaluation/judge_calibration_v1_holdout.md"),
     "Holdout set (15 questions)"),
]


INSTRUCTIONS = """# Sprint 7.14 — Judge Calibration Labeling

## What this is

You're hand-labeling 89 calibration questions (+ 15 holdout in the second file) so we can measure how well different judge candidates (gpt-4o-mini, Sonnet 4.6, Opus 4.7, multi-judge consensus) agree with **your** ground truth verdicts. The shipping gates are Cohen's κ ≥ 0.75 + FPR ≤ 5% on adversarial cases.

## What you do for each record

Read:
1. **Question** — what was asked
2. **Gold answer** — the FinanceBench reference answer
3. **System answer** — what our RAG system produced (full text shown)
4. **Original judge verdict + reason** — what gpt-4o-mini said
5. **Draft label + reason** — my proposed label based on the audit

Decide:
- **PASS** if the system answer conveys the same factual content as gold. Allow:
  - Rounding (5.43% vs 5.4%; 1.3315 vs 1.33)
  - Different units that mean the same (-1.53% vs -0.02 decimal; $1,577M vs 1,577 million USD)
  - Extra context, computation steps, or caveats — judge based on the **final asserted answer**
  - Different phrasing (Las Vegas Strip Resorts ≈ Las Vegas resorts)
- **FAIL** if the system answer:
  - Asserts a different numeric value (wrong number)
  - Says opposite yes/no or trend direction (wrong direction)
  - Refuses to answer when gold gives a definite answer
  - Misses substantively (partial answer with critical drivers missing)

**Adversarial cases (marked CAREFUL REVIEW + ADVERSARIAL)**: I manually corrupted the system answer to be clearly wrong. **All of these should be FAIL.** If you think any are NOT clearly wrong, mark SKIP — that means the corruption wasn't strong enough.

**Dataset_suspect cases** (1 in calibration): the system answer is correct against the actual document, but the gold label is wrong. The judge's job is to match the gold label as written — so the correct label here is **FAIL** (system disagrees with gold). Dataset errors are a separate cleanup.

## How to label

For each record, find the line:

> **MY VERDICT:** `__REPLACE__`

Replace `__REPLACE__` with one of: `PASS`, `FAIL`, or `SKIP` (uncertain — review later).

Optionally fill the `**MY NOTE:**` line with brief reasoning if needed.

When done, save the file (`.md`) and ping me — I'll parse it back into JSONL.

---

"""


def _format_record(idx, total, rec):
    careful_flag = "  **[CAREFUL REVIEW]**" if rec.get("requires_careful_review") else ""
    adv_flag = "  **[ADVERSARIAL — should FAIL]**" if rec.get("is_adversarial") else ""
    body = []
    body.append(f"## Record {idx + 1} of {total}  —  `{rec['id']}`{careful_flag}{adv_flag}")
    body.append("")
    body.append(f"**Stratum:** `{rec['stratum']}`")
    body.append(f"**fb_id:** `{rec['fb_id']}`")
    body.append("")
    body.append(f"### Question")
    body.append("")
    body.append(rec["question"])
    body.append("")
    body.append(f"### Gold answer")
    body.append("")
    body.append(f"> {rec['gold']}")
    body.append("")
    body.append(f"### System answer (full)")
    body.append("")
    body.append("```")
    body.append(rec["system_answer"])
    body.append("```")
    body.append("")
    body.append(f"### Original gpt-4o-mini judge verdict")
    body.append("")
    body.append(f"- **Verdict:** {'PASS' if rec['original_judge_pass'] else 'FAIL'}")
    body.append(f"- **Reason:** {rec['original_judge_reason']}")
    body.append("")
    body.append(f"### My draft label")
    body.append("")
    body.append(f"- **Draft:** `{rec['draft_label']}`")
    body.append(f"- **Source:** `{rec['draft_source']}`")
    body.append(f"- **Reason:** {rec['draft_reason']}")
    if rec.get("corruption_type"):
        body.append(f"- **Corruption type:** `{rec['corruption_type']}` (this is an adversarial case)")
    body.append("")
    body.append(f"### ➤ Your call")
    body.append("")
    body.append(f"**MY VERDICT:** `__REPLACE__`")
    body.append("")
    body.append(f"**MY NOTE:** ")
    body.append("")
    body.append("---")
    body.append("")
    return "\n".join(body)


def export(in_path, out_path, title):
    with open(in_path) as f:
        records = [json.loads(line) for line in f if line.strip()]
    print(f"loaded {len(records)} records from {in_path}")

    out = [INSTRUCTIONS]
    out.append(f"# {title}\n")
    out.append(f"_Total: {len(records)} records. Estimated time: ~{len(records) * 30 // 60} min._\n")
    out.append("\n---\n")
    for i, rec in enumerate(records):
        out.append(_format_record(i, len(records), rec))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out))
    print(f"wrote {out_path}  ({out_path.stat().st_size // 1024} KB)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=None,
                        help="Single JSONL input. If omitted, exports both calibration + holdout.")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output .md path (only used with --input)")
    args = parser.parse_args()

    if args.input:
        out = args.output or args.input.with_suffix(".md")
        export(args.input, out, args.input.stem)
        return

    for in_path, out_path, title in DEFAULT_INPUTS:
        if not in_path.exists():
            print(f"  skip: {in_path} not found")
            continue
        export(in_path, out_path, title)


if __name__ == "__main__":
    main()
