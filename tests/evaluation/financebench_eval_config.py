"""FinanceBench evaluation gate thresholds."""

FINANCEBENCH_THRESHOLDS = {
    "faithfulness": 0.45,
    "answer_relevancy": 0.28,
    "context_precision": 0.50,
    "context_recall": 0.20,
}

# Slice-level quality constraints (diagnostics block)
FINANCEBENCH_DIAGNOSTIC_THRESHOLDS = {
    "max_refusal_rate": 0.35,
}

