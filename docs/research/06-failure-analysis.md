# Sprint 7.5 Step 1 — Failure Case Inspection

*Date: 2026-04-22. Inspected the 20 worst-faithfulness cases from `after_sprint7b_claude_sonnet.pipeline.json`.*

## TL;DR — the hypothesis was wrong

Sprint 7.5 planned an entity-overlap filter based on the assumption that cross-entity contamination (Apple queries retrieving Microsoft chunks) was the dominant failure mode. **It isn't.** Sprint 7a.v2's existing entity extraction + Qdrant filter already prevents that class of failure almost entirely.

The actual dominant failure is **the router falsely classifying valid queries as `out_of_scope`**, causing ~40% of our worst eval cases. Entity filtering would fix zero of them.

## Failure taxonomy of the 20 worst cases

Each of the bottom-20 cases (by RAGAS faithfulness score) fell into one of four categories:

### Category A — Router false out-of-scope (40% of bottom-20, 8 cases)

The router classified these clearly-in-scope queries as out-of-scope, routing them to `out_of_scope_response` (a terminal node) before retrieval or generation ever ran. All 8 produced the identical canned refusal text *"I'm sorry, but that question is outside my scope..."*.

| # | Query | Why this is in-scope | Router verdict |
|---|-------|---------------------|----------------|
| 12 | What was Tesla's gross margin in 2023? | Direct 10-K MD&A query | ❌ out-of-scope |
| 16 | How fast did Microsoft Azure grow in fiscal year 2023? | Direct 10-K segment query | ❌ out-of-scope |
| 32 | What services did Global Consulting Partners provide? | Direct invoice content | ❌ out-of-scope |
| 38 | What is the maximum hotel rate for New York City? | Direct expense policy content | ❌ out-of-scope |
| 39 | What is the daily meal allowance for business travel? | Direct expense policy | ❌ out-of-scope |
| 40 | What approval is needed for a $50,000 business trip? | Direct expense policy | ❌ out-of-scope |
| 44 | What insurance is required for new vendor onboarding? | Direct expense policy | ❌ out-of-scope |
| 57 | How did Tesla's average vehicle selling price change in 2023? | Direct 10-K MD&A | ❌ out-of-scope |

Root cause: the ROUTER_PROMPT is sparse and interprets expense-policy and invoice detail questions as "personal" rather than "financial documents." The router is on Groq Llama 3.3 70B with a minimal classification prompt — easy to fix.

### Category B — Legitimate refusals penalized by RAGAS (25%, 5 cases)

The router correctly fired out-of-scope on these, but RAGAS's faithfulness judge compared the refusal text to ground-truth refusal text and scored 0 because the wording didn't match. **Not actual pipeline failures.**

| # | Query | Truth | Verdict |
|---|-------|-------|---------|
| 52 | Current stock price of Apple | Not in 10-K, correctly refused | ✓ correct behavior |
| 53 | What will Tesla's revenue be in 2025? | Future prediction, correctly refused | ✓ correct behavior |
| 54 | Weather forecast | Out-of-scope, correctly refused | ✓ correct behavior |
| 55 | Who is the CEO of Apple? | Not in MD&A sections we ingested | ✓ correct behavior |
| 56 | Amazon's revenue | Amazon not in corpus | ✓ correct behavior |

These inflate "failure" counts but are actually correct. Any pipeline change should not break these.

### Category C — Correct answers, RAGAS over-strict (25%, 5 cases)

These cases have answer_relevancy and context_precision at or near 1.0, but faithfulness at 0.0–0.5. The answer is objectively correct ("$450/hour", "24%", "44.1%", "GCP-2024-3291", "CONFIDENTIAL") but the RAGAS judge is penalizing phrasing differences between answer and context. **Not actual pipeline failures either.**

| # | Query | Our Answer | Ground Truth | RAGAS faith |
|---|-------|-----------|-------------|-------------|
| 11 | Apple's gross margin in 2023 | 44.1% | 44.1% | 0.50 |
| 20 | Microsoft Dynamics 365 growth | 24% | 24% | 0.50 |
| 30 | Invoice number | GCP-2024-3291 | GCP-2024-3291 | 0.33 |
| 35 | Global Consulting hourly rate | $450.00/hour | $450/hour | 0.00 |
| 37 | Confidentiality level | CONFIDENTIAL | confidential | 0.33 |

This is a well-documented RAGAS quirk — gpt-4o-mini as faithfulness judge is more conservative than gpt-4o-level judges and penalizes answers that don't have verbatim-quoted support.

### Category D — True retrieval or reasoning failures (10%, 2 cases)

| # | Query | What happened |
|---|-------|---------------|
| 6 | Which company had the highest revenue in 2023? | Answered "Microsoft $211.9B" but correct answer is Apple $383B. **Comparative query failure** — entity filter set `target_company=None` (correctly), so retrieval pulled chunks across companies but didn't surface the highest-revenue one in the top-K. |
| 50 | Do any invoices exceed Tier 4 threshold? | Refused. Requires cross-document reasoning (reading invoices + policy + comparing). Architectural limit — our pipeline doesn't do multi-document synthesis. |

These are real failures but represent a small fraction.

## What no bottom-20 case shows

**Zero of the 20 worst cases show cross-entity contamination** (e.g., "Apple query → Microsoft chunks in context"). The entity_extractor node + Qdrant company filter from Sprint 7a.v2 is working as designed.

## Implications for Sprint 7.5

The planned entity-overlap filter **would not improve any of the 20 worst cases**. The planned XGBoost stacker likewise wouldn't help — its training labels would be noise because the current grader/generator aren't the bottleneck. Both planned steps miss the actual problem.

## Revised fix hypothesis

**Fix the router prompt.** The current ROUTER_PROMPT is sparse:

```
"retrieval": The user wants to find information from financial documents
"clarification": The user is greeting, asking about capabilities, or needs clarification
"out_of_scope": The query is unrelated to financial documents
```

The router has no explicit signal that expense policies, invoice details, business-trip-approval rules, and segment-level financial metrics are all in-scope. It interprets "hotel rate" or "meal allowance" as personal-life queries.

Expected impact if we fix it:
- ~8 of 20 worst cases recover — those queries proceed to retrieval + generation instead of terminating at `out_of_scope_response`
- Given retrieval + generator are already strong (the Sprint 7b evidence), most should score faithfully
- **Projected: faithfulness 0.656 → ~0.72, answer_relevancy 0.707 → ~0.76**

This is a ~1-hour fix (prompt + router tests), not a 3-day detour.

## Revised Sprint 7.5 plan

| Step | Original plan | Revised plan |
|------|---------------|--------------|
| 1 | Inspect 20 failures (1 hr) | ✅ Done — this doc |
| 2 | Deterministic entity-overlap filter (4 hr) | **Skip** — entity contamination is not the bottleneck |
| 3 | Re-run RAGAS (20 min) | Keep — measure after router fix |
| 4 | FinanceBench adoption (1-2 days) | Keep — external benchmark still valuable |
| 5 | XGBoost LTR stacker (2 days, optional) | **Skip** — grader isn't the bottleneck either |
| NEW 2' | **Router prompt + tests fix** (1 hr) | Replaces old Step 2 |
| NEW 2'' | Evaluate cost of switching RAGAS evaluator gpt-4o-mini → gpt-4o (from benchmarking research) | May address Category C inflation |

Net effect: Sprint 7.5 shrinks from ~4 days to ~2 days (router fix + RAGAS re-run + FinanceBench), with a cleaner portfolio story: *"we benchmarked, hypothesized a retrieval filter, ran failure case inspection, and the data showed a router prompt issue was the actual bottleneck. Fixed it, validated on FinanceBench."*

## The meta-lesson for the portfolio writeup

This is itself a valuable finding. The instinct to throw more sophisticated machinery (entity-overlap classifier, XGBoost LTR stacker, ColBERT) at a retrieval precision problem was **wrong for this specific pipeline**. 20 minutes of failure case inspection saved 3 days of building the wrong thing. This is how RAGAS-driven development is supposed to work.
