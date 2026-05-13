"""Export the pipeline diagnostic candidate set to markdown for offline labeling.

Each record has 6 fields to label, with two of them auto-prefilled (entity
verification is just a Y/N check). Mirrors the judge-calibration markdown
workflow.

After labeling, run scripts/parse_pipeline_diagnostic_md.py to feed verdicts
back into the JSONL.
"""

import argparse
import json
from pathlib import Path

DEFAULT_INPUT = Path("tests/evaluation/pipeline_diagnostic_v1.jsonl")
DEFAULT_OUTPUT = Path("tests/evaluation/pipeline_diagnostic_v1.md")

EXCERPT_CHARS = 1200


INSTRUCTIONS = """# Sprint 7.15 Phase 0 — Pipeline Diagnostic Labeling

## What this is

You're hand-labeling 75 FinanceBench questions across **5 fields** so we can measure each pipeline component against your ground truth.

| Field | What stage it diagnoses |
|---|---|
| INTENT | Router — does it correctly classify the question type? |
| COMPLEXITY | Router — does it correctly route to simple-retrieval vs research-agent? |
| TARGET_COMPANY / TARGET_YEAR | Entity extractor — does it pick the right company + fiscal year? |
| EXPECTED_SUB_QUERIES | Decomposer — can the system meaningfully break the question apart? |
| HALLU_GROUNDED | Hallucination-checker — does its existing verdict match yours? |

## Composition

- **48 still-failing cases** (priority — surfaces which stage breaks on each)
- **27 passing cases** (stratified across question types — clean PASS baselines)
- Total: 75, shuffled so failing/passing don't cluster

## Quick reference card — the labels you fill per record

```
**INTENT:**                 retrieval | clarification | out_of_scope
**COMPLEXITY:**             simple_lookup | research_required
**TARGET_COMPANY:**         OK   (or override with correct slug)
**TARGET_YEAR:**            OK   (or override with correct year)
**EXPECTED_SUB_QUERIES:**   N/A  (or 2-5 numbered sub-queries if COMPLEXITY=research_required)
**HALLU_GROUNDED:**         Y | N | PARTIAL
**NOTES:**                  (optional)
```

---

## Field-by-field rules (everything self-contained — no need to open source code)

### 1. INTENT — `retrieval` / `clarification` / `out_of_scope`

| Value | Meaning | Example FB questions |
|---|---|---|
| `retrieval` | Substantive question answerable from financial documents (10-K, 10-Q, invoices, expense policies). **~95% of FB questions are this.** | "What was Apple's FY2023 revenue?" / "Does Adobe have improving FCF conversion?" / "Compare 3M segments." |
| `clarification` | Vague greeting / "what can you do" / under-specified | "Hi" / "Tell me about finance" / "What can you help with?" |
| `out_of_scope` | Unrelated to finance | "What's the weather?" / Code questions / Recipes |

For FB questions you'll mostly type `retrieval`. Speed through this field.

### 2. COMPLEXITY — `simple_lookup` / `research_required`

**Definition (the rule the router itself uses):**

#### `simple_lookup` — single-fact retrieval

The answer is a specific number, name, date, segment, or short phrase that lives in ONE section of ONE document. Yes/no questions about company-level facts ARE simple_lookup, even when a one-line explanation accompanies the answer.

Examples that are `simple_lookup`:
- "What was Apple's FY2023 total revenue?"
- "What was 3M's FY2018 capital expenditure?"
- "Does 3M maintain a stable trend of dividend distribution?"
- "Is 3M a capital-intensive business?"
- "Is Apple's debt rating investment grade?"
- "Who is the auditor for the 2022 10-K?"
- "What is the meal per diem in San Francisco?"

#### `research_required` — multi-step

Choose this ONLY if the query has **at least one** of these explicit triggers:

1. **Explicit formula or ratio** — the question defines or names a calculation:
   - "Compute days payable outstanding (DPO) defined as 365 × avg AP / COGS"
   - "Fixed asset turnover ratio defined as revenue / average PP&E"
   - "Operating cash flow ratio = X / Y"

2. **Qualifier word** that changes what counts as a correct answer:
   - "exclude" / "excluding" / "organic" / "if we exclude" / "adjusted"

3. **Comparison** across years / segments / companies:
   - "Compare X vs Y" / "compared to" / "X vs Y" / "year-over-year direction"
   - "Is FY2022 better than FY2021?"

4. **Decomposition language** — requires management's stated drivers:
   - "What drove..." / "drivers of..." / "primarily due to..." / "explain why..."

5. **Applicability judgment** — questions that ask whether the metric even applies:
   - "If [metric] is not relevant, state that and explain why"
   - "Is the quick ratio the right metric for this business?"

6. **2+ DISTINCT financial quantities from DIFFERENT document sections** — the answer requires combining inputs from multiple parts of the doc:
   - Revenue (income statement) AND PP&E (balance sheet) for a turnover ratio
   - Cash from operations (cash flow statement) AND current liabilities (balance sheet) for op CF ratio
   - Net income (income statement) AND dividends paid (cash flow statement) for retention ratio
   - Multi-year inputs (e.g., FY2017 inventory + FY2018 inventory for an average)

Examples that are `research_required`:
- "What drove operating margin change FY22 vs FY21 for 3M?" — decomposition trigger
- "If we exclude M&A, which segment dragged 3M's growth?" — qualifier trigger
- "Compute days payable outstanding for Pepsico in FY2022" — explicit formula
- "What is the FY2017 operating cash flow ratio for Adobe?" — needs cash flow + balance sheet
- "What is the FY2019 cash conversion cycle (DIO + DSO − DPO) for General Mills?" — explicit formula + multiple inputs

#### Tiebreaker rule

**When uncertain, prefer `simple_lookup`.** The agent (research) path is slower (~90s vs ~20s) and can REGRESS simple lookups by adding unnecessary disclaimers. Only choose `research_required` when at least one trigger above is **clearly** present.

### 3. TARGET_COMPANY — verify the auto-prefilled slug

The auto-prefilled value comes from FinanceBench's own `company` field, mapped to the system's canonical slug (e.g., "3M" → `3m`, "Apple Inc." → `apple`). **In almost every case it's correct.**

- If auto-prefilled value matches the question's company → type `OK`
- If wrong (e.g., a question about "AmEx" but auto-prefilled `american_express`) → override with the correct slug

### 4. TARGET_YEAR — verify the auto-prefilled year

The auto-prefilled year comes from regex on the `doc_name` field. **Almost always correct.**

- If correct → type `OK`
- If wrong (e.g., 10-Q for Q2 of fiscal 2023 but question asks about Q1) → override with the correct year

Edge cases to watch:
- 10-Q filings — fiscal year vs calendar year can differ (e.g., Ulta's "fiscal 2022" = 12 months ending Jan 2023)
- Multi-year questions ("what was the FY2017-FY2019 average...") — pick the LATEST year of the question; that's what entity extraction targets

### 5. EXPECTED_SUB_QUERIES — only fill if COMPLEXITY = `research_required`

If COMPLEXITY = `simple_lookup`, just leave the line as `N/A`. Skip.

If COMPLEXITY = `research_required`, write 2–5 sub-queries that, combined, would cover the original question. **Each sub-query must make sense on its own** (independently retrievable). Format as a numbered list.

#### Worked examples

**Q**: "What is the FY2017 operating cash flow ratio for Adobe? (defined as cash from operations / total current liabilities)"
```
1. Adobe FY2017 cash from operations
2. Adobe FY2017 total current liabilities
```

**Q**: "What is the FY2019 cash conversion cycle for General Mills? CCC = DIO + DSO − DPO. DIO = 365 × avg inventory / COGS. DSO = 365 × avg AR / Revenue. DPO = 365 × avg AP / (COGS + change in inventory)."
```
1. General Mills FY2019 inventory and FY2018 inventory
2. General Mills FY2019 accounts receivable and FY2018 accounts receivable
3. General Mills FY2019 accounts payable and FY2018 accounts payable
4. General Mills FY2019 COGS
5. General Mills FY2019 revenue
```

**Q**: "What drove operating margin change for 3M in FY2022?"
```
1. 3M FY2022 operating margin
2. 3M FY2021 operating margin (for comparison)
3. Drivers and one-off charges affecting 3M FY2022 operating margin (litigation, impairment, restructuring)
```

**Q**: "If we exclude the Combat Arms Earplugs litigation, which 3M segment dragged growth in FY2022?"
```
1. 3M FY2022 segment growth rates
2. 3M FY2022 Combat Arms Earplugs litigation impact by segment
```

**Rules for good sub-queries:**
- Each one should be a single-source retrieval (one section of one document)
- Together they should cover ALL inputs needed for the answer
- Don't repeat the original question verbatim — break it apart
- 2-5 sub-queries is the norm; more than 5 means you're over-fragmenting

### 6. HALLU_GROUNDED — `Y` / `N` / `PARTIAL`

This judges whether the V1 system's answer is **faithful to the retrieved chunks** (not whether it's correct against the gold answer). The retrieved chunks shown below are the ones the system actually saw.

| Value | When to use |
|---|---|
| `Y` | All claims in the system's answer are supported by the retrieved chunks. If the system refused ("I don't have enough information"), that's still `Y` — it didn't fabricate. |
| `N` | The system made up numbers, cited fake sources, or asserted facts not present in the chunks. Pure hallucination. |
| `PARTIAL` | Some claims are grounded in the chunks; some are made up or asserted without support. |

#### Worked examples

**System answer**: "Apple FY2023 revenue was $383.3 billion, calculated as the sum of product sales ($298.1B) and services revenue ($85.2B) [Source: 10K Page 25]."
**Retrieved chunks**: Page 25 chunk shows: "Net sales: $383,285M. Products: $298,085M. Services: $85,200M."
→ **`Y` (grounded)** — all numbers and the source citation match the chunk.

**System answer**: "Apple FY2023 revenue was $383.3 billion driven by 12% iPhone growth [Source: 10K Page 25]."
**Retrieved chunks**: Page 25 has the revenue figure but says nothing about iPhone growth %.
→ **`PARTIAL`** — revenue claim grounded; iPhone-growth claim not in the chunk.

**System answer**: "Pfizer's Upjohn separation involved $700M in costs, 90% incurred through Q2 2023 [Source: 10-Q Page 41]."
**Retrieved chunks**: Page 41 shows exactly that text.
→ **`Y` (grounded)**.

**System answer**: "I don't have enough information in the retrieved chunks to compute this ratio."
**Retrieved chunks**: Cash flow statement chunks but no balance sheet chunks.
→ **`Y` (grounded)** — refusal IS grounded; system honestly described what it could/couldn't see.

**System answer**: "Coca-Cola's FY2022 dividend payout ratio is 0.55, calculated as $7,617M / $13,945M."
**Retrieved chunks**: Page 67 shows net income = $9,542M (not $13,945M).
→ **`N` (not grounded)** — the system fabricated the $13,945M denominator; the chunk shows a different number.

**Important**: this is judging GROUNDEDNESS in the retrieved chunks shown, NOT whether the system's final answer matches the gold answer. A correct answer can still be ungrounded (if the system happened to know it from training data rather than the chunks). An incorrect answer can be grounded (if the chunks themselves had wrong/incomplete data).

---

## How to label

For each record, find the placeholders and replace them inline:

```
**INTENT:** `__REPLACE__`         →  type retrieval / clarification / out_of_scope
**COMPLEXITY:** `__REPLACE__`     →  type simple_lookup / research_required
**TARGET_COMPANY:** `OK ...`      →  type OK or your override
**TARGET_YEAR:** `OK ...`         →  type OK or your override
**EXPECTED_SUB_QUERIES:**         →  N/A or a numbered list (if research_required)
**HALLU_GROUNDED:** `__REPLACE__` →  type Y / N / PARTIAL
**NOTES:**                        →  optional
```

When done, save the file (.md). I'll parse it back to JSONL.

## Time estimate

| Step | Per Q | Total for 75 |
|---|---|---|
| INTENT | ~5 sec | ~6 min |
| COMPLEXITY | ~30-45 sec | ~30-50 min |
| TARGET company + year (verify) | ~10 sec | ~12 min |
| EXPECTED_SUB_QUERIES (only ~30 complex Qs) | ~1-2 min × 30 | ~30-60 min |
| HALLU_GROUNDED | ~1 min | ~75 min |
| **Total** | | **~3 hours** |

You can do it in chunks — the parser handles partial files gracefully.

---

"""


def _format_record(idx, total, rec):
    lines = []
    status_flag = "**[FAILING]**" if rec["v1_pass_status"] == "FAIL" else "**[PASSING]**"
    audit_flag = f"  audit={rec['v1_audit_category']}" if rec.get("v1_audit_category") else ""
    lines.append(f"## Record {idx + 1} of {total}  —  `{rec['id']}`  {status_flag}{audit_flag}")
    lines.append("")
    lines.append(f"**fb_id:** `{rec['fb_id']}`")
    lines.append(f"**company:** {rec['company']}")
    lines.append(f"**doc:** {rec['doc_name']}")
    lines.append(f"**question_type:** {rec['question_type']}")
    lines.append("")
    lines.append("### Question")
    lines.append("")
    lines.append(rec["question"])
    lines.append("")
    lines.append("### Gold answer")
    lines.append("")
    lines.append(f"> {rec['gold']}")
    lines.append("")
    lines.append("### V1 system answer (for HALLU_GROUNDED labeling)")
    lines.append("")
    ans = rec["v1_system_answer"]
    if len(ans) > EXCERPT_CHARS:
        lines.append("```")
        lines.append(ans[:EXCERPT_CHARS] + "\n\n[... truncated, total {} chars]".format(len(ans)))
        lines.append("```")
    else:
        lines.append("```")
        lines.append(ans)
        lines.append("```")
    lines.append("")
    lines.append(f"### V1 retrieved chunks (top {len(rec['v1_retrieved_chunks_top'])} of {rec['v1_n_chunks_retrieved']} — for HALLU_GROUNDED)")
    lines.append("")
    for i, c in enumerate(rec["v1_retrieved_chunks_top"], 1):
        lines.append(f"**Chunk {i}:**")
        lines.append("```")
        lines.append(c[:800] + ("..." if len(c) > 800 else ""))
        lines.append("```")
        lines.append("")
    lines.append("### Auto-prefilled fields (verify or correct)")
    lines.append("")
    lines.append(f"- **Auto target_company_slug:** `{rec['auto_target_company_slug']}`  (display: {rec['auto_target_company_display']})")
    lines.append(f"- **Auto target_fiscal_year:** `{rec['auto_target_fiscal_year']}`")
    lines.append("")
    lines.append("### ➤ Your labels")
    lines.append("")
    lines.append("**INTENT:** `__REPLACE__`  (one of: retrieval, clarification, out_of_scope)")
    lines.append("")
    lines.append("**COMPLEXITY:** `__REPLACE__`  (one of: simple_lookup, research_required)")
    lines.append("")
    lines.append(f"**TARGET_COMPANY:** `OK` (auto={rec['auto_target_company_slug']}; override if wrong)")
    lines.append("")
    lines.append(f"**TARGET_YEAR:** `OK` (auto={rec['auto_target_fiscal_year']}; override if wrong)")
    lines.append("")
    lines.append("**EXPECTED_SUB_QUERIES:**")
    lines.append("`N/A` (only fill if COMPLEXITY = research_required; otherwise leave as N/A)")
    lines.append("")
    lines.append("**HALLU_GROUNDED:** `__REPLACE__`  (one of: Y, N, PARTIAL)")
    lines.append("")
    lines.append("**NOTES:** ")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    with open(args.input) as f:
        records = [json.loads(line) for line in f if line.strip()]
    print(f"loaded {len(records)} records from {args.input}")

    out = [INSTRUCTIONS]
    out.append(f"# Sprint 7.15 Phase 0 — Pipeline Diagnostic Labeling\n")
    out.append(f"_Total: {len(records)} records. Estimated time: ~3 hours._\n\n---\n")
    for i, rec in enumerate(records):
        out.append(_format_record(i, len(records), rec))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(out))
    print(f"wrote {args.output}  ({args.output.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
