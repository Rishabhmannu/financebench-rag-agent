"""Interactive labeling helper for the Sprint 7.14 judge calibration set.

Walks through each record in `judge_calibration_v1.jsonl` (or `_holdout.jsonl`),
shows the question + gold + system answer + the draft label + original judge
reason, and accepts your verdict. Saves after each entry so you can quit and
resume.

Controls:
  A  accept the draft label
  P  override to PASS
  F  override to FAIL
  S  skip (mark uncertain — review later)
  B  back to previous record
  Q  quit and save
  N  add a note (then re-prompt)
  V  view the full system answer (default shows tail)
"""

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_FILE = Path("tests/evaluation/judge_calibration_v1.jsonl")
EXCERPT_TAIL = 600


def _load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _save(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _show(record, idx, total):
    _clear()
    careful = "[CAREFUL REVIEW]" if record.get("requires_careful_review") else ""
    adversarial = "[ADVERSARIAL — must FAIL]" if record.get("is_adversarial") else ""
    print(f"=== {idx + 1}/{total}  {record['id']}  stratum={record['stratum']}  {careful} {adversarial}")
    print()
    print(f"fb_id: {record['fb_id']}")
    print(f"Q: {record['question']}")
    print()
    print(f"GOLD: {record['gold']}")
    print()
    answer = record["system_answer"]
    if len(answer) > EXCERPT_TAIL:
        print(f"SYSTEM ANSWER (last {EXCERPT_TAIL} chars; press V to see full):")
        print(f"  ...{answer[-EXCERPT_TAIL:]}")
    else:
        print(f"SYSTEM ANSWER:")
        print(f"  {answer}")
    print()
    print(f"ORIGINAL JUDGE PASS: {record['original_judge_pass']}")
    print(f"ORIGINAL JUDGE REASON: {record['original_judge_reason'][:300]}")
    print()
    print(f"DRAFT LABEL: {record['draft_label']}  (source: {record['draft_source']})")
    print(f"DRAFT REASON: {record['draft_reason']}")
    if record.get("corruption_type"):
        print(f"CORRUPTION TYPE: {record['corruption_type']}")
    print()
    if record.get("human_label"):
        print(f"PRIOR HUMAN LABEL: {record['human_label']}  ({record.get('human_reason') or 'no note'})")
        print()


def _show_full(record):
    print()
    print("=" * 78)
    print(f"FULL SYSTEM ANSWER for {record['id']} ({record['fb_id']}):")
    print("=" * 78)
    print(record["system_answer"])
    print("=" * 78)
    input("\nPress Enter to return...")


def _prompt_choice(record):
    while True:
        raw = input(
            "[A]ccept draft  [P]ass  [F]ail  [S]kip  [B]ack  [V]iew full  [N]ote  [Q]uit&save: "
        ).strip().lower()
        if raw in ("a", "p", "f", "s", "b", "v", "n", "q"):
            return raw
        print("  ? unrecognized; pick one of A/P/F/S/B/V/N/Q")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--from-record", type=str, default=None,
                        help="Jump to this record id and start labeling from there.")
    parser.add_argument("--include-labeled", action="store_true",
                        help="Show records that already have a human_label (default: skip).")
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"FAIL: {args.file} not found. Run scripts/build_judge_calibration.py first.")

    records = _load(args.file)
    print(f"loaded {len(records)} records from {args.file}")

    # Determine where to start
    start_idx = 0
    if args.from_record:
        for i, r in enumerate(records):
            if r["id"] == args.from_record:
                start_idx = i
                break
    elif not args.include_labeled:
        # Resume after last labeled
        for i, r in enumerate(records):
            if r.get("human_label") is None:
                start_idx = i
                break
        else:
            print(f"All {len(records)} records already labeled. Use --include-labeled to revisit.")
            return

    i = start_idx
    pending_note = None
    while 0 <= i < len(records):
        rec = records[i]
        if not args.include_labeled and rec.get("human_label") is not None:
            i += 1
            continue

        _show(rec, i, len(records))
        if pending_note:
            print(f"NOTE STAGED: {pending_note}")
            print()
        choice = _prompt_choice(rec)

        if choice == "v":
            _show_full(rec)
            continue
        if choice == "n":
            pending_note = input("Note: ").strip() or None
            continue
        if choice == "q":
            _save(args.file, records)
            print(f"saved progress to {args.file}; labeled so far: "
                  f"{sum(1 for r in records if r.get('human_label'))}/{len(records)}")
            return
        if choice == "b":
            i = max(0, i - 1)
            # Find the previous record that needs labeling (or that was labeled)
            while i > 0 and (not args.include_labeled) and records[i].get("human_label") is not None:
                i -= 1
            continue
        if choice == "s":
            rec["human_label"] = "SKIP"
            rec["human_reason"] = pending_note or "uncertain"
            pending_note = None
            _save(args.file, records)
            i += 1
            continue

        if choice == "a":
            label = rec["draft_label"]
            reason = pending_note or "accepted draft"
        elif choice == "p":
            label = "PASS"
            reason = pending_note or "override to PASS"
        else:  # f
            label = "FAIL"
            reason = pending_note or "override to FAIL"

        rec["human_label"] = label
        rec["human_reason"] = reason
        pending_note = None
        _save(args.file, records)
        i += 1

    _save(args.file, records)
    labeled = sum(1 for r in records if r.get("human_label") and r["human_label"] != "SKIP")
    skipped = sum(1 for r in records if r.get("human_label") == "SKIP")
    print(f"\nDone. Labeled: {labeled}  Skipped: {skipped}  Of total: {len(records)}")
    print(f"Output: {args.file}")


if __name__ == "__main__":
    sys.exit(main())
