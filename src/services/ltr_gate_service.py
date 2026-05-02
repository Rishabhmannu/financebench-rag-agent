"""LTR gate service.

Loads an optional XGBoost model to score candidates. If unavailable, returns
None scores and caller falls back to LLM grader.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


def build_features(query: str, candidates: list[dict], target_company: str | None, target_fiscal_year: int | None) -> list[dict]:
    """Build simple per-candidate feature dicts for gate scoring/logging."""
    feats: list[dict] = []
    for idx, chunk in enumerate(candidates):
        meta = chunk.get("metadata") or {}
        rerank_score = float(chunk.get("rerank_score", 0.0) or 0.0)
        hybrid_score = float(chunk.get("score", 0.0) or 0.0)
        chunk_text = chunk.get("content", "") or ""
        chunk_year_raw = meta.get("fiscal_year") or meta.get("fb_doc_period")
        chunk_year = int(chunk_year_raw) if str(chunk_year_raw).isdigit() else None
        feat = {
            "candidate_id": idx,
            "rerank_score": rerank_score,
            "hybrid_score": hybrid_score,
            "entity_match": 1.0 if (target_company is None or meta.get("company") == target_company) else 0.0,
            "year_match": 1.0 if (target_fiscal_year is None or chunk_year == target_fiscal_year) else 0.0,
            "chunk_len": float(len(chunk_text)),
            "is_table": 1.0 if str(meta.get("chunk_type", "")).lower() == "table" else 0.0,
        }
        feats.append(feat)
    return feats


@lru_cache(maxsize=1)
def _load_model(model_path: str):
    path = Path(model_path)
    if not path.exists():
        return None
    try:
        import xgboost as xgb
    except Exception:
        return None
    model = xgb.XGBRanker()
    model.load_model(path)
    return model


def score_candidates(features: list[dict], model_path: str) -> list[float] | None:
    """Score candidates with optional XGBoost model. Returns None when unavailable."""
    model = _load_model(model_path)
    if model is None:
        return None
    try:
        import numpy as np
    except Exception:
        return None
    x = np.array(
        [
            [
                f["rerank_score"],
                f["hybrid_score"],
                f["entity_match"],
                f["year_match"],
                f["chunk_len"],
                f["is_table"],
            ]
            for f in features
        ],
        dtype=float,
    )
    return [float(v) for v in model.predict(x)]


def dump_feature_log(path: str, query: str, features: list[dict], scores: list[float] | None) -> None:
    """Append feature rows for later LTR training/debug."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, feat in enumerate(features):
        rows.append({"query": query, "features": feat, "ltr_score": (scores[i] if scores else None)})
    with out.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

