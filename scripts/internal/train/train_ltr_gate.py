"""Train optional LTR gate from logged candidate features.

Expected input file: data/diagnostics/ltr_features.jsonl
Each line must include:
  {"query": "...", "features": {...}, "label": 0|1}

This script is intentionally simple and can be run after collecting labels
from grader outcomes/manual adjudication.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Train XGBoost LTR gate model")
    parser.add_argument("--input", default="data/diagnostics/ltr_features_labeled.jsonl")
    parser.add_argument("--output", default="data/models/ltr_gate.json")
    args = parser.parse_args()

    try:
        import numpy as np
        import xgboost as xgb
    except Exception as e:
        raise SystemExit(f"xgboost/numpy not available: {e}")

    rows = []
    for line in Path(args.input).read_text().splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    if not rows:
        raise SystemExit("No training rows found")

    x, y = [], []
    for row in rows:
        feat = row["features"]
        x.append(
            [
                feat.get("rerank_score", 0.0),
                feat.get("hybrid_score", 0.0),
                feat.get("entity_match", 0.0),
                feat.get("year_match", 0.0),
                feat.get("chunk_len", 0.0),
                feat.get("is_table", 0.0),
            ]
        )
        y.append(int(row.get("label", 0)))

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=int)

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        objective="binary:logistic",
        eval_metric="logloss",
        tree_method="hist",
    )
    model.fit(x, y)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(out)
    print(f"Saved {out} on {len(y)} samples")


if __name__ == "__main__":
    main()

