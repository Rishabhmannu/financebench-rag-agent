"""Check SEC + FinanceBench result files against co-primary gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.evaluation.financebench_eval_config import (
    FINANCEBENCH_DIAGNOSTIC_THRESHOLDS,
    FINANCEBENCH_THRESHOLDS,
)


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def check_metrics(name: str, scores: dict, thresholds: dict) -> list[str]:
    failures = []
    for k, t in thresholds.items():
        if float(scores.get(k, 0.0)) < float(t):
            failures.append(f"{name}:{k}={scores.get(k)} < {t}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Dual benchmark threshold checker")
    parser.add_argument("--sec", required=True, help="Path to SEC eval JSON")
    parser.add_argument("--financebench", required=True, help="Path to FinanceBench eval JSON")
    args = parser.parse_args()

    sec = load(Path(args.sec))
    fb = load(Path(args.financebench))

    failures = []
    failures.extend(check_metrics("sec", sec.get("scores", {}), sec.get("thresholds", {})))
    failures.extend(check_metrics("financebench", fb.get("ragas", {}), FINANCEBENCH_THRESHOLDS))

    diag = (fb.get("diagnostics") or {})
    refusal_rate = float(diag.get("refusal_rate", 1.0))
    if refusal_rate > FINANCEBENCH_DIAGNOSTIC_THRESHOLDS["max_refusal_rate"]:
        failures.append(
            f"financebench:refusal_rate={refusal_rate:.4f} > {FINANCEBENCH_DIAGNOSTIC_THRESHOLDS['max_refusal_rate']}"
        )

    if failures:
        for f in failures:
            print(f"[FAIL] {f}")
        raise SystemExit(1)
    print("All dual benchmark thresholds passed.")


if __name__ == "__main__":
    main()

