# Evaluation

This document is the evidence layer behind the FinanceBench-150 headline of **73.3% pass rate under a calibrated Sonnet 4.6 + v2 LLM-as-judge (Cohen's κ = 0.932 vs human labels)**. It covers the methodology, the two benchmark datasets used, the full per-sprint trajectory, and reproduction commands. For the engineering narrative (what we learned, what we rolled back, why), see [engineering-log.md](engineering-log.md).

## Evaluation methodology

### Two evaluation datasets, two roles

| Dataset | Size | Role | Final score |
|---|---|---|---|
| Internal SEC 10-K | 61 Q&A over AAPL / MSFT / TSLA FY2023 (249 Qdrant chunks) | Primary regression gate for graph + prompt changes | Faithfulness 0.811, Answer Relevancy 0.834, Context Precision 0.747 |
| FinanceBench (external) | 150 Q across 32 companies (68k Qdrant chunks from 10-K PDFs) | Co-primary external benchmark for generalization | **73.3% correctness pass rate** (Sonnet 4.6 + v2 judge, κ=0.932). Adjusted-actionable: 78.0% excluding 9 FB dataset errors |

### Multi-judge scoring

Every full evaluation runs three judging systems in parallel:

- **RAGAS** — faithfulness, answer relevancy, context precision, context recall
- **DeepEval** — faithfulness, contextual recall, contextual precision, answer relevancy
- **Custom LLM correctness judge** — pass/fail against the gold answer

RAGAS and DeepEval use gpt-4o-mini (their default). The **correctness judge** ships as **Claude Sonnet 4.6 + IMPROVED_PROMPT v2** — calibrated to **Cohen's κ = 0.932 vs hand-labeled human ground truth** on an 89-Q stratified calibration set (10 adversarial leniency-guard cases included) with a 15-Q holdout. The prior gpt-4o-mini correctness judge measured at κ=0.490 and contributed ~47% FNR (Sprint 7.13 audit + Sprint 7.14 Phase 1 details in `engineering-log.md`). The [`tests/evaluation/rejudge.py`](../tests/evaluation/rejudge.py) script can re-score any existing `*.correctness.json` against the new judge in ~3 min for ~$0.50.

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

### Phase 1 — under the gpt-4o-mini correctness judge (campaign-original judge)

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

Range across all Phase-1 canonical runs: 44.0–47.3% — *the JUDGE's ceiling, not the system's*. Refusal rate: 14.0% → 7.3% (halved). Per-eval cost trajectory: $9.70 → $5.28 (−46%) up to Sprint 7.9.

### Phase 2 — under the calibrated Sonnet 4.6 + v2 judge (Cohen's κ = 0.932)

| Sprint | Intervention | Pass rate | Δ vs prior | Cost | Status |
|---|---|:---:|:---:|:---:|:---:|
| **7.14 Phase 2** | V1 canonical config rejudged with the calibrated judge — same system, fair scoring | **68.0%** (102/150) | +22pp re-frame | ~$0.50 | rejudged |
| 7.15 | + 4 interventions (year-regex fix, decomposer prompt+cap, hallu Sonnet 4.6 upgrade, router prompt) | 72.0% (108/150) | +4.0pp | ~$17 | shipped |
| 7.15 follow-up | Fix 1 (cap revert 5→4) + Fix 2 (YoY rule) — 22-case validation | (projected −1 net) | — | ~$2 | Fix 1 reverted; Fix 2 kept |
| **7.15 final** | **+ Fix 2 (YoY rule) — full 150-Q measured re-run with multi-judge panel** | **73.3%** (110/150) | **+1.3pp** | ~$20 | **shipped** |

**Current shipped state**: 4 interventions + Fix 2 (`is X improving as of FY Y` → strictly YoY rule in decomposer). Fix 2 added 6 incremental rescues − 4 incremental regressions = **net +2 cases over the 4-intervention baseline**. Validates the YoY rule but also surfaces an over-generalization (4 regressions are cases where multi-year context was needed; "increase or decrease" and "historically consistent" phrasings were over-fitted to YoY). A targeted Fix 3 to tighten YoY trigger phrasing is deferred — net +2 beats the n=150 noise floor.

**Multi-judge panel at the Sprint 7.15 final state (vs V1 baseline)**:

| Metric | V1 baseline | 4fix + Fix 2 | Δ |
|---|---:|---:|---:|
| Correctness (κ=0.932) | 68.00% | **73.33%** | **+5.33pp** |
| RAGAS faithfulness | 0.707 | 0.733 | +0.026 |
| RAGAS context_precision | 0.733 | 0.669 | **−0.064** |
| RAGAS context_recall | 0.386 | 0.381 | ~0 |
| DeepEval faithfulness | 0.829 | 0.851 | +0.022 |
| DeepEval contextual_precision | 0.768 | 0.752 | −0.016 |
| DeepEval contextual_recall | 0.728 | **0.795** | **+0.067** |
| DeepEval answer_relevancy | — | 0.815 | — |

Trade-offs visible in the panel: retrieval **recall up, precision down** (decomposer emits more / narrower sub-queries → broader chunk pool, slightly noisier per-chunk). Faithfulness up on both judges (Sonnet 4.6 hallu upgrade landing in answer quality). Net correctness moves +5.33pp.

**Adjusted-actionable pass rate** (excluding 9 FinanceBench dataset errors verified during Sprint 7.15 residual audit): **110/141 = 78.0%** under the calibrated judge.

### Why the re-framing matters

The Sprint 7.14 judge calibration discovered that ~47% of "failures" in Phase 1 were judge bugs (PASS_JUDGE_BUG + PASS_NUMERIC_ROUNDING + PASS_OTHER categories from the 81-Q audit). The same V1 system that scored 46% under gpt-4o-mini scored 68% under the κ=0.932 judge. **The system was always in the production-RAG band; the campaign-original judge was the bottleneck for 6 sprints.** Sprint 7.15's +5.33pp on top of 68% came from genuine engineering wins exposed by component-level F1 diagnostics — measured directly under the calibrated judge, validated by an independent residual-failure audit that showed PASS_JUDGE_BUG dropping 25% → 0%. See `docs/engineering-log.md` Sprints 7.13/7.14/7.15 for the full mechanism analysis.

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

Current canonical config (Sprint 7.15 final shipped state, **73.3% pass rate under κ=0.932 judge**):

```bash
EMBEDDING_PROVIDER=voyage \
EMBEDDING_MODEL=voyage-finance-2 \
EMBEDDING_DIMENSIONS=1024 \
RERANKER_ADAPTER_PATH=data/models/reranker_ft_v1 \
python tests/evaluation/run_financebench.py \
  --output tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_4fix_plus_fix2.json \
  --collection financebench_corpus_pypdf_voyage_finance2 \
  --ragas-judge-model gpt-4o-mini \
  --deepeval-concurrency 6 \
  --flush-every 5

# Then re-judge correctness with the calibrated Sonnet 4.6 + v2 prompt
python tests/evaluation/rejudge.py \
  --input tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_4fix_plus_fix2.correctness.json \
  --parallelism 4
```

Pipeline wall time ~90 min with the upgraded Sonnet 4.6 hallu-checker (Sprint 7.15 final). Add `--skip-ragas --skip-deepeval` to drop the multi-judge panel — pure correctness pass-rate measurement only needs the local correctness scorer + rejudge (~$13 vs ~$20 with panel). Append `--resume-pipeline` to the pipeline command if interrupted — the cache is flushed every 5 questions. The rejudge `--parallelism 4` is set defensively to avoid Anthropic 529 overload errors when re-judging 150 records simultaneously.

## Co-primary benchmark governance

- SEC 61-Q is the primary regression gate for graph + prompt changes.
- FinanceBench (150 Q across 32 companies) is the co-primary external benchmark for generalization.
- Evaluation outputs include diagnostics slices (refusal rate, lookup / multi-hop / calc per-slice metrics, contamination buckets) in addition to aggregates.
- Baseline artifacts are checksum-frozen in [`tests/evaluation/eval_results/baseline_manifest.json`](../tests/evaluation/eval_results/baseline_manifest.json).
- Milestone snapshots are committed: `baseline_real_sec_fy2023.json`, `after_sprint7_5_router_fix.json`, `after_sprint7_8_voyage_finance2.json`, `after_sprint7_9_voyage_tiered_ft.json`, `financebench_pypdf_voyage_tiered_ft_litellm_v1_grader.rejudged_sonnet_v2.correctness.json` (Sprint 7.14 V1 rejudge → 68.0%), `financebench_pypdf_voyage_tiered_ft_litellm_4fix.rejudged_sonnet_v2.correctness.json` (Sprint 7.15 with 4 interventions → 72.0%), `financebench_pypdf_voyage_tiered_ft_litellm_4fix_plus_fix2.rejudged_sonnet_v2.correctness.json` (Sprint 7.15 final shipped → **73.3%**).
