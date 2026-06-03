"""Dual-judge agreement check for RAGAS on cached pipeline outputs.

Purpose:
  Re-score a sampled subset of cached pipeline outputs with two different
  LLM judges and emit an agreement report to detect judge drift/bias.

Typical usage:
  python scripts/dual_judge_check.py \
    --pipeline-cache tests/evaluation/eval_results/financebench_baseline.pipeline.json \
    --primary-judge openai:gpt-4o-mini \
    --secondary-judge anthropic:claude-sonnet-4-5 \
    --sample-size 30 \
    --output tests/evaluation/eval_results/financebench_dual_judge_report.json
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from ragas import EvaluationDataset, SingleTurnSample, evaluate
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness

from src.config.settings import settings

FINANCEBENCH_QA = Path("data/raw/financebench/financebench_open_source.jsonl")
SEC_EVAL_QA = Path("tests/evaluation/eval_dataset.json")


def _load_ground_truth_map(dataset_hint: str) -> dict[str, str]:
    if dataset_hint == "financebench":
        if not FINANCEBENCH_QA.exists():
            raise FileNotFoundError(f"Missing dataset: {FINANCEBENCH_QA}")
        mapping: dict[str, str] = {}
        for line in FINANCEBENCH_QA.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            mapping[row["question"]] = row["answer"]
        return mapping

    if not SEC_EVAL_QA.exists():
        raise FileNotFoundError(f"Missing dataset: {SEC_EVAL_QA}")
    data = json.loads(SEC_EVAL_QA.read_text())
    return {row["question"]: row["ground_truth"] for row in data}


def _build_judge_llm(spec: str):
    """Build RAGAS-compatible judge from provider:model spec."""
    if ":" in spec:
        provider, model = spec.split(":", 1)
    else:
        provider, model = "openai", spec
    provider = provider.strip().lower()
    model = model.strip()

    if provider == "openai":
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is required for openai judge")
        return LangchainLLMWrapper(
            ChatOpenAI(
                model=model,
                temperature=0,
                api_key=settings.OPENAI_API_KEY,
            )
        )
    if provider == "anthropic":
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is required for anthropic judge")
        return LangchainLLMWrapper(
            ChatAnthropic(
                model_name=model,
                temperature=0,
                api_key=settings.ANTHROPIC_API_KEY,
            )
        )
    raise ValueError(f"Unsupported provider in judge spec: {spec}")


def _run_ragas(
    *,
    judge_spec: str,
    questions: list[str],
    answers: list[str],
    contexts: list[list[str]],
    ground_truths: list[str],
) -> tuple[dict[str, float], list[dict]]:
    judge_llm = _build_judge_llm(judge_spec)
    metrics = [
        Faithfulness(llm=judge_llm),
        AnswerRelevancy(llm=judge_llm),
        ContextPrecision(llm=judge_llm),
        ContextRecall(llm=judge_llm),
    ]
    samples = [
        SingleTurnSample(
            user_input=q,
            response=a,
            retrieved_contexts=c,
            reference=gt,
        )
        for q, a, c, gt in zip(questions, answers, contexts, ground_truths)
    ]
    dataset = EvaluationDataset(samples=samples)
    result = evaluate(dataset=dataset, metrics=metrics, show_progress=True)
    df = result.to_pandas()

    metric_cols = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
    means = {m: float(df[m].mean()) for m in metric_cols}
    rows = []
    for i in range(len(df)):
        row = {"idx": i}
        for m in metric_cols:
            row[m] = float(df.iloc[i][m])
        rows.append(row)
    return means, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Dual-judge agreement report for RAGAS")
    parser.add_argument("--pipeline-cache", required=True, help="Path to *.pipeline.json cache")
    parser.add_argument(
        "--dataset",
        choices=["financebench", "sec"],
        default="financebench",
        help="Which dataset ground-truth mapping to use",
    )
    parser.add_argument("--primary-judge", default="openai:gpt-4o-mini", help="Primary judge provider:model")
    parser.add_argument("--secondary-judge", default="anthropic:claude-sonnet-4-5", help="Secondary judge provider:model")
    parser.add_argument("--sample-size", type=int, default=30, help="How many rows to sample from cache")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible sampling")
    parser.add_argument("--agreement-threshold", type=float, default=0.10, help="Per-metric abs diff threshold")
    parser.add_argument("--output", required=True, help="Output JSON report path")
    args = parser.parse_args()

    cache = json.loads(Path(args.pipeline_cache).read_text())
    questions_all = cache["questions"]
    answers_all = cache["answers"]
    contexts_all = cache["contexts"]
    n = len(questions_all)
    if n == 0:
        raise RuntimeError("Pipeline cache has no questions")

    gt_map = _load_ground_truth_map(args.dataset)
    indices = list(range(n))
    rng = random.Random(args.seed)
    rng.shuffle(indices)
    chosen = sorted(indices[: min(args.sample_size, n)])

    questions = [questions_all[i] for i in chosen]
    answers = [answers_all[i] for i in chosen]
    contexts = [contexts_all[i] for i in chosen]
    ground_truths = [gt_map.get(q, "") for q in questions]

    primary_means, primary_rows = _run_ragas(
        judge_spec=args.primary_judge,
        questions=questions,
        answers=answers,
        contexts=contexts,
        ground_truths=ground_truths,
    )
    secondary_means, secondary_rows = _run_ragas(
        judge_spec=args.secondary_judge,
        questions=questions,
        answers=answers,
        contexts=contexts,
        ground_truths=ground_truths,
    )

    metric_cols = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
    deltas = {m: secondary_means[m] - primary_means[m] for m in metric_cols}
    mean_abs_diffs = {}
    agreement_rates = {}
    for m in metric_cols:
        diffs = [abs(secondary_rows[i][m] - primary_rows[i][m]) for i in range(len(primary_rows))]
        mean_abs_diffs[m] = sum(diffs) / len(diffs) if diffs else 0.0
        agreement_rates[m] = (
            sum(1 for d in diffs if d <= args.agreement_threshold) / len(diffs) if diffs else 1.0
        )

    sample_rows = []
    for i, idx in enumerate(chosen):
        row = {
            "original_index": idx,
            "question": questions[i],
            "primary": {m: primary_rows[i][m] for m in metric_cols},
            "secondary": {m: secondary_rows[i][m] for m in metric_cols},
            "abs_diff": {m: abs(secondary_rows[i][m] - primary_rows[i][m]) for m in metric_cols},
        }
        sample_rows.append(row)

    report = {
        "dataset": args.dataset,
        "pipeline_cache": args.pipeline_cache,
        "sample_size": len(chosen),
        "seed": args.seed,
        "primary_judge": args.primary_judge,
        "secondary_judge": args.secondary_judge,
        "agreement_threshold": args.agreement_threshold,
        "aggregate": {
            "primary_means": primary_means,
            "secondary_means": secondary_means,
            "delta_secondary_minus_primary": deltas,
            "mean_abs_diff": mean_abs_diffs,
            "agreement_rate": agreement_rates,
        },
        "samples": sample_rows,
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))

    print("Dual-judge report saved:")
    print(out)
    print("Agreement rates:")
    for m in metric_cols:
        print(f"  {m:18s} {agreement_rates[m]:.3f}")


if __name__ == "__main__":
    main()

