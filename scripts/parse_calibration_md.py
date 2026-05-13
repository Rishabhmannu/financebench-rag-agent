"""Parse a labeled judge-calibration markdown file back into the JSONL.

Reads the .md file, extracts MY VERDICT / MY NOTE per record (by `id`), and
updates the corresponding records in the JSONL. Records still containing
`__REPLACE__` are reported as unlabeled.
"""

import argparse
import json
import re
import sys
from pathlib import Path

VERDICT_VALUES = {"PASS", "FAIL", "SKIP"}


def parse_markdown(md_text: str) -> dict[str, dict]:
    """Returns id -> {verdict, note}. Only records with a non-placeholder verdict."""
    out: dict[str, dict] = {}
    # Split by "## Record N of N — `id`"
    record_blocks = re.split(r"^## Record \d+ of \d+\s+—\s+`([^`]+)`", md_text, flags=re.MULTILINE)
    # record_blocks alternates: [preamble, id1, body1, id2, body2, ...]
    for i in range(1, len(record_blocks) - 1, 2):
        rec_id = record_blocks[i].strip()
        body = record_blocks[i + 1]
        verdict_match = re.search(r"\*\*MY VERDICT:\*\*\s*`?(\w+)`?", body)
        note_match = re.search(r"\*\*MY NOTE:\*\*\s*([^\n]*)", body)
        verdict_raw = (verdict_match.group(1) if verdict_match else "").upper()
        note = (note_match.group(1).strip() if note_match else "")
        if verdict_raw in VERDICT_VALUES:
            out[rec_id] = {"verdict": verdict_raw, "note": note}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--md", type=Path, required=True, help="The labeled markdown file")
    parser.add_argument("--jsonl", type=Path, required=True, help="The JSONL to update in-place")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write the JSONL; just report what would change")
    args = parser.parse_args()

    md_text = args.md.read_text()
    parsed = parse_markdown(md_text)

    with open(args.jsonl) as f:
        records = [json.loads(line) for line in f if line.strip()]
    by_id = {r["id"]: r for r in records}

    n_parsed = len(parsed)
    n_in_jsonl = sum(1 for rid in parsed if rid in by_id)
    n_missing = [rid for rid in parsed if rid not in by_id]

    print(f"md: {len(md_text) // 1024} KB; parsed {n_parsed} labeled records")
    if n_missing:
        print(f"  WARN: {len(n_missing)} record ids in md not found in jsonl: {n_missing[:5]}{'...' if len(n_missing) > 5 else ''}")
    print(f"  matched {n_in_jsonl}/{n_parsed} to jsonl")

    # Apply
    changed = 0
    by_verdict = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    overrides_vs_draft = 0
    overrides_vs_existing = 0
    for rid, info in parsed.items():
        if rid not in by_id:
            continue
        rec = by_id[rid]
        new_label = info["verdict"]
        new_note = info["note"] or "(from markdown)"
        if rec.get("human_label") != new_label:
            if rec.get("human_label") is not None:
                overrides_vs_existing += 1
            rec["human_label"] = new_label
            rec["human_reason"] = new_note
            changed += 1
        else:
            # Update note even if verdict unchanged
            rec["human_reason"] = new_note
        by_verdict[new_label] += 1
        if new_label != rec["draft_label"] and new_label != "SKIP":
            overrides_vs_draft += 1

    print(f"  changes: {changed} records updated  ({overrides_vs_existing} overrode previous human labels)")
    print(f"  by verdict: {by_verdict}")
    print(f"  draft-vs-human overrides (excluding SKIP): {overrides_vs_draft}")

    # Report unlabeled
    unlabeled = [r["id"] for r in records if r["id"] not in parsed]
    if unlabeled:
        print(f"  still unlabeled in jsonl: {len(unlabeled)} records (first 5: {unlabeled[:5]})")
    else:
        print(f"  all {len(records)} records labeled")

    if args.dry_run:
        print("(dry-run; jsonl not written)")
        return

    with open(args.jsonl, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {args.jsonl}")


if __name__ == "__main__":
    sys.exit(main())
