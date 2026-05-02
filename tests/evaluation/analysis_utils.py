"""Shared evaluation analysis helpers (Phase 0 instrumentation)."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

REFUSAL_MARKERS = [
    "don't have enough information",
    "do not have enough information",
    "insufficient information",
    "cannot answer",
    "i don't know",
    "not enough information",
    "unable to answer",
    "no relevant information",
    # Claude's refusal templates (added 2026-05 after Sprint 7.6 Day 1 inspection)
    "couldn't find relevant information",
    "could not find relevant information",
    "couldn't find any relevant",
    "could not find any relevant",
    "this could mean the information isn't in",
    # OpenAI's refusal templates
    "i'm sorry, but i can't",
    "i cannot provide",
]


def is_refusal(answer: str | None) -> bool:
    """Heuristic refusal detector used for cross-run consistency."""
    text = (answer or "").strip()
    if not text:
        return True
    lower = text.lower()
    return any(marker in lower for marker in REFUSAL_MARKERS)


def classify_question_type(question: str) -> str:
    """Classify query shape for slice-level metrics.

    Multi-hop refers to questions that require synthesis across multiple
    sections / sources — comparisons, exclusions of components, "what drove X"
    decompositions. The bare "if " marker was previously too greedy: it
    swept up conditional-format calc questions ("Does X have improving Y? If
    Y is not relevant, explain why...") into multi-hop, conflating two
    distinct failure shapes. Tightened to specific multi-hop phrasings.
    """
    q = question.lower()
    calc_markers = (
        "ratio",
        "calculate",
        "round ",
        "year-over-year",
        "change in",
        "times has",
        "days payable",
        "return on assets",
        "turnover",
        "as a %",
        "in units of percents",
    )
    multihop_markers = (
        "exclude",          # "If we exclude the impact of M&A..."
        "compare",          # "How does X compare to Y"
        "compared to",
        "driven by",        # "decline driven by..."
        "what drove",       # "What drove operating margin change"
        "which segment",
        " vs ",             # "FY2022 vs FY2021"
        " vs.",
        "year over year",
    )

    if any(m in q for m in calc_markers):
        return "calc"
    if any(m in q for m in multihop_markers):
        return "multi_hop"
    return "lookup"


def extract_contamination_buckets(
    queries: list[str],
    contexts: list[list[str]],
    target_companies: list[str | None] | None = None,
    target_years: list[int | None] | None = None,
) -> dict:
    """Approximate contamination buckets from retrieved context text.

    This is intentionally heuristic and lightweight; it enables trend tracking
    across runs without expensive post-processing.
    """
    buckets = Counter()
    total = len(queries)

    for i in range(total):
        ctx_text = "\n".join(contexts[i] or []).lower()
        target_company = target_companies[i] if target_companies else None
        target_year = target_years[i] if target_years else None

        if target_company:
            # Basic mismatch signal: target company absent while another common
            # financebench company token appears in retrieved context.
            company_tokens = ["apple", "microsoft", "tesla", "amazon", "3m", "adobe", "amd"]
            present = {t for t in company_tokens if t in ctx_text}
            if target_company not in present and present:
                buckets["wrong_entity"] += 1

        if target_year and str(target_year) not in ctx_text and ctx_text.strip():
            buckets["wrong_period"] += 1

        if not (contexts[i] and contexts[i] != [""]):
            buckets["empty_context"] += 1

    return {
        "counts": dict(buckets),
        "rates": {k: v / total for k, v in buckets.items()} if total else {},
    }


def build_slice_summary(
    questions: list[str],
    answers: list[str],
    pass_labels: Iterable[bool] | None = None,
) -> dict:
    """Compute refusal/pass slices by question type."""
    pass_list = list(pass_labels) if pass_labels is not None else [False] * len(questions)
    by_type: dict[str, dict] = {}

    for i, q in enumerate(questions):
        q_type = classify_question_type(q)
        if q_type not in by_type:
            by_type[q_type] = {"n": 0, "refusals": 0, "passes": 0, "answered": 0, "answered_passes": 0}

        row = by_type[q_type]
        row["n"] += 1
        refusal = is_refusal(answers[i] if i < len(answers) else "")
        if refusal:
            row["refusals"] += 1
        else:
            row["answered"] += 1
            if i < len(pass_list) and pass_list[i]:
                row["answered_passes"] += 1
        if i < len(pass_list) and pass_list[i]:
            row["passes"] += 1

    for row in by_type.values():
        n = row["n"] or 1
        answered = row["answered"] or 1
        row["refusal_rate"] = row["refusals"] / n
        row["pass_rate"] = row["passes"] / n
        row["pass_when_answered"] = row["answered_passes"] / answered

    return by_type

