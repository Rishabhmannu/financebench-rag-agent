"""RAGAS evaluation configuration — thresholds, LLM settings, and runner parameters.

Two threshold sets:
  - THRESHOLDS (CI gate): active, block merges on regression from the real-data baseline.
  - TARGET_THRESHOLDS: aspirational, to be restored after Sprint 7 (hybrid search +
    reranker + Claude Sonnet 4.6) lands.

Canonical baseline (2026-04-22, real SEC FY2023 10-Ks, pure dense retrieval,
GPT-4o-mini generator): see eval_results/baseline_real_sec_fy2023.json
  faithfulness=0.586, answer_relevancy=0.645, context_precision=0.568, context_recall=0.555
"""

# Active CI gate — set at baseline + ~0.02 so regressions fail but Sprint-6 state passes
THRESHOLDS = {
    "faithfulness": 0.60,
    "answer_relevancy": 0.66,
    "context_precision": 0.58,
}

# Aspirational targets — restore as THRESHOLDS after Sprint 7
TARGET_THRESHOLDS = {
    "faithfulness": 0.80,
    "answer_relevancy": 0.75,
    "context_precision": 0.70,
}

# Informational-only thresholds (not enforced in CI)
INFORMATIONAL_THRESHOLDS = {
    "context_recall": 0.65,
}

# Evaluator LLM (used by RAGAS to score metrics)
EVALUATOR_MODEL = "gpt-4o-mini"

# Eval runner settings
EVAL_USER_ROLE = "admin"
EVAL_USER_ID = "eval_runner"
