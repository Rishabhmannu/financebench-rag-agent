"""Evaluate chunker A/B result files against dual-benchmark acceptance gates.

Usage:
  python scripts/run_chunker_ab_gate.py \
    --baseline tests/evaluation/eval_results/financebench_baseline.json \
    --candidate tests/evaluation/eval_results/financebench_docling_v2.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_scores(path: Path) -> dict:
    payload = json.loads(path.read_text())
    return payload.get("ragas") or payload.get("scores") or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Chunker A/B acceptance gate")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--min-delta", type=float, default=0.0, help="Required average metric delta")
    args = parser.parse_args()

    baseline = load_scores(Path(args.baseline))
    candidate = load_scores(Path(args.candidate))
    keys = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
    deltas = {k: float(candidate.get(k, 0.0)) - float(baseline.get(k, 0.0)) for k in keys}
    avg_delta = sum(deltas.values()) / len(keys)

    print("Deltas:")
    for k, v in deltas.items():
        print(f"  {k:18s} {v:+.4f}")
    print(f"  {'avg':18s} {avg_delta:+.4f}")

    if avg_delta < args.min_delta:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

