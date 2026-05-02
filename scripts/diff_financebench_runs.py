"""Diff two FinanceBench pipeline caches to understand WHY one beat the other.

Compares per-question: refusal vs answer, answer length, context count,
context overlap. Shows concrete examples in each failure mode so we can
decide whether to fix Docling chunking or roll back.

Usage:
    python scripts/diff_financebench_runs.py
"""

import json
from pathlib import Path

from src.services.company_registry import canonical_company_slug
from tests.evaluation.analysis_utils import build_slice_summary, extract_contamination_buckets, is_refusal

BASE_PATH = Path("tests/evaluation/eval_results/financebench_baseline.pipeline.json")
DOCL_PATH = Path("tests/evaluation/eval_results/financebench_docling.pipeline.json")
FB_QA_PATH = Path("data/raw/financebench/financebench_open_source.jsonl")


def main():
    base = json.loads(BASE_PATH.read_text())
    docl = json.loads(DOCL_PATH.read_text())
    fb_qa = [json.loads(line) for line in open(FB_QA_PATH)]
    assert base["questions"] == docl["questions"], "Question order mismatch"

    questions = base["questions"]
    base_answers = base["answers"]
    docl_answers = docl["answers"]
    base_contexts = base["contexts"]
    docl_contexts = docl["contexts"]
    ground_truths = [r["answer"] for r in fb_qa[: len(questions)]]
    companies = [r["company"] for r in fb_qa[: len(questions)]]
    fb_ids = [r["financebench_id"] for r in fb_qa[: len(questions)]]

    # Classify each question
    both_answered = []
    both_refused = []
    base_only = []  # baseline answered, docling refused
    docl_only = []  # docling answered, baseline refused
    for i in range(len(questions)):
        br = is_refusal(base_answers[i])
        dr = is_refusal(docl_answers[i])
        if br and dr:
            both_refused.append(i)
        elif not br and not dr:
            both_answered.append(i)
        elif br and not dr:
            docl_only.append(i)
        else:
            base_only.append(i)

    print("=" * 80)
    print("ANSWER-VS-REFUSAL CLASSIFICATION (n=150)")
    print("=" * 80)
    print(f"  Both answered:           {len(both_answered):3d}  ({len(both_answered) / 150 * 100:.0f}%)")
    print(f"  Both refused:            {len(both_refused):3d}  ({len(both_refused) / 150 * 100:.0f}%)")
    print(f"  Baseline ONLY answered:  {len(base_only):3d}  ({len(base_only) / 150 * 100:.0f}%)  (Docling regressed these)")
    print(f"  Docling  ONLY answered:  {len(docl_only):3d}  ({len(docl_only) / 150 * 100:.0f}%)  (Docling fixed these)")
    print()

    target_companies = [canonical_company_slug(c) for c in companies]
    target_years = [int(r["doc_period"]) if str(r.get("doc_period", "")).isdigit() else None for r in fb_qa[: len(questions)]]
    base_contam = extract_contamination_buckets(questions, base_contexts, target_companies, target_years)
    docl_contam = extract_contamination_buckets(questions, docl_contexts, target_companies, target_years)
    print("=" * 80)
    print("CONTAMINATION BUCKETS (heuristic)")
    print("=" * 80)
    print(f"  Baseline: {base_contam['counts']}")
    print(f"  Docling:  {docl_contam['counts']}")
    print()

    base_slices = build_slice_summary(questions, base_answers, None)
    docl_slices = build_slice_summary(questions, docl_answers, None)
    print("=" * 80)
    print("QUERY-TYPE REFUSAL SLICES")
    print("=" * 80)
    for key in sorted(set(base_slices) | set(docl_slices)):
        b = base_slices.get(key, {})
        d = docl_slices.get(key, {})
        print(
            f"  {key:10s}  baseline_refusal={b.get('refusal_rate', 0):.3f}  "
            f"docling_refusal={d.get('refusal_rate', 0):.3f}"
        )
    print()

    # Context stats
    base_avg_ctx = sum(len(c) for c in base_contexts) / len(base_contexts)
    docl_avg_ctx = sum(len(c) for c in docl_contexts) / len(docl_contexts)
    base_empty_ctx = sum(1 for c in base_contexts if not c or c == [""])
    docl_empty_ctx = sum(1 for c in docl_contexts if not c or c == [""])
    base_ctx_chars = [sum(len(ch) for ch in c) for c in base_contexts]
    docl_ctx_chars = [sum(len(ch) for ch in c) for c in docl_contexts]

    print("=" * 80)
    print("CONTEXT STATS (how much did retrieval return per Q?)")
    print("=" * 80)
    print(f"  Baseline: avg {base_avg_ctx:.1f} chunks/Q, avg {sum(base_ctx_chars) / len(base_ctx_chars):.0f} chars/Q, empty: {base_empty_ctx}")
    print(f"  Docling:  avg {docl_avg_ctx:.1f} chunks/Q, avg {sum(docl_ctx_chars) / len(docl_ctx_chars):.0f} chars/Q, empty: {docl_empty_ctx}")
    print()

    def _show(idx: int, tag: str) -> None:
        print("-" * 80)
        print(f"[{tag}] fb_id={fb_ids[idx]}  company={companies[idx]}")
        print(f"  Q: {questions[idx][:180]}")
        print(f"  GT: {ground_truths[idx][:300]}")
        print(f"  BASELINE ans: {base_answers[idx][:250]}")
        print(f"  DOCLING  ans: {docl_answers[idx][:250]}")
        # Top-1 retrieved chunk preview
        bc = base_contexts[idx][0] if base_contexts[idx] else ""
        dc = docl_contexts[idx][0] if docl_contexts[idx] else ""
        print(f"  BASELINE top1 ({len(bc)} chars): {bc[:300]!r}")
        print(f"  DOCLING  top1 ({len(dc)} chars): {dc[:300]!r}")

    # Regression cases — Docling refused where baseline answered
    print("=" * 80)
    print(f"REGRESSIONS — Baseline answered, Docling refused (showing 5 of {len(base_only)})")
    print("=" * 80)
    for idx in base_only[:5]:
        _show(idx, "REGRESSION")
    print()

    # Win cases — Docling fixed questions pypdf couldn't answer
    print("=" * 80)
    print(f"WINS — Docling answered, Baseline refused (showing 5 of {len(docl_only)})")
    print("=" * 80)
    for idx in docl_only[:5]:
        _show(idx, "WIN")
    print()

    # Where both answered, flag cases where answers diverge a lot (likely one is wrong)
    print("=" * 80)
    print("DIVERGENT ANSWERS — both answered but differ substantially (showing 5)")
    print("=" * 80)

    def _tokens(s: str) -> set:
        return set(w.lower() for w in s.split() if len(w) > 3)

    divergent = []
    for i in both_answered:
        bt = _tokens(base_answers[i])
        dt = _tokens(docl_answers[i])
        if not bt or not dt:
            continue
        jaccard = len(bt & dt) / len(bt | dt)
        divergent.append((jaccard, i))
    divergent.sort()
    for _, idx in divergent[:5]:
        _show(idx, "DIVERGENT")
    print()

    # Summary hypothesis check
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    net = len(docl_only) - len(base_only)
    print(f"  Net answer-rate change: {'+' if net >= 0 else ''}{net} questions ({'+' if net >= 0 else ''}{net / 150 * 100:.1f}%)")
    print(f"  Docling fixed:     {len(docl_only)} questions (ones pypdf couldn't)")
    print(f"  Docling broke:     {len(base_only)} questions (ones pypdf could)")
    print(f"  Context size Δ:    baseline={sum(base_ctx_chars) / len(base_ctx_chars):.0f} → docling={sum(docl_ctx_chars) / len(docl_ctx_chars):.0f} chars/Q")
    print()


if __name__ == "__main__":
    main()
