"""Post-process a FinanceBench eval into a per-question review artifact.

Joins four artifacts produced by `tests/evaluation/run_financebench.py`:

  - `<output>.json`            (aggregates + diagnostics)
  - `<output>.pipeline.json`   (questions, generated answers, retrieved contexts)
  - `<output>.ragas.json`      (RAGAS per-sample scores)        [optional]
  - `<output>.deepeval.json`   (DeepEval per-sample scores + reasons)
  - `<output>.correctness.json`(LLM-judge correctness per sample) [optional]

Outputs:

  - `<output>.review.json`   — full record per question (incl. retrieved chunks)
  - `<output>.review.csv`    — flat one-row-per-question table for spreadsheet review

Usage:
    python scripts/analyze_financebench_run.py \\
        --input tests/evaluation/eval_results/financebench_pypdf_clean.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from tests.evaluation.analysis_utils import (
    classify_question_type,
    is_refusal,
)


def _safe_load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"  warning: failed to parse {path}: {exc}", file=sys.stderr)
        return None


def _index_by_fb_id(per_sample: list[dict] | None) -> dict[str, dict]:
    if not per_sample:
        return {}
    return {s["fb_id"]: s for s in per_sample if s.get("fb_id")}


def build_review(input_path: Path) -> tuple[Path, Path]:
    aggregate = _safe_load(input_path)
    if aggregate is None:
        raise FileNotFoundError(f"Aggregate file not found: {input_path}")

    pipeline = _safe_load(input_path.with_suffix(".pipeline.json"))
    if pipeline is None:
        raise FileNotFoundError(f"Pipeline cache not found alongside {input_path}")

    ragas = _safe_load(input_path.with_suffix(".ragas.json"))
    deepeval = _safe_load(input_path.with_suffix(".deepeval.json"))
    correctness = _safe_load(input_path.with_suffix(".correctness.json"))

    deepeval_by_id = _index_by_fb_id((deepeval or {}).get("per_sample"))
    correctness_by_id = _index_by_fb_id((correctness or {}).get("per_sample"))
    ragas_per_sample = (ragas or {}).get("per_sample") or []

    questions = pipeline.get("questions", [])
    answers = pipeline.get("answers", [])
    contexts = pipeline.get("contexts", [])
    n = len(questions)

    # We need fb_id + gold + company per question; the deepeval/correctness
    # per_sample files carry that, but if neither exists we fall back to the
    # FinanceBench dataset on disk.
    fb_id_lookup: dict[int, dict] = {}
    for src in (deepeval_by_id.values(), correctness_by_id.values()):
        for s in src:
            # crude index by question text; deepeval keeps order so we'll trust order if matched
            pass

    # Easier path: deepeval per_sample preserves question order (one per cached q).
    deepeval_ordered = (deepeval or {}).get("per_sample") or []
    correctness_ordered = (correctness or {}).get("per_sample") or []

    review_records = []
    for i in range(n):
        q = questions[i]
        a = answers[i]
        ctxs = contexts[i] or []

        # Pull metadata from whichever per-sample source has it
        meta = {}
        if i < len(deepeval_ordered):
            meta = deepeval_ordered[i]
        elif i < len(correctness_ordered):
            meta = correctness_ordered[i]

        fb_id = meta.get("fb_id", f"index_{i}")
        company = meta.get("company")
        gold = meta.get("gold")

        deepeval_scores: dict = {}
        deepeval_reasons: dict = {}
        if i < len(deepeval_ordered):
            de = deepeval_ordered[i].get("deepeval", {})
            # de looks like {metric: {score: float, reason: str}, ...}
            for metric, payload in de.items():
                if isinstance(payload, dict):
                    deepeval_scores[metric] = payload.get("score")
                    deepeval_reasons[metric] = payload.get("reason")
                else:
                    deepeval_scores[metric] = payload

        ragas_scores = ragas_per_sample[i] if i < len(ragas_per_sample) else {}

        correctness_pass: bool | None = None
        correctness_reason: str | None = None
        if i < len(correctness_ordered):
            correctness_pass = correctness_ordered[i].get("pass")
            correctness_reason = correctness_ordered[i].get("reason")

        record = {
            "i": i,
            "fb_id": fb_id,
            "company": company,
            "slice": classify_question_type(q),
            "question": q,
            "gold": gold,
            "generated_answer": a,
            "refused": is_refusal(a),
            "empty_context": not ctxs or ctxs == [""],
            "n_chunks": len(ctxs),
            "context_chars": sum(len(c) for c in ctxs),
            "correctness_pass": correctness_pass,
            "correctness_reason": correctness_reason,
            "ragas": {k: float(v) if v is not None else None for k, v in ragas_scores.items()},
            "deepeval_scores": deepeval_scores,
            "deepeval_reasons": deepeval_reasons,
            "retrieved_contexts": ctxs,
        }
        review_records.append(record)

    # Aggregates summary header
    summary = {
        "input_file": str(input_path),
        "n_samples": n,
        "aggregate_ragas": aggregate.get("ragas"),
        "aggregate_deepeval": aggregate.get("deepeval"),
        "aggregate_correctness": aggregate.get("correctness"),
        "diagnostics": aggregate.get("diagnostics"),
    }

    review_json_path = input_path.with_suffix(".review.json")
    review_json_path.write_text(json.dumps({"summary": summary, "records": review_records}, indent=2))

    # Flat CSV for scrolling
    review_csv_path = input_path.with_suffix(".review.csv")
    csv_columns = [
        "i", "fb_id", "company", "slice", "refused", "empty_context",
        "n_chunks", "context_chars",
        "correctness_pass", "correctness_reason",
        "ragas_faithfulness", "ragas_answer_relevancy", "ragas_context_precision", "ragas_context_recall",
        "de_faithfulness", "de_answer_relevancy", "de_contextual_precision", "de_contextual_recall",
        "de_faithfulness_reason",
        "question", "gold", "generated_answer",
    ]
    with review_csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_columns, extrasaction="ignore")
        writer.writeheader()
        for r in review_records:
            row = {
                "i": r["i"],
                "fb_id": r["fb_id"],
                "company": r["company"],
                "slice": r["slice"],
                "refused": r["refused"],
                "empty_context": r["empty_context"],
                "n_chunks": r["n_chunks"],
                "context_chars": r["context_chars"],
                "correctness_pass": r["correctness_pass"],
                "correctness_reason": r["correctness_reason"],
                "ragas_faithfulness": r["ragas"].get("faithfulness"),
                "ragas_answer_relevancy": r["ragas"].get("answer_relevancy"),
                "ragas_context_precision": r["ragas"].get("context_precision"),
                "ragas_context_recall": r["ragas"].get("context_recall"),
                "de_faithfulness": r["deepeval_scores"].get("faithfulness"),
                "de_answer_relevancy": r["deepeval_scores"].get("answer_relevancy"),
                "de_contextual_precision": r["deepeval_scores"].get("contextual_precision"),
                "de_contextual_recall": r["deepeval_scores"].get("contextual_recall"),
                "de_faithfulness_reason": (r["deepeval_reasons"].get("faithfulness") or "")[:500],
                "question": r["question"],
                "gold": r["gold"],
                "generated_answer": r["generated_answer"],
            }
            writer.writerow(row)

    return review_json_path, review_csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build per-question review artifact from a FinanceBench run.")
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Aggregate JSON path (e.g. financebench_pypdf_clean.json)")
    args = parser.parse_args()

    review_json, review_csv = build_review(args.input)
    print(f"  json: {review_json}")
    print(f"  csv:  {review_csv}")

    # Quick sanity stats
    payload = json.loads(review_json.read_text())
    records = payload["records"]
    n_pass = sum(1 for r in records if r.get("correctness_pass"))
    n_refused = sum(1 for r in records if r.get("refused"))
    n_empty = sum(1 for r in records if r.get("empty_context"))
    n = len(records)
    print(f"\n  n records:            {n}")
    print(f"  correctness passes:   {n_pass} ({n_pass / n:.1%})") if n else None
    print(f"  refusals:             {n_refused} ({n_refused / n:.1%})") if n else None
    print(f"  empty_context:        {n_empty} ({n_empty / n:.1%})") if n else None
    return 0


if __name__ == "__main__":
    sys.exit(main())
