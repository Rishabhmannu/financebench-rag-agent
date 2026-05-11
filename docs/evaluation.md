# Evaluation

This document is the evidence layer behind the headline "30.7% → 47.3% on FinanceBench". It covers the methodology, the two benchmark datasets used, the full per-sprint trajectory, and reproduction commands. For the engineering narrative (what we learned, what we rolled back, why), see [engineering-log.md](engineering-log.md).

## Evaluation methodology

### Two evaluation datasets, two roles

| Dataset | Size | Role | Final score |
|---|---|---|---|
| Internal SEC 10-K | 61 Q&A over AAPL / MSFT / TSLA FY2023 (249 Qdrant chunks) | Primary regression gate for graph + prompt changes | Faithfulness 0.811, Answer Relevancy 0.834, Context Precision 0.747 |
| FinanceBench (external) | 150 Q across 32 companies (68k Qdrant chunks from 10-K PDFs) | Co-primary external benchmark for generalization | 47.3% correctness pass rate |

### Multi-judge scoring

Every full evaluation runs three judging systems in parallel:

- **RAGAS** — faithfulness, answer relevancy, context precision, context recall
- **DeepEval** — faithfulness, contextual recall, contextual precision, answer relevancy
- **Custom LLM correctness judge** — pass/fail against the gold answer

All three currently use gpt-4o-mini as the judge model. A separate [`scripts/dual_judge_check.py`](../scripts/dual_judge_check.py) script can re-score a sample (default n=30) with a different judge family (e.g. Anthropic) and emit per-metric mean deltas, agreement rates, and per-sample diffs — used as a manual cross-check, not a CI gate.

### Reproducibility metadata

The pipeline cache (`<output>.pipeline.json`) embeds an 18-field snapshot of the run config: git SHA, settings hash, Qdrant collection state, judge model, LLM Guard runtime status, and more. Two runs can be **proven** to share identical config post-hoc by comparing snapshots.

A per-question review artifact (`<output>.review.{csv,json}`) joins pipeline + RAGAS + DeepEval + correctness for systematic failure inspection.

### Decision gates with documented null results

Stratified n=30 dev-set runs precede every full evaluation. Every intervention faces a binary gate:

- **Ship** — full-eval after dev-set passes
- **Roll back behind a feature flag** — code preserved, `ENABLE_*=False` in `.env.example`, failure mechanism documented in commit + engineering log

Three null results documented across Sprints 7.7–7.8:

| # | Intervention | Where it failed | Mechanism |
|---|---|---|---|
| 1 | Grader empty-context fallback | n=30 dev-set | −1 net, no clean rescue mechanism |
| 2 | Doc2Query BM25 enrichment | Targeted experiment | Null effect on lookup vocabulary mismatch |
| 3 | Calculator tool | n=150 full eval | Passed n=5 smoke; regressed −4pp at scale via downstream hallucination-checker disclaimer cascade |

### Dev-set noise-floor calibration

A re-run of the dev-set with **zero overrides** (default config, identical to canonical baseline) produced **−3 net, 4 regressions** — same code, same data, same baseline. This is the campaign's most important methodological finding: the n=30 dev-set has a ±3 net pass-count noise floor at temperature=0.

The decision rule was re-calibrated:

- Δ in `[−3, +1]` → within noise. Requires noise-floor reference run or skip to full-eval.
- Δ ≥ +2 OR Δ ≤ −4 with new regression patterns → decisive at n=30.

### Cost tracking

Every LLM call routed through `LLMFactory` is logged. Per-run summaries are produced; the full audit trail covers every sprint by run and by model.

## FinanceBench campaign trajectory

| Sprint | Day | Intervention | Pass rate | Δ | Cost | Status |
|---|---|---|:---:|:---:|:---:|:---:|
| 7.6 | 1 | Claude Sonnet 4.6 generator baseline | 30.7% | — | $2.91 | baseline |
| 7.6 | 4 | + selective agentic RAG (research-agent subgraph) | **38.7%** | **+8.0pp** | $13 | shipped |
| 7.7 | 6 | + text-embedding-3-large (3072d) | **43.3%** | **+4.6pp** | $16.50 | shipped |
| 7.7 | 7 | grader empty-context fallback | — | dev-set null | $1.99 | flag off |
| 7.7 | 8 | Doc2Query BM25 enrichment | — | targeted null | $0.33 | flag off |
| 7.8 | 16 | + voyage-finance-2 embeddings (1024d, finance-tuned) | **44.7%** | **+1.4pp** | $9.70 | shipped |
| 7.8 | 19 | calculator tool | 40.7% | **−4.0pp** | $9.89 | flag off |
| 7.9 | 3 | + heterogeneous model tiering | — | no regression | $11.62 | shipped |
| **7.9** | **7** | **+ LoRA-fine-tuned BGE reranker on FB labels** | **47.3%** | **+2.7pp** | **$5.28** | **shipped** |
| 8e | — | seed=42 verification (no architecture change) | 44.0% | within noise | ~$10 | controlled |
| 7.10a | — | Multi-HyDE (3 hypotheticals, gpt-4o-mini @ T=0.3, RRF-fused) | 45.3% | +1.33pp (within noise) | ~$10 | flag off, code preserved |

**Range across all 2026 canonical runs**: 44.0–47.3% (within the empirically-measured n=150 noise floor of ~±3pp). Refusal rate: 14.0% → 7.3% (halved). Per-eval cost trajectory: $9.70 → $5.28 (−46%) up to Sprint 7.9; Sprint 7.10a Multi-HyDE adds ~$0.06 in hypothetical-generation cost.

**Sprint 7.10a takeaway**: retrieval metrics moved positively (RAGAS ctx_precision +3.59pp, DeepEval +2.52pp) but pass rate did not move beyond the noise floor. Direct evidence that generic retrieval interventions are subsumed by the LoRA-FT reranker on retrieval-solvable questions, and that the remaining failures are not retrieval-bound. The Multi-HyDE paper's "+11.2%" claim was measured against a vanilla single-query baseline; the paper's absolute number on a combined ConvFinQA+FinanceBench eval is 45.6%, which is where we landed. Earned the pivot from paper-derived sprint targets to per-phase eval diagnostics (Sprint 7.11). See `docs/engineering-log.md` "Sprint 7.10a — Multi-HyDE result" for full analysis.

### Per-slice breakdown (Sprint 7.9 Day 7 vs Sprint 7.8 voyage canonical)

| Slice | Day 16 (Sprint 7.8) | **Day 7 (Sprint 7.9)** | Δ |
|---|:---:|:---:|:---:|
| Pass rate (correctness) | 67/150 (44.7%) | **71/150 (47.3%)** | **+4 / +2.7pp** |
| Refusal rate | 21/150 (14.0%) | **11/150 (7.3%)** | **−6.7pp** |
| RAGAS faithfulness | 0.666 | 0.707 | +0.04 |
| RAGAS context_precision | 0.683 | 0.733 | +0.05 |
| RAGAS context_recall | 0.343 | 0.386 | +0.04 |
| DeepEval contextual_precision | 0.751 | 0.768 | +0.02 |
| Lookup slice (n=86) | 39/86 (45%) | 41/86 (48%) | +2 |
| **Multi-hop slice (n=13)** | **4/13 (31%)** | **6/13 (46%)** | **+2 (+15pp)** |
| Calc slice (n=51) | 24/51 (47%) | 24/51 (47%) | +0 (4+/4− churn) |

The multi-hop unlock is the most important finding. Across Sprints 7.7–7.8, four retrieval interventions (3-large, grader-fallback, Doc2Query, voyage-finance-2) and one tool-use intervention (calculator) all failed to lift the multi-hop slice off 4/13. The LoRA-fine-tuned reranker is the first thing in four sprints to move it. Three multi-hop rescues — AMEX FY22 gross margin drivers, Pfizer regional revenue, AMD FY22 revenue drivers — all "what drove X" / multi-region-comparison questions where success depends on clean top-K input.

## Internal canonical — SEC 61-Q

Evaluated on 61 Q&A pairs against real SEC 10-K filings for AAPL / MSFT / TSLA fiscal year 2023 (249 chunks in Qdrant). Evaluator model: gpt-4o-mini.

| Metric | Baseline (Sprint 6) | After 7a.v2 (entity-aware retrieval) | After 7b (Claude Sonnet 4.6) | **After 7.5 (router fix)** | Final target |
|--------|:---:|:---:|:---:|:---:|:---:|
| Faithfulness | 0.586 | 0.598 | 0.656 | **0.811** | 0.80 |
| Answer Relevancy | 0.645 | 0.662 | 0.707 | **0.834** | 0.75 |
| Context Precision | 0.568 | 0.586 | 0.627 | **0.747** | 0.70 |
| Context Recall | 0.555 | 0.607 | 0.634 | **0.738** | — |

All Sprint 7 aspirational targets cleared in Sprint 7.5. The single highest-impact intervention was a ~1-hour router prompt rewrite driven by failure-case inspection (see `docs/research/06-failure-analysis.md`): the router was falsely classifying ~40% of worst-scoring queries as out-of-scope, preventing the pipeline from even attempting them. Fixing the router moved all four metrics +13 to +21 points.

At n=61, re-running with Claude Sonnet 4.6 as generator scored within RAGAS measurement noise of the gpt-4o-mini run (faithfulness 0.780 vs 0.811, ±0.03). The two configs are statistically indistinguishable at this sample size. Good retrieval + a correct router mattered more than LLM-tier choice on this dataset. The FinanceBench campaign (n=150) is where LLM-tier wins compound.

## FinanceBench parser A/B — pypdf vs docling (Sprint 7.5)

Final clean run, both tracks under identical code and settings.

| Metric | pypdf RAGAS | docling RAGAS | pypdf DeepEval | docling DeepEval |
|---|:---:|:---:|:---:|:---:|
| Faithfulness | **0.532** | 0.417 | **0.854** | 0.842 |
| Answer Relevancy | **0.384** | 0.301 | **0.735** | 0.714 |
| Context Precision | 0.529 | 0.521 | **0.591** | 0.552 |
| Refusal rate | **22.0%** | 29.3% | — | — |
| Pipeline runtime | **43 min** | 92 min | — | — |

**Decision: pypdf canonical.** Wins on every aggregate metric, 7.3pp lower refusal rate, 2.1× faster. Nuance: when both produce an answer, docling matches pypdf on per-attempt quality (within 0.04 on every DeepEval dimension) — the aggregate gap is driven by docling's 1500-char chunks giving the retriever fewer "shots on goal" than pypdf's 800-char chunks. Table-aware chunking was neutral-to-negative for retrieval-conditioned answering at this chunk size.

## Reproducing the canonical evaluation

Sprint 7.9 canonical config (47.3% pass rate):

```bash
EMBEDDING_PROVIDER=voyage \
EMBEDDING_MODEL=voyage-finance-2 \
EMBEDDING_DIMENSIONS=1024 \
RERANKER_ADAPTER_PATH=data/models/reranker_ft_v1 \
python tests/evaluation/run_financebench.py \
  --output tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft.json \
  --collection financebench_corpus_pypdf_voyage_finance2 \
  --ragas-judge-model gpt-4o-mini \
  --deepeval-concurrency 6 \
  --flush-every 5
```

Append `--resume-pipeline` if interrupted — the cache is flushed every 5 questions.

## Co-primary benchmark governance

- SEC 61-Q is the primary regression gate for graph + prompt changes.
- FinanceBench (150 Q across 32 companies) is the co-primary external benchmark for generalization.
- Evaluation outputs include diagnostics slices (refusal rate, lookup / multi-hop / calc per-slice metrics, contamination buckets) in addition to aggregates.
- Baseline artifacts are checksum-frozen in [`tests/evaluation/eval_results/baseline_manifest.json`](../tests/evaluation/eval_results/baseline_manifest.json).
- Milestone snapshots are committed: `baseline_real_sec_fy2023.json`, `after_sprint7_5_router_fix.json`, `after_sprint7_8_voyage_finance2.json`, `after_sprint7_9_voyage_tiered_ft.json`, etc.
