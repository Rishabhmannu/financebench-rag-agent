"""Parse the labeled pipeline-diagnostic markdown back into JSONL.

Reads the labeled .md file, extracts 6 fields per record (INTENT, COMPLEXITY,
TARGET_COMPANY verification, TARGET_YEAR verification, EXPECTED_SUB_QUERIES,
HALLU_GROUNDED, NOTES), and updates pipeline_diagnostic_v1.jsonl in place.
"""

import argparse
import json
import re
import sys
from pathlib import Path

INTENT_VALUES = {"retrieval", "clarification", "out_of_scope"}
COMPLEXITY_VALUES = {"simple_lookup", "research_required"}
HALLU_VALUES = {"Y", "N", "PARTIAL"}


def _extract_inline(label_pattern: str, body: str) -> str | None:
    """Find lines like '**LABEL:** `value` ...' and return the bare value."""
    m = re.search(rf"\*\*{label_pattern}:\*\*\s*`?([^`\n]+?)`?(?:\s+\(.+\))?(?:\n|$)", body)
    if not m:
        return None
    return m.group(1).strip()


def _extract_subqueries(body: str) -> list[str] | str | None:
    """Parse the EXPECTED_SUB_QUERIES section — could be 'N/A' or a numbered list."""
    m = re.search(r"\*\*EXPECTED_SUB_QUERIES:\*\*\s*\n(.+?)(?=\n\*\*[A-Z_]+:\*\*|\n---)", body, re.DOTALL)
    if not m:
        return None
    section = m.group(1).strip()
    # If section is just "N/A" (possibly with backticks)
    if re.match(r"^`?N/A`?\s*(\(.+\))?\s*$", section, re.IGNORECASE):
        return "N/A"
    # Otherwise extract numbered list items
    items = re.findall(r"^\s*\d+\.\s+(.+)$", section, re.MULTILINE)
    return items if items else section.strip()


def _extract_notes(body: str) -> str:
    m = re.search(r"\*\*NOTES:\*\*\s*(.*?)(?=\n---|\Z)", body, re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()


def parse_markdown(md_text: str) -> dict[str, dict]:
    """Returns id -> dict of labels."""
    out: dict[str, dict] = {}
    record_blocks = re.split(r"^## Record \d+ of \d+\s+—\s+`([^`]+)`", md_text, flags=re.MULTILINE)
    for i in range(1, len(record_blocks) - 1, 2):
        rec_id = record_blocks[i].strip()
        body = record_blocks[i + 1]

        intent = _extract_inline("INTENT", body)
        complexity = _extract_inline("COMPLEXITY", body)
        tc = _extract_inline("TARGET_COMPANY", body)
        ty = _extract_inline("TARGET_YEAR", body)
        subq = _extract_subqueries(body)
        hg = _extract_inline("HALLU_GROUNDED", body)
        notes = _extract_notes(body)

        # Skip records still containing __REPLACE__
        if intent == "__REPLACE__" or complexity == "__REPLACE__" or hg == "__REPLACE__":
            continue

        out[rec_id] = {
            "human_intent": intent if intent in INTENT_VALUES else None,
            "human_complexity": complexity if complexity in COMPLEXITY_VALUES else None,
            "human_target_company_correct": (tc.lower() == "ok"),
            "human_target_company_override": None if tc.lower() == "ok" else tc,
            "human_target_year_correct": (ty.lower() == "ok"),
            "human_target_year_override": None if ty.lower() == "ok" else ty,
            "human_expected_sub_queries": subq,
            "human_hallu_grounded": hg if hg in HALLU_VALUES else None,
            "human_notes": notes,
        }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--md", type=Path, required=True)
    parser.add_argument("--jsonl", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    md_text = args.md.read_text()
    parsed = parse_markdown(md_text)

    with open(args.jsonl) as f:
        records = [json.loads(line) for line in f if line.strip()]
    by_id = {r["id"]: r for r in records}

    print(f"md: {len(md_text) // 1024} KB; parsed {len(parsed)} labeled records")
    matched = sum(1 for rid in parsed if rid in by_id)
    print(f"  matched {matched}/{len(parsed)} to jsonl")

    # Per-field completeness
    n_intent = sum(1 for v in parsed.values() if v["human_intent"])
    n_complexity = sum(1 for v in parsed.values() if v["human_complexity"])
    n_hallu = sum(1 for v in parsed.values() if v["human_hallu_grounded"])
    n_subq_complex = sum(1 for v in parsed.values() if v["human_complexity"] == "research_required"
                         and v["human_expected_sub_queries"] not in (None, "N/A"))
    n_complex_total = sum(1 for v in parsed.values() if v["human_complexity"] == "research_required")
    print(f"  field completeness: intent={n_intent}  complexity={n_complexity}  hallu_grounded={n_hallu}  "
          f"sub_queries (of {n_complex_total} complex)={n_subq_complex}")

    # Apply
    changed = 0
    for rid, info in parsed.items():
        if rid not in by_id:
            continue
        rec = by_id[rid]
        for k, v in info.items():
            if rec.get(k) != v:
                rec[k] = v
                changed = 1
    print(f"  records updated: {sum(1 for r in records if r.get('human_intent'))}")

    # Report unlabeled
    unlabeled = [r["id"] for r in records if not r.get("human_intent")]
    if unlabeled:
        print(f"  still unlabeled (no INTENT): {len(unlabeled)}")

    if args.dry_run:
        print("(dry-run; jsonl not written)")
        return

    with open(args.jsonl, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {args.jsonl}")


if __name__ == "__main__":
    sys.exit(main())
