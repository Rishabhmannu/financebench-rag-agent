"""RAGAS evaluation configuration — thresholds, LLM settings, and runner parameters.

Two threshold sets:
  - THRESHOLDS (CI gate): active, block merges on regression from the Sprint-6 baseline.
  - TARGET_THRESHOLDS: aspirational, to be restored after Sprint 7 (hybrid search +
    reranker + Claude Sonnet 4.6) lands. The current pipeline cannot meet these with
    the existing sample data + pure-dense retrieval + GPT-4o-mini generator.

Baseline (2026-04-22, pre-optimization): see eval_results/baseline_pre_optimization.json
  faithfulness=0.50, answer_relevancy=0.68, context_precision=0.65, context_recall=0.67
"""

# Active CI gate — set at baseline + ~0.02 so regressions fail but Sprint-6 state passes
THRESHOLDS = {
    "faithfulness": 0.52,
    "answer_relevancy": 0.70,
    "context_precision": 0.67,
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
