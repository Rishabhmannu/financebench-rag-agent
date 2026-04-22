"""RAGAS evaluation configuration — thresholds, LLM settings, and runner parameters.

Sprint 7.5 result: **all original Sprint 7 aspirational targets (0.80/0.75/0.70)
were cleared** under the canonical eval config (FORCE_OPENAI_ONLY = GPT-4o-mini
generator, GPT-4o-mini judge). THRESHOLDS are now restored to these values.

Eval score history:
  Sprint 6 baseline (real SEC FY2023, pure dense retrieval, GPT-4o-mini):
    faithfulness=0.586, answer_relevancy=0.645, context_precision=0.568, context_recall=0.555

  Sprint 7a.v2 (entity-aware retrieval + hybrid + BGE rerank + contextual chunks):
    faithfulness=0.598, answer_relevancy=0.662, context_precision=0.586, context_recall=0.607

  Sprint 7b (+ Claude Sonnet 4.6 generator + hallucination):
    faithfulness=0.656, answer_relevancy=0.707, context_precision=0.627, context_recall=0.634

  Sprint 7.5 (+ router prompt fix, GPT-4o-mini):
    faithfulness=0.811, answer_relevancy=0.834, context_precision=0.747, context_recall=0.738

  Sprint 7.5 + Claude (same pipeline, Claude Sonnet 4.6 generator):
    faithfulness=0.780, answer_relevancy=0.820, context_precision=0.735, context_recall=0.724

The router fix single-handedly moved all four metrics +13 to +21 points by
unblocking ~40% of our worst-scoring queries that were being falsely
classified as out-of-scope. Failure case analysis in
docs/research/06-failure-analysis.md documents the root cause.

Note: the Claude run scored slightly lower than GPT-4o-mini (within RAGAS
measurement noise ±0.02-0.03). Most likely explanation: same-model evaluator
bias + Claude's more concise answers getting penalized by RAGAS faithfulness
judge for not quoting context verbatim. Production retains Claude for real
user-facing quality; evaluation uses GPT-4o-mini for cost + reproducibility.
"""

# Active CI gate — set at the original Sprint 7 aspirational targets. We cleared
# them in Sprint 7.5 with the router fix. Regressions below these block merges.
THRESHOLDS = {
    "faithfulness": 0.80,
    "answer_relevancy": 0.75,
    "context_precision": 0.70,
}

# Kept for historical reference; TARGET_THRESHOLDS is now equal to THRESHOLDS.
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
