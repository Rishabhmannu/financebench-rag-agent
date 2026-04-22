"""RAGAS evaluation configuration — thresholds, LLM settings, and runner parameters.

Two threshold sets:
  - THRESHOLDS (CI gate): active, block merges on regression from the current
    state-of-pipeline. Updated after each sprint that moves scores materially.
  - TARGET_THRESHOLDS: aspirational final goals. Gap to current state indicates
    what's left for future sprints (data expansion, retrieval tuning, etc.).

Eval score history:
  Sprint 6 baseline (2026-04-22, real SEC FY2023, pure dense retrieval, GPT-4o-mini):
    faithfulness=0.586, answer_relevancy=0.645, context_precision=0.568, context_recall=0.555

  Sprint 7a.v2 (entity-aware retrieval + hybrid + BGE rerank + contextual chunks):
    faithfulness=0.598, answer_relevancy=0.662, context_precision=0.586, context_recall=0.607

  Sprint 7b (+ Claude Sonnet 4.6 generator + hallucination with prompt caching):
    faithfulness=0.656, answer_relevancy=0.707, context_precision=0.627, context_recall=0.634

Thresholds are pinned at Sprint 7b result - 0.03 (measurement noise floor) so
the CI gate fails on real regression but survives RAGAS run-to-run variance.

Gap to TARGETS after Sprint 7:
  faithfulness     0.656 -> 0.80  (0.14 gap — data quality ceiling; expand sections)
  answer_relevancy 0.707 -> 0.75  (0.04 gap — reachable with prompt tuning or slightly better reranker)
  context_precision 0.627 -> 0.70 (0.07 gap — smaller chunks or Cohere Rerank 3.5)
"""

# Active CI gate — set at Sprint 7b results minus RAGAS measurement noise (~0.03).
# Blocks merges that regress current quality. Restore to TARGET_THRESHOLDS when
# future sprints close the data/retrieval gaps identified above.
THRESHOLDS = {
    "faithfulness": 0.62,
    "answer_relevancy": 0.68,
    "context_precision": 0.60,
}

# Aspirational final targets from the original Sprint 7 plan. Restoring these
# requires further work beyond Sprint 7 (see docstring gap analysis).
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
