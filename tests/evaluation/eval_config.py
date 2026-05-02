"""RAGAS evaluation configuration — thresholds, LLM settings, and runner parameters.

Sprint 7.5 result: **all original Sprint 7 aspirational targets (0.80/0.75/0.70)
were cleared** under the canonical eval config (FORCE_OPENAI_ONLY = GPT-4o-mini
generator, GPT-4o-mini judge). THRESHOLDS are now restored to these values.

These thresholds gate the SEC 61-Q internal eval (the primary regression gate).
The FinanceBench 150-Q external benchmark uses its own thresholds in
`tests/evaluation/financebench_eval_config.py` and a separate runner.

Internal SEC 61-Q eval score history:
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

External FinanceBench 150-Q score history (Sprint 7.5 Step 4, 2026-05-02):
  Both tracks: identical code (git 144ac41f + reliability patches), identical
  settings (FORCE_OPENAI_ONLY=true, RERANKER_DEVICE=cpu, LLM Guard runtime
  disabled), apples-to-apples by reproducibility metadata.

  pypdf clean (canonical):
    RAGAS:    faithfulness=0.532, answer_relevancy=0.384, context_precision=0.529, context_recall=0.248
    DeepEval: faithfulness=0.854, answer_relevancy=0.735, contextual_precision=0.591, contextual_recall=0.488
    refusal_rate=22.0% (33/150), empty_context=26.0% (39/150)
    pipeline_runtime=43 min, collection points=68,059

  docling clean (rejected):
    RAGAS:    faithfulness=0.417, answer_relevancy=0.301, context_precision=0.521, context_recall=0.242
    DeepEval: faithfulness=0.842, answer_relevancy=0.714, contextual_precision=0.552, contextual_recall=0.492
    refusal_rate=29.3% (44/150), empty_context=31.3% (47/150)
    pipeline_runtime=92 min, collection points=50,253

  Decision: pypdf canonical. Wins on every aggregate metric, 7.3 pp lower
  refusal rate, 5.3 pp lower empty-context, 2.1× faster. Docling holds
  per-attempt quality (within 0.04 on every DeepEval dimension when both
  produce an answer) but loses on aggregate due to lower retrieval coverage
  from its larger 1500-char chunks → fewer chunks → fewer "shots on goal".

  Estimated pass rate: pypdf ≈ 51%, docling ≈ 50%. Peer-tier with FinanceBench
  [Patronus 2023] + FinGEAR [EMNLP 2025] baseline RAG (38–55%). Above
  GPT-4-Turbo single-chunk RAG (38–43%). Below FinGEAR graph-augmented (~55%).

  RAGAS vs DeepEval divergence on aggregate driven by refusal-handling: RAGAS
  rates refusals as 0/null on faithfulness; DeepEval rates them ~0.86 (no
  claims to verify → defaults to 1.0). Answered-only DeepEval is the more
  honest per-attempt signal: pypdf 0.85/0.93/0.80/0.66, docling 0.84/0.93/0.81/0.71.

  Real bottleneck: retrieval coverage. Both parsers cap at 26-31% empty-context
  rate. Sprint 7.6 candidates (empty-context fallback, query decomposition for
  calc questions) target this directly — see IMPLEMENTATION_PLAN.md.
"""

import os

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
EVALUATOR_MODEL = os.environ.get("RAGAS_EVALUATOR_MODEL", "gpt-4o-mini")

# Eval runner settings
EVAL_USER_ROLE = "admin"
EVAL_USER_ID = "eval_runner"
