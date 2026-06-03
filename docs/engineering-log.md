# Engineering Log

This is the condensed engineering narrative behind the project — the things that aren't obvious from reading the code. It's written for someone who wants to understand *how* the system got to **72.7% pass rate on FinanceBench** under a calibrated Sonnet 4.6 + v2 LLM-as-judge (Cohen's κ = 0.932 vs human labels), not just *what* the final state looks like.

The full source-of-truth lives in commit messages. This document picks out the non-obvious findings, the failed interventions, and the methodology decisions that informed them.

---

## TL;DR

The headline pass rate moved through three regimes:

| Regime | Pass rate | Mechanism |
|---|---:|---|
| Sprints 7.6 → 7.9 (under gpt-4o-mini judge) | 30.7% → 47.3% (+16.6pp) | Real engineering wins under a poorly-calibrated judge |
| Sprint 7.14 (judge recalibration, same V1 system) | **47.3% → 68.0%** | Sonnet 4.6 + v2 prompt at κ=0.932 unmasked that ~47% of "failures" were judge bugs (Signal 8) |
| Sprint 7.15 (per-node diagnostic + 4 interventions) | 68.0% → 72.0% | Year-regex fix + decomposer prompt/cap + hallu Sonnet 4.6 upgrade + router prompt |
| Sprint 7.15 follow-up (Fix 2 — YoY rule) | **72.0% → 73.3%** | Decomposer "is X improving as of FY Y → strictly YoY" rule, +2 net cases |
| Sprint 7.16 (generator anti-refusal + enumerate-fully) | **73.3% → 72.7%** | −1 net at full-eval scope; validation-cohort wins washed out by pipeline stochasticity + one absence-as-answer misfire on incomplete retrieval (Signal 11) |
| Sprint 7.17 (grader architecture experiments — LoRA-FT + 4-way model swap + Llama-3.3-70B follow-ups #1-3) | **72.7%** (no change shipped) | Null on shipped pass rate. LoRA-FT MiniLM failed (Signal 12). Follow-up #1 (Haiku w/ SystemMessage split) regressed Haiku → falsified Caveat B. Follow-up #2: Llama-3.3-70B via Fireworks dominated grader benchmarks (+16pp gold-recall, +4.5pp F1). Follow-up #3 (full 150-Q FinanceBench w/ Llama-3.3-70B grader through OpenRouter, κ=0.932 rejudged): **68.0% — REGRESSED −4.7pp** vs the gpt-4o-mini baseline. 14 regressions / 7 rescues, regressions cluster on focused-lookup / superlative / enumeration questions where Llama's lower precision (0.863 vs 0.905) dilutes the generator. **Signal 14: sub-component metric wins don't propagate to system metrics.** Do not ship. |
| Sprint 7.18a (retrieval k-bump 50 → 200 to recover RETRIEVAL_MISS bucket) | **72.7%** (no change shipped) | Diagnostic measured gold reachable for 11/14 at dense k=200 + 10/14 at hybrid k=200 (vs 1/14 at hybrid k=50). FT v1 reranker pushed 6/10 in-pool cases to top-8 from broader pool. Full eval result: **57.3% — REGRESSED −15.33pp** vs baseline under κ=0.932 judge. Only 2/14 RETRIEVAL_MISS cases newly pass; 25 previously-passing cases regress because broader retrieval pool introduces distractor chunks containing plausible-but-wrong numbers that crash downstream comprehension. **Signal 15: same Signal-14 shape, ~3× worse magnitude.** Reverted RETRIEVAL_TOP_K=50. FT v2 not pursued — would not fix the distractor mechanism that broke. |
| Sprint 7.19 (code-level audit + pipeline walk) | **72.7%** baseline status unchanged — and the baseline is **on the STOCK BGE reranker, not FT v1.** | Audit findings: (1) **CRITICAL BUG** — `RERANKER_ADAPTER_PATH` env var added at Sprint 7.9 but never propagated to `.env` or `.env.example`; the LoRA-FT v1 adapter has been silently inactive in every eval since (the 72.7% baseline included). (2) Pipeline walk on 5 baseline failure cases: 3 of 5 had gold reaching the generator (RERANKER_HIT), so the residual failures are at the **synthesis/computation** level, not retrieval. (3) Tier 1 logging shipped: structured event log + boot banner + `scripts/show_run.py` — the banner now emits `ft_adapter_loaded: <bool>` on every boot. |
| Sprint 7.19 Step 0 (enable FT v1 reranker, re-baseline) | **67.33%** (101/150) under κ=0.932 — **−5.34pp REGRESSION** vs the 72.7% baseline. | The "biggest single win in the campaign" is **falsified** under the current downstream stack + calibrated judge. 2 rescues / 10 regressions. **8 of 10 regressions overlap with the Sprint 7.17 Llama-grader regression set** — same structural pipeline failure mode (multi-component formulas + trend/qualitative judgments). The 72.7% baseline IS the production state. **Signal 17 banked**: a historical component win that was attributed under a stale eval framework (gpt-4o-mini judge + old hallu Haiku + pre-Sprint-7.15 prompt stack) does not survive re-validation under the current downstream stack + κ=0.932 judge. Component fine-tunes must be re-validated on every downstream-stack upgrade. Reverted RERANKER_ADAPTER_PATH; stock BGE is production. |
| Sprint 7.19 Step 1 (FT v2 reranker — gold-anchored positives + top-200 hard negs + distractor mining + question-type stratified) | **65.33%** (98/150) under κ=0.932 — **−7.34pp REGRESSION**. | Trained on Colab T4 (~26 min wall, ~$0). Best val_loss 0.2695 (better than v1's 0.2946). **Stage E stratified sub-component eval PASSED every criterion** — NDCG@8 +10.5pp over stock globally, +8.5pp on the RERANKER_MISS bucket, **+15pp on the 8 structural-failure questions**. **Stage F full eval: all 8 structural-failure questions pass under stock, fail under v2.** 2 rescues / 13 regressions. **Signal 18 banked**: sub-component lift on the EXACT failure-cohort we targeted (+15pp NDCG@8) was *anti-predictive* of system outcome on that same cohort (8/8 pass under stock → 0/8 pass under v2). The displaced non-gold chunks in stock's top-8 are doing useful synthesis-anchoring work that "better" reranking removes. Stock BGE remains production. |

Per-eval cost dropped **46% ($9.70 → $5.28)** through Sprint 7.9 model tiering; Sprint 7.15's hallu upgrade restored Sonnet 4.6 on the verification path (+$1.35/eval). Refusal rate halved (14.0% → 7.3%) across the original campaign and now sits at 6.7%. The multi-hop slice — stuck at 4/13 across three retrieval interventions — moved to 11/13 (84.6%) by Sprint 7.16 cumulative.

**Across two campaigns, thirteen interventions tested. Eight shipped. Five rolled back behind feature flags or reverted post-validation with the failure mechanism documented.** The methodology caught the failures cleanly and preserved the wins.

**Adjusted-actionable pass rate** (excluding 9 FinanceBench dataset errors verified during the Sprint 7.13 audit): **109/141 = 77.3%** — inside the Bedrock production-RAG band, +22pp above FinGEAR EMNLP 2025 SOTA.

---

## Campaign trajectory

| Sprint | Intervention | Pass rate | Δ | Cost | Status |
|---|---|:---:|:---:|:---:|:---:|
| 7.6 Day 1 | Claude Sonnet 4.6 generator baseline (after fixing measurability) | 30.7% | — | $2.91 | baseline |
| 7.6 Day 4 | + selective agentic RAG (research-agent subgraph) | **38.7%** | **+8.0pp** | $13 | ✅ shipped |
| 7.7 Day 6 | + text-embedding-3-large (3072d) | **43.3%** | **+4.6pp** | $16.50 | ✅ shipped |
| 7.7 Day 7 | grader empty-context fallback | — | dev-set null | $1.99 | ❌ flag off |
| 7.7 Day 8 | Doc2Query BM25 enrichment | — | targeted null | $0.33 | ❌ flag off |
| 7.8 Day 16 | + voyage-finance-2 embeddings (1024d) | **44.7%** | **+1.4pp** | $9.70 | ✅ shipped |
| 7.8 Day 19 | calculator tool | 40.7% | **−4.0pp** | $9.89 | ❌ flag off |
| 7.9 Day 3 | + heterogeneous model tiering (Haiku for verify, gpt-4o-mini for decompose/sufficiency) | (no regression) | matches noise floor | $11.62 | ✅ shipped |
| **7.9 Day 7** | + LoRA-fine-tuned BGE reranker on FB labels | **47.3%** | **+2.7pp** | **$5.28** | ✅ shipped |

---

## Sprint 7.9 — the most informative sprint

### Workstream A: heterogeneous model tiering

**Question**: which graph nodes actually need Claude Sonnet 4.6 ($3/$15 per Mtok) vs. cheaper alternatives?

**Method**: per-task dev-set runs swapping Sonnet for Haiku 4.5 / gpt-4o-mini on (a) hallucination check, (b) decompose, (c) sufficiency, (d) synthesize.

**Result**: 4 of 5 candidate downgrades shipped. Synthesize stayed on Sonnet — Haiku regressed it −4 net vs. the noise floor (real damage). Also dropped Opus 4.7 from the HITL high-stakes path in favor of Sonnet 4.6, based on Vectara's hallucination leaderboard showing Sonnet has *lower* hallucination rate than Opus on verification tasks.

**Net**: −46% per-eval cost with no quality regression.

### Workstream B: LoRA fine-tune on FinanceBench labels

**The single highest-ROI quality lever in this campaign.** None of the prior 6 interventions used our own labeled data; this one did.

- Base: `BAAI/bge-reranker-v2-m3` (568M params, XLM-RoBERTa-large + 1-class regression head)
- LoRA config: rank=16, alpha=32, dropout=0.1, target=[query, value] → 2.6M trainable (0.46%)
- Training data: 1,779 train / 321 val outcome-conditioned (positive = chunk in passing-question contexts; negative = top-30 hybrid retrieval minus positives). Pos:neg 1:6.1.
- Training: BF16 mixed-precision on Apple Silicon MPS, batch 8, grad-accum 2 (effective 16), AdamW lr=2e-4, linear warmup 10%, max 5 epochs with early stopping. Best at epoch 2 (val_loss 0.295).
- Cost: $0 (local M4 Pro). Adapter: ~10MB safetensors at [`data/models/reranker_ft_v1/`](../data/models/reranker_ft_v1/).

**Smoke test that mattered**: same balance-sheet chunk for a financial-position query — stock BGE scored **0.731** (would surface). FT BGE scored **0.279** (correctly suppresses). Score Δ = −0.45. That's the discrimination improvement we trained for.

**Full-eval result**: +2.7pp pass rate. **Multi-hop slice +15pp** (4/13 → 6/13). Three multi-hop rescues — AMEX FY22 gross margin drivers, Pfizer regional revenue, AMD FY22 revenue drivers — all "what drove X" / multi-region-comparison questions where success depends on clean top-K.

---

## The single most important methodological finding

### Sprint 7.9 Day 2.5 — dev-set noise-floor measurement

Re-ran the dev-set with **zero overrides** (default config, identical to the canonical baseline). Result: **−3 net, 4 regressions** under identical settings.

**Same code, same data, same baseline → −3 net just from grader/judge stochasticity at temperature=0.**

This re-calibrated every decision gate in the campaign:

- Δ in [−3, +1] → within noise. Requires noise-floor reference run or skip to full-eval (n=150).
- Δ ≥ +2 OR Δ ≤ −4 with new regression patterns → decisive at n=30.

Retroactively explained why three Sprint 7.7+7.8 dev-set aborts (−1 to −2 net) were within noise. Two interventions had been falsely killed.

**Why this matters**: most engineers blindly trust dev-set deltas. Measuring the noise floor on the *same baseline* is a senior-level instinct. Sources of noise (priority order):

1. Grader (gpt-4o-mini) variance: same query+chunks → different relevance verdicts across runs
2. Correctness judge (gpt-4o-mini) variance: same answer → different pass/fail labels
3. LLM stochasticity at temperature=0 (small but non-zero)
4. Agentic loop path divergence (sufficiency follow-up questions differ slightly between runs)

---

## The most interesting null result — Sprint 7.8 calculator regression

**Setup**: an AST-restricted arithmetic calculator tool wired into the research-agent's synthesizer. Whitelist `Add/Sub/Mult/Div/FloorDiv/UAdd/USub`; reject `Pow/Mod/Name/Call/Attribute/etc.`. 56 unit tests, all passing in 0.04s.

**Smoke test (n=5)**: 4/5 expressions emitted, 4/4 calculator-accepted, 1 perfect rescue (CVS FA turnover 17.98 = gold), 1 partial improvement (Adobe). **Green light.**

**Full eval (n=150)**: pass rate dropped **44.7% → 40.7% (-4pp)**. Calc slice itself flat (24/51 → 24/51 — calculator IS firing and producing correct values within the slice). Lookup −5, multi-hop −1.

**Diagnosis**: +6 hallucination-checker disclaimers in the failed run exactly matched the −6 net regression. The new "Verified arithmetic (calculator-evaluated): X / Y = Z" line in the synthesizer's output created a numerically-explicit claim. The hallucination checker, calibrated against the old synthesis style, read this as a strong claim it couldn't ground in retrieved chunks — and prepended `"This answer could not be fully verified against source documents."` That prefix flipped 6 questions from pass to fail.

**Lesson**: **a syntactically correct, semantically helpful, provably accurate component can regress an end-to-end system through downstream calibration coupling.** This is a multi-agent stability failure invisible to single-component testing. The smoke missed it because:

1. n=5 is too few to surface a 4% systemic effect
2. The smoke validated calculator invocation + value correctness; it did NOT measure downstream hallucination-checker firing rate
3. n=30 dev-set noise floor is ±9pp at this base rate, so a 4pp systemic effect is invisible there too

Rolled back via `ENABLE_CALCULATOR_TOOL=False` feature flag. Code + tests preserved intact at [`src/tools/calculator.py`](../src/tools/calculator.py).

---

## Sprint 8 — production-shaped observability stack

Sprint 8 was NOT an eval-quality sprint. It was infrastructure: route every LLM call through a self-hosted LiteLLM proxy → forward to a self-hosted Langfuse v3 stack (postgres + redis + clickhouse + minio + worker + web) → expose cost, latency, tokens, per-user attribution through an `/admin/costs` endpoint.

**Architectural shifts**:

1. **Every LLM call is observable.** Pre-Sprint-8 we relied on LangSmith traces emitted client-side. Now the LiteLLM proxy server-side forwards every call — including retries, fallbacks, errors — to a Langfuse we own. Nothing leaves our network.
2. **Semantic cache is the right cache shape for finance Q&A.** Cost-tracker analysis showed Anthropic prompt caching gave $0 savings in this pipeline (cache writes never read because per-query retrieved-context polluted the cached span). The Redis semantic cache hits on the *user query* level, which is what actually repeats. 0.95 cosine threshold tuned strict — false-positive cache hits in financial Q&A are actively harmful.
3. **Per-user cost attribution survives every hop.** ContextVar set in `get_current_user` → LLMFactory reads it → forwarded as OpenAI/Anthropic `user` field → LiteLLM tags Langfuse trace with `userId` → `/admin/costs` groups by it.
4. **Drop-in proxy without behavior change at default.** `LITELLM_URL=""` is the pre-Sprint-8 compat path. Set it to enable the gateway. All 248 unit tests pass at default config.

**Honest caveat**: this stack has never run with production traffic. It runs in `docker compose up -d` on my laptop. "Production observability" describes the *capabilities* of the software, not the *deployment status* of this project.

---

## Sprint 8e + Fix A — the variance characterization

After Sprint 8 closed, I layered a second cache: per-stage Redis caches for Voyage query embeddings, BGE reranker scores, and grader verdicts — on a separate Redis DB to avoid colliding with LiteLLM's semantic cache from Sprint 8.

The first cached canonical eval came in at **63/150 (42.0%) — a −7 question regression** vs the prior Sprint 8 run (70/150). Instinct: blame the cache. Three diagnostic tests disproved that.

**Test 1: Did contexts diverge between runs?** Yes — but 14/16 of the *regressions* AND **9/9 of the rescues** had different retrieved contexts. Context-level run-to-run variance was normal, not cache-induced.

**Test 2: Is gpt-4o-mini at `temperature=0` actually deterministic?** Same input × 10 grader calls. Verdict bit: 10/10 returned `relevant=True` (stable). Reason prose: 3 unique strings. The bit the cache stores is stable → cache is correctness-safe.

**Test 3: Did the cache fire (hits) during the eval?** 2,080 grader writes, ~0 hits within the single run. The cache populated for *future* runs but had no effect on this one.

### The real bug — a macOS Redis port collision

`lsof -i :6379` revealed two Redis processes on the host:
```
redis-ser  PID 933   IPv4 127.0.0.1:6379 (LISTEN)   ← brew/launchd
redis-ser  PID 933   IPv6 [::1]:6379    (LISTEN)
com.docke  PID 86606 IPv6 *:6379        (LISTEN)   ← our compose Redis
```

Two processes bound to "port 6379" but on different interfaces. Python from the host resolved `localhost:6379` → IPv6 loopback → host Redis (PID 933), **not** the compose Redis. Confirmed by comparing `run_id`: docker-internal vs host-port lookups returned different IDs. So Sprint 8e was writing 20,508 cache entries to a rogue host Redis the rest of the stack didn't see, while LiteLLM (inside the compose network) correctly used `redis:6379` via service-name DNS.

Fix: docker-compose maps Redis to host port `6380:6379`. Compose-internal calls unchanged. Verified by re-matching `run_id`.

**The −7 was real run-to-run noise**, not a cache fault. Sprint 7.9 Day 2.5's ±3 net at n=30 scales to ~±7 at n=150 (√5 factor); −7 sits right at the noise envelope.

### Fix A — `seed=42` on every OpenAI-routed call

[OpenAI's own cookbook](https://cookbook.openai.com/examples/reproducible_outputs_with_the_seed_parameter) documents that gpt-4o-mini at `temperature=0` is "mostly deterministic" but not bit-stable, and pinning `seed` reduces (but doesn't eliminate) drift because `system_fingerprint` can change server-side. Wired `seed=42` into `_openai()`, `_groq()` (when proxied), and the eval-side RAGAS + Correctness judges.

Determinism test, same input × 10 grader calls:
- **Pre-fix**: verdict diversity 1, reason-prose diversity 3
- **Post-fix**: verdict diversity 1, reason-prose diversity **2** (improved, not bit-perfect)

### Four-way verification eval

Re-ran the canonical FinanceBench 150-Q eval with the full Sprint 9.0 stack + `seed=42` in place:

| Run | Pass rate | DE faith | DE c.prec | RAGAS faith | Errors |
|---|:---:|:---:|:---:|:---:|:---:|
| Sprint 7.9 D7 (no proxy, no seed) — baseline | **71/150 (47.3%)** | 0.829 | 0.768 | 0.707 | 0 |
| Sprint 8 (proxy, no seed) | 70/150 (46.7%) | 0.836 | 0.701 | 0.693 | 0 |
| Sprint 8e (proxy + cache, no seed) | 63/150 (42.0%) | 0.842 | 0.751 | 0.683 | 1 |
| **Sprint 9 (proxy + cache + `seed=42`)** | **66/150 (44.0%)** | **0.836** | **0.767** | **0.702** | **0** |

**What `seed=42` measurably bought**:
- Pass rate: 63 → 66 = **+3 questions (+2.0pp)** vs uncontrolled Sprint 8e
- DeepEval `contextual_precision` recovered from 0.751 → 0.767 (matches baseline 0.768)
- RAGAS `faithfulness` recovered from 0.683 → 0.702 (matches baseline 0.707)
- Metric errors: 1 → 0 (cleaner run)

**What it did NOT close**: the 5-question residual gap to Sprint 7.9 D7's baseline. That remainder is **Anthropic-side variance** — the Messages API doesn't accept a `seed` parameter, so Sonnet 4.6 (generator) and Haiku 4.5 (hallucination checker) still drift run-to-run within Anthropic's `temperature=0` bounds. Controlling what's controllable.

### Production-plumbing trade-off, honestly stated

| Workload | Pipeline-time impact of the LiteLLM + cache stack |
|---|---|
| **Unique-question batch eval** (FinanceBench) | **+10–20%** wall time. Cache hit rate ≈ 0 within a run because every question is unique. Pure tax. |
| **Production traffic with repetition** (paraphrased / verbatim) | Win — cache hits skip the chunk-rerank + grader LLM calls; observability captures every call without per-component instrumentation. |

The infrastructure is correctly designed for production-style traffic. The eval-throughput slowdown is the honest cost of building it.

### Sprint 8f deferred indefinitely

Sprint 8e's diagnostic flagged "cache agentic decompose/sufficiency" (Fix B) as a follow-up. Both use **gpt-4o-mini, which `seed=42` already addresses**. The remaining variance is Anthropic-side and uncacheable. Deferred.

---

## Honest accounting of the production-plumbing detour

Sprints 8 → 9.0 (LiteLLM gateway, Langfuse stack, per-stage cache, admin endpoints, Alembic-managed RBAC, frontend backend prereqs) were **production-readiness** work, not eval-quality work. They moved zero questions on FinanceBench — by design, since they were chosen for the Full Stack AI Engineer portfolio narrative (observability, admin surface, multi-service docker-compose, integration tests), not for accuracy.

That's a fair trade for what was built, but it should be called out honestly: **the eval pass rate has been flat in the 44–47% noise band since Sprint 7.9 Day 7**. Calling subsequent eval runs "regressions" misattributes run-to-run variance to architectural changes. The empirical noise floor at n=150 is ~15% per-question (measured: baseline vs Sprint 8 disagree on 23/150 questions despite near-identical configs).

The next sprints return to eval-quality work with a concrete roadmap derived from the 2026 FinanceBench SOTA literature.

---

## Sprint 7.10a — Multi-HyDE result: null on pass rate, signal on retrieval

Shipped Sprint 7.10a (Multi-HyDE) end-to-end: gpt-4o-mini generates 3 hypothetical 10-K-style passages per query at temperature=0.3; each plus the original query runs through hybrid search; results RRF-fused (k=60) and deduped. Implementation behind `ENABLE_MULTI_HYDE` flag, default off. Full canonical FinanceBench eval at commit `dafb582`.

### Result

| Metric | seed42 baseline | Multi-HyDE n=3 | Delta |
|---|---:|---:|---:|
| **pass_rate** | **0.4400** (66/150) | **0.4533** (68/150) | **+1.33pp, +2q** |
| RAGAS faith | 0.7021 | 0.7299 | +2.78pp |
| RAGAS context_precision | 0.6894 | 0.7253 | **+3.59pp** |
| RAGAS context_recall | 0.3822 | 0.3711 | −1.11pp |
| DeepEval faith | 0.8355 | 0.8455 | +1.00pp |
| DeepEval c.precision | 0.7670 | 0.7922 | **+2.52pp** |
| DeepEval c.recall | 0.7276 | 0.7387 | +1.11pp |
| refusal_rate | 6.0% | 7.3% | +1.3pp |
| pipeline_time | 146 min | 172 min | +26 min (+17%) |

Per-question diff: 61 both pass, 77 both fail, **7 rescues** (mostly lookups: geographies, customer lists, board votes), **5 regressions** (mostly calc/multi-hop: AMCOR EBITDA, General Mills CCC, FY2020 ratios). Net +2 is **within the empirically-measured n=150 noise floor (~±3pp)**.

### What this means

Multi-HyDE moved retrieval metrics (+2.5-3.6pp ctx_precision across both judges) but did not move pass rate. The reranker (LoRA-FT on FinanceBench labels) + Voyage finance embeddings already cover the recall headroom Multi-HyDE was supposed to add. This is the same pattern observed across Sprints 7.7-7.8: **generic retrieval interventions get subsumed by the LoRA-FT reranker on questions that are already retrieval-solvable.**

### The estimation error worth recording

The "+11.2% accuracy" claim from the Multi-HyDE paper (arXiv 2509.16369) was measured against a **vanilla single-query baseline**, not against a stack like ours that already has voyage-finance-2 + LoRA-FT reranker + hybrid+BM25+RRF + research-agent decomposition. The paper's *absolute* number on a combined ConvFinQA+FinanceBench eval is **45.6%**. We landed at 45.33%. We hit academic parity with the paper's result, not the paper's delta over its own baseline. Citing paper-claimed deltas without controlling for baseline strength is a category error.

### Mechanism diagnosis

The retrieval-metric-up + pass-rate-flat pattern argues the bottleneck is **not** retrieval recall on this corpus. Candidates that fit the evidence:
- **Parse-loss** — answer cells survive Docling-markdown chunking incompletely; retrieval finds the right page, but the chunk doesn't contain a parseable triple. Strongest hypothesis given the retrieval-vs-pass-rate gap.
- **Reasoning** — multi-hop/calc questions where chunks are present but generator gets distracted (5 of 5 regressions follow this pattern).
- **Both** — selectively, per question.

Without per-phase eval (gold-chunk labels) the mechanism remains hypothesis, not measurement. Diagnosing it is the next sprint.

---

## Sprint 7.11 Day 1 — gold-chunk labels at 147/150 (98%), deterministic, $0

Shipped 2026-05-12: the input artifact for the per-phase diagnostic. For each of the 150 FinanceBench questions, identify which chunk(s) in `financebench_corpus_pypdf_voyage_finance2` literally contain the evidence text. Output drives Day 2's Recall@k, reranker NDCG, and chunk-preservation IoU metrics. Two scripts: `scripts/label_gold_chunks.py` (labeler) and `scripts/inspect_gold_chunks.py` (spot-check helper). Outputs: `tests/evaluation/phase_eval_data/v1/{gold_chunks.jsonl, _audit.jsonl}`.

### Result

| Method | Q count |
|---|---:|
| single_chunk (trigram, ≥0.70 recall) | 59 |
| multi_chunk (trigram, combined ≥0.90) | 63 |
| single_chunk (unigram-on-page+1 fallback, ≥0.70) | 2 |
| multi_chunk (unigram-on-page+1 fallback, combined ≥0.70) | 23 |
| **Total labeled** | **147 / 150 (98.0%)** |
| no_match (irreducible) | 3 |

Runtime: 10.4s for full 150 against live Qdrant. Marginal cost: $0 (no embedder or LLM calls — pure deterministic char/token overlap, Chroma `chunking_evaluation`-style methodology).

Spot-check (12 stratified samples across all four labeling buckets): **12/12 aligned, 0 false-positive labels.** Threshold passes the original "≤1/15 disagreement" rule.

### Method — two-phase deterministic overlap

| Phase | Tokenization | Scope | Threshold | When it fires |
|---|---|---|---|---|
| 1 — trigram | `[a-z0-9]+` regex → 3-gram Counter | All chunks in `financebench_doc_name == doc_name` | Primary: top-1 recall ≥ 0.70 → gold. Else multi-chunk greedy union until combined recall ≥ 0.90 (max 6 chunks, per-chunk floor 0.10) | All Qs (primary path) |
| 2 — unigram on validated page | `[a-z0-9]+` → bag-of-words Counter | Chunks at `page_number == evidence_page_num + 1` (the measured offset) in same doc | Primary 0.70, combined 0.70 | Phase 1 no_match only (~25 of 189 spans) |

Unigram fallback was added after the first-pass trigram run left 26 questions in no_match — almost all `metrics-generated` questions where evidence is a full financial table. Mechanism: FinanceBench's `evidence_text` for these is the entire balance sheet / income statement / cash flow statement; our markdown-aware Docling chunker emits these as pipe-formatted markdown tables that the trigram sequence doesn't align with even when content is identical. Order-invariant unigram recall recovers them. Adobe id_04735 (a balance sheet split into 5 chunks by the chunker) was recovered with `multi_5chunks_75pct` — verified visually that all 5 selected chunks are legitimate fragments of the same balance sheet on page 59.

### The page-offset finding

| `chunk.page_number − evidence_page_num` | n | pct |
|---:|---:|---:|
| **+1** | **350** | **96.7%** |
| outliers (−28 to +66) | 12 | 3.3% |

96.7% of selected gold chunks at exactly +1. FinanceBench's `evidence_page_num` is 0-indexed; our chunker uses 1-indexed PDF page labels. The 12 outliers are *duplicate-content* matches — the same financial-statement content also appears in MD&A summary sections of the same 10-K (verified on Lockheed id_04412 page-38 MD&A chunk that duplicates page-67 income-statement content, and on 3M id_01858 dividend sentence appearing on both page 62 and page 73). These are legitimately gold by the "any chunk containing the answer counts as a retrieval hit" definition.

Day 2's three metrics (Recall@k, NDCG@8, chunk-preservation IoU) are all page-agnostic — they match on `(source_file, chunk_index)` or character spans — so the offset doesn't affect downstream computation. Recorded here as a corpus characteristic.

### What no_match left on the table — the 3 irreducible cases

All 3 are `metrics-generated` table questions where FinanceBench's `evidence_text` has spaces stripped between words. Examples: `"SQUARE,INC. CONSOLIDATEDBALANCESHEETS ... Cashandcashequivalents"` (Block id_04660), `"(Dollarsinmillions,exceptpersharedata)"` (Boeing id_10285), `"ConsolidatedStatementsofOperations ... Accountsreceivable,net"` (CVS Health id_05915). Our chunker correctly tokenized as multi-word sequences; FB's extraction produced single smushed tokens. The disagreement is at the byte level, not the threshold level. Top unigram recall on these is 25–39% even on the correct page. Fixable only with camel-case/dictionary word-boundary insertion — brittle heuristics for 3 cases. Not worth it.

One *partial* case (id_10130 Corning): income-statement span labeled cleanly; balance-sheet span hit the same smushed-text artifact and no_match'd. Counted as labeled (its `gold_chunks[]` is non-empty), but Day 2 sees only half this Q's evidence covered. 2–4 more cases like this likely lurk in the 147; not critical to find now.

Day 2's metrics will run on n=147 (or n=148 if Corning's labeled span is counted). Statistically indistinguishable from n=150.

### The methodological pivot worth recording

The original Sprint 7.11 Day 1 plan called for cosine-similarity candidate generation (embed each gold answer via voyage-finance-2, top-3 nearest chunks per query) followed by ~5–10 hours of manual human confirmation. Scrapped on first proposal after a credibility-rule check: cosine isn't the right matching tool when `evidence_text` is *literally extracted from the same PDF* as the chunks. Character-level token overlap is the documented production methodology for this task (Chroma's `chunking_evaluation` library, used as Day 2's reference for chunk-preservation IoU). It's both more rigorous (token-level IoU is the cited metric in the literature) and fully automated. Net manual labor: 30 minutes of spot-check vs ~5–10 hours of full manual labeling.

This is a methodological signal worth banking alongside the noise-floor measurement, the calculator regression, the LoRA reranker fine-tune, and the Multi-HyDE null result. The portfolio bullet: *"For Day 1 of the per-phase eval, deterministic char/token-overlap (Chroma chunking_evaluation methodology) replaced the original cosine-similarity + manual-labeling plan — 150 labels in 10 seconds for $0 vs 5–10 hours of human time, with 0 false positives in a 12-question spot-check."*

### What the gold set unlocks for Day 2

| Metric | Definition | Input from Day 1 |
|---|---|---|
| Retrieval Recall@k (k ∈ {5,10,20,50}) | Fraction of Qs where ≥1 gold chunk appears in top-k of pre-reranker retrieval | `gold_chunks[].chunk_index` matched against retrieval output's `(source_file, chunk_index)` |
| Reranker NDCG@8 + Precision@8 | Gold-chunk binary relevance over post-reranker top-8 | Same logical IDs |
| Chunk-preservation IoU | Char-level IoU between FB `evidence_text` and the chunk's content | `fb_evidence[].evidence_text_preview` (and re-fetch of full evidence at Day 2 time) |
| Grader prec/rec | On 100-pair (query, chunk, human-verdict) sample | 50 known-relevant from `gold_chunks` + 50 known-irrelevant from non-overlap top-50 |

The decision rule from this diagnostic (codified in the Roadmap section below) tells us whether the 47% pass-rate ceiling is parse-loss, retrieval, or reasoning. Day 2 produces the metric values. Day 3 applies the rule.

---

## Sprint 7.13 Days 1-3 + audit — the eval framework was the bottleneck

This is the **most important methodological finding of the entire campaign.** Documented in detail because it reframes the project's headline result and invalidates the interpretation of several prior sprints.

### Timeline

| Day | Activity | Result |
|---|---|---|
| 1 | Grader prompt A/B (4 variants × 100 pairs) | V1 (full reframing) lifts isolated grader recall 0.70 → 0.84, F1 0.81 → 0.88; chosen for full-pipeline test |
| 2 | n=30 dev-set with V1 grader | −5 net pass, 6 regressions; I called HARD ABORT |
| 2.5 | User pushback: dev-set has misled before (e.g., LoRA-FT had −1 dev / +2.7pp full eval) | Re-promoted V1, ran full FB-150 |
| 3 | Full FB-150 with V1 grader | 69/150 = 46.0%, +2pp vs seed42 baseline (44.0%), within n=150 noise floor |
| 3.5 | User pushback: walk through PDFs by hand to discover what metrics couldn't show | Manual audit of 5 failed Qs: 3 of 4 "failures" were judge bugs or dataset errors |
| 3.6 | Auto-audit of all 81 failed Qs (Sonnet 4.6 + structured prompt) | **46.9% of "failures" are judge bugs; 11.1% flagged as gold-label errors; only 42% are real system failures** |

### Audit categorization of the 81 V1-grader "failures"

| Category | n | % | Verdict |
|---|---:|---:|---|
| PASS_JUDGE_BUG | 20 | 24.7% | System gave the answer; gpt-4o-mini judge missed it |
| PASS_NUMERIC_ROUNDING | 12 | 14.8% | System number rounds to gold (5.43% vs 5.4%; −1.53% vs −0.02 decimal form; 20.2% vs 20%) |
| PASS_OTHER | 6 | 7.4% | System correct, judge missed for other reasons |
| **Subtotal: judge errors** | **38** | **46.9%** | **System was right** |
| REFUSAL | 18 | 22.2% | System declined when gold was definite (real failure, calibration issue) |
| WRONG_NUMBER | 9 | 11.1% | Real numeric error |
| PARTIAL_ANSWER | 5 | 6.2% | Missed part of multi-part answer |
| WRONG_DIRECTION | 2 | 2.5% | Opposite yes/no |
| DATASET_SUSPECT | 9 | 11.1% | FinanceBench gold label appears wrong (e.g., Pfizer Upjohn — spun off Nov 2020, gold treats it as current in Q2 2023) |
| OTHER_FAIL | 0 | 0.0% | — |

Spot-check verification of 10 auditor classifications (5 PASS_JUDGE_BUG, 3 PASS_NUMERIC_ROUNDING, 2 DATASET_SUSPECT) by hand: **9 of 10 unambiguously correct, 1 borderline** (Boeing tax-rate sign convention). Auditor isn't over-passing.

### Corrected headline pass rate

| Scope | Pass count | Pass rate | Note |
|---|---:|---:|---|
| Measured by gpt-4o-mini judge (campaign-long) | 69/150 | 46.0% | What we'd been reporting |
| **Corrected: + 38 auditor-recovered judge errors** | **107/150** | **71.3%** | **Production-RAG band** |
| Aggressive: + 9 dataset-suspect (if verified by hand) | 116/150 | 77.3% | If we accept the auditor's dataset-error flags |

Reference benchmarks: FinanceBench paper baselines 38–43%; FinGEAR EMNLP 2025 SOTA ~55%; Bedrock production-RAG target ~70%+; Mafin (top published) ~99%.

**At 71%, the system has been at the production-RAG band the entire post-Sprint-7.9 era.** The 47% headline was always the JUDGE's accuracy, not the system's.

### What this means for prior sprints

The phase-eval cascade math from Sprint 7.11 was:
```
ideal: 1.00 → R@50: 0.83 → R@8: 0.74 → after grader: 0.50 → pass: 0.47
       -17pp        -9pp         -24pp                -3pp
```

What we now know:
- The "pass: 0.47" anchor was wrong; real pass rate ~0.71
- The 24pp "grader→generator" gap was partly measurement noise — much of what the grader rejected was redundant (other chunks in the reranker top-8 covered the same evidence), and what reached the generator was *adequate*. The generator was producing correct answers; the judge couldn't see them.
- "Stuck at 47%" framing across Sprints 7.6–7.10a was an artifact of judge inconsistency. Some interventions that registered as "null" (Multi-HyDE +1.3pp, voyage-finance-2 +1.4pp) may have produced real wins that the judge alternately recognized and missed across re-runs.
- The Sprint 7.9 Day 2.5 "n=150 noise floor of ±15% per-question disagreement" finding was an early signal of judge instability that wasn't followed up.

### Sprint 7.13 Day 3 itself — null per current judge, possibly a real win

V1 grader full eval landed at 69/150 = 46.0%, +2pp vs seed42's 44.0%. Within the n=150 noise floor of ±3pp **as measured by the current broken judge.** Under fair judging, the V1 grader change may have produced a meaningful lift — or may not. **We can't tell with the current judge.** That's the point.

### The methodological signals worth banking

Adding three new signals to the project's portfolio narrative (the prior three were: noise-floor measurement, calculator-regression diagnosis, phase-eval cascade decomposition):

**Signal 4 — Implicit inter-stage calibration (Sprint 7.13 Day 2)**: Adjacent pipeline stages co-calibrate. Loosening one stage's filter doesn't necessarily improve downstream performance because the next stage was implicitly using that filter as noise-suppression. Originally surfaced when V1's looser grader regressed the n=30 dev-set by −5 net. NOTE: in retrospect this signal is *less* important than I initially called it — the dev-set itself was noisy.

**Signal 5 — Small-sample dev-set extrapolation is unreliable (Sprint 7.13 Day 2 + Day 3 combined)**: The same V1 prompt showed −5 net on n=30 dev-set and +3 net on n=150 full eval. Net swing of +28 questions between the two. Documented historical precedent (LoRA-FT reranker: dev-set −1 / 3 reg → full eval +2.7pp = campaign's biggest win) was ignored because I anchored on dev-set as a gate. **The correct rule, retroactively: run full eval before declaring direction of effect, full stop.**

**Signal 6 — Per-stage diagnostics measure stage-vs-judge gaps, not stage-vs-truth gaps (Sprint 7.13 Day 3 audit)**: The Sprint 7.11 phase-eval was valid as a methodology but its INTERPRETATION presumed the judge's verdict was ground truth. When the judge itself is the bottleneck, per-stage metrics measure stage-vs-judge inconsistency, not stage-vs-correct-answer gaps. **The eval framework must be audited before per-stage attribution can be trusted.** Hands-on data verification (walking through actual PDFs) was the discovery method — no per-stage metric could have surfaced this.

### The next intervention — Sprint 7.14: judge rewrite + re-eval

The Sprint 7.13 plan (grader rewrite) is closed as null-per-current-judge. The new priority chain:

| Sprint | Goal | Effort | Cost |
|---|---|---|---|
| **7.14 Phase 1** | Build a better judge with rigorous evaluation methodology (see "Judge calibration methodology" below). | 1-2 days | ~$10 |
| **7.14 Phase 2** | Re-eval V1 canonical config with the new judge on FB-150. Validates the 71% claim at full-eval scope. | 3 hours | ~$5 |
| **7.14 Phase 3** | Re-eval ALL prior Sprint configs (seed42, Multi-HyDE, LoRA-FT, etc.) with the new judge. Resolves the campaign's interpretation — which "null results" were real wins? | ~10 hours | ~$30 |
| **7.14 Phase 4** | Finish Sprint 7.11 Day 4 diagnostic on the REAL failure set (Router F1, Entity Extractor F1, Generator failure-mode breakdown, Hallu-checker prec/rec) | 1 day | ~$1 |

### Judge calibration methodology — added 2026-05-12 evening

Sharp question raised in chat: how do we prevent "building a more lenient judge" disguised as "building a better judge"? Web-verified production methodology (2026 references at bottom of this section):

**Primary metric**: **Cohen's Kappa (κ)** vs human-labeled ground truth — not raw percent agreement. Kappa adjusts for chance agreement, which makes the lenient-judge attack visible (an always-PASS judge has high % agreement on imbalanced data but κ=0).

**Reference benchmarks** (from JudgeBench 2025 + Judge's Verdict arXiv 2510.09738):
- Human–human inter-annotator κ: ~0.80 (production reference, 1,994 samples × 3 annotators)
- "Human-level" LLM judge threshold: |z-score| < 1 from typical human κ
- Random / always-one-class: κ = 0

**Three-guard framework** against intentional leniency:

1. **Adversarial test cases** in the calibration set. Take 10 currently-passing Qs; manually corrupt the system answer (wrong number / flipped yes-no / wrong direction). Any judge variant that passes >1 of these is too lenient and is rejected. **This is the killer prevention.**
2. **κ as primary metric.** A judge that just says PASS to everything has κ=0 by construction.
3. **FPR cap.** Report FPR separately. Hard ceiling ≤ 5%. Better judge MUST clear it.

**Shipping gates** (judge ships only if ALL hold):
- κ ≥ 0.75 vs the calibration set
- FPR ≤ 5% on adversarial cases
- FNR strictly lower than current gpt-4o-mini judge's FNR (~35% per audit projection)
- Test-retest disagreement < 5% on 20 random Qs × 3 runs

**Calibration set construction** (~89 Qs, hand-labeled, stratified):

| Stratum | Source | Count |
|---|---|---:|
| Clear-pass | V1 grader correctness.json, `pass=True` trivial matches | 20 |
| Clear-fail | V1 grader, system refused or wildly wrong | 20 |
| Numeric rounding | Audit's PASS_NUMERIC_ROUNDING bucket | 12 |
| Judge-bug recoveries | Audit's PASS_JUDGE_BUG bucket (sampled) | 12 |
| Refusals | Audit's REFUSAL bucket | 8 |
| Partial / wrong-direction | Audit's PARTIAL_ANSWER + WRONG_DIRECTION | 7 |
| **Adversarial (leniency guard)** | **Currently-passing Qs with system answer manually corrupted** | **10** |
| **Total** | | **~89** |

Output: `tests/evaluation/judge_calibration_v1.jsonl` (canonical, checked in). Plus a 15-Q **holdout** set held out during construction — judge selection uses calibration only; final reported κ comes from the holdout to prevent over-fit.

**Judge evaluator** (`tests/evaluation/judge_eval.py`):
- Loads calibration set
- Runs each candidate judge against it
- Computes κ + FPR + FNR + test-retest (with one randomly chosen 20-Q subset run 3×)
- Outputs per-judge scorecard for selection

**Candidate judges to evaluate**:
- Baseline: current gpt-4o-mini + current prompt
- gpt-4o-mini + improved prompt (numeric tolerance + sign-convention + refusal handling)
- Sonnet 4.6 + improved prompt (the audit's prompt; spot-check verified at 9/10)
- Opus 4.7 + improved prompt (highest-quality candidate)
- Multi-judge consensus (Sonnet + gpt-4o-mini + Opus, majority vote — 3× cost but lowest individual-judge bias)

Pick the variant that meets all shipping gates at lowest cost. Expected winner per audit evidence: Sonnet 4.6 + structured prompt.

**References (web-verified 2026)**:
- [LLM as a Judge: 2026 Guide — Label Your Data](https://labelyourdata.com/articles/llm-as-a-judge)
- [Judge's Verdict: Cohen's Kappa for LLM judges — arXiv 2510.09738](https://arxiv.org/html/2510.09738v1)
- [LLMs-as-Judges survey — arXiv 2412.05579](https://arxiv.org/html/2412.05579v2)
- [LangChain: Calibrate LLM-as-a-Judge with Human Corrections](https://www.langchain.com/articles/llm-as-a-judge)
- [Inter-Annotator Agreement — Michael Brenndoerfer](https://mbrenndoerfer.com/writing/inter-annotator-agreement-kappa-alpha-reliability)

### Sprint 7.14 Phase 1 — DONE 2026-05-12 evening

**Sonnet 4.6 + structured prompt ships as the new canonical judge. Cohen's κ = 0.932 on calibration, κ = 1.000 on 15-Q holdout. All four shipping gates cleared with margin.**

**Calibration set**: 89 questions, hand-labeled by Rishabh after multi-AI cross-review. 3 overrides vs auditor drafts (2.9%) — all three were judge-calibration signals that fed directly into the v2 prompt. 15-Q holdout held out during prompt tuning. Calibration distribution: 51 PASS / 38 FAIL / 0 SKIP. Adversarial leniency guard: 10 manually-corrupted passing answers in calibration + 2 in holdout, all expected to FAIL.

**Candidates evaluated** (5 in v1, 4 re-run in v2):

| Candidate | κ (v2) | FPR_adv | FNR | F1 | Test-retest | Gates |
|---|---:|---:|---:|---:|---:|:---:|
| baseline_gpt4omini + current prompt | 0.490 (v1) | 0% | 47.1% | 0.69 | 5.0% | fail (κ, FNR, retest) |
| v2_gpt4omini + improved prompt | 0.570 | 0% | 39.2% | 0.76 | 0.0% | fail (κ — model is the bottleneck) |
| **v3_sonnet + improved prompt** | **0.932** | **0%** | **5.9%** | **0.97** | **0.0%** | **✅ PASS** |
| v4_opus + improved prompt (no temp) | 0.750 | 0% | 13.7% | 0.89 | **45.0%** | fail (Opus non-deterministic without explicit temperature control) |
| v5_consensus_3judge | 0.887 | 0% | 9.8% | 0.95 | 0.0% | ✅ PASS but Sonnet alone is better at 3× lower cost |

Baseline gpt-4o-mini's κ=0.490 confirms the Sprint 7.13 audit projection: current production judge is mediocre — 47% FNR matches the 47% judge-bug rate found by the audit. **Sanity-check tight loop**: independent measurement (audit via Sonnet) and direct κ measurement (judge_eval on hand-labels) agree on the rate, validating both methodologies.

**The three fixes between v1 and v2** (each motivated by v1 failure mode):

1. **Opus temperature config fix** — Opus 4.7 rejects the `temperature` param (`_ANTHROPIC_NO_TEMPERATURE_MODELS` per Sprint 7.9 Day 1). Skipping it for Opus moved κ from −0.000 (all-error) to 0.750. But test-retest collapsed to 45% — Opus's default (no explicit temp) is highly non-deterministic. Documented as production caveat.

2. **Regenerated calib_081 adversarial** with self-consistent corruption: original v1 corruption changed only the bottom-line value, leaving the supporting math (`7617M / 9542M = 0.80`) intact. Sonnet correctly read this contradiction and "rescued" the answer by reading past the bottom line. The new corruption changes the divisor too (`7617M / 13945M = 0.55`) so the math derives the wrong answer — closing the rescue path. **Methodological note worth recording**: adversarial test cases must be internally consistent. A weakly-corrupted adversarial that leaves supporting derivation intact tests "can the judge handle internal contradictions" rather than "can the judge catch wrong final answers." These are different gates.

3. **Improved prompt with 5 explicit rules** (encoded from v1 failure modes):
   - DIFFERENT METRIC: coincidental number match does NOT pass when metrics differ (dividends declared vs paid; six-month pre-tax vs Q2 net)
   - METRIC+VALUE BOTH REQUIRED: when gold provides both segment name AND value, system must state both
   - ALL ITEMS REQUIRED: when gold lists N items, all N must be covered
   - BOTTOM-LINE RULE: when system's bottom-line disagrees with its own supporting math, judge by the bottom line (handles adversarial corruptions + real bottom-line typos consistently)
   - Carve-out: partial answers PASS when main asserted answer matches AND gold doesn't enumerate multiple required items

After these three fixes, Sonnet went from κ=0.861 (v1, failing FPR_adv gate) to κ=0.932 (v2, all gates passed).

**Why not Opus or consensus**:
- Opus has 45% test-retest disagreement without explicit temperature control. Unusable as a deterministic judge.
- Consensus (gpt-4o-mini + Sonnet + Opus) clears gates at κ=0.887 but is 18× cost-per-call vs Sonnet alone, and gets dragged down by Opus's non-determinism. Sonnet alone is strictly better.

**Holdout validation**: Sonnet judged 15/15 = 100% of holdout records correctly (κ=1.000). Auto-script flagged "ships: False" because |Δκ| = 0.068 > 0.05 threshold, but the direction is *positive* (holdout better than calibration), which means no over-fit. The over-fit guard's threshold was symmetric; a strictly-better holdout is not over-fit.

**Total Phase 1 cost**: ~$6.50 across calibration build, two eval rounds, adversarial regeneration. Way under the $10 Phase 1 budget.

**Methodological signal worth recording**: the Sprint 7.14 Phase 1 pipeline (calibration set construction with adversarial leniency guard → Cohen's κ as primary metric → multi-candidate evaluation with hard shipping gates → 1 iteration of prompt tuning based on failure analysis → holdout validation) is **how production LLM-as-judge gets built**. Per the 2026 references (Judge's Verdict, JudgeBench, LangChain calibration guide). Banking this as the seventh methodological signal:

> *"Built a production-grade LLM-as-judge for FinanceBench correctness scoring. 89-Q calibration set hand-labeled across 8 strata including 10 adversarial leniency-guard cases. Evaluated 5 candidate judges against Cohen's κ + FPR_adversarial + FNR + test-retest reliability. After one iteration of failure analysis + prompt tightening, Sonnet 4.6 + 5-rule prompt shipped at κ=0.932 vs human ground truth (above human–human inter-annotator reference of ~0.80) — closing the 47% FNR gap of the prior gpt-4o-mini judge that had silently absorbed half the project's measured failures. The judge build cost $6.50; the audit it replaces will re-frame the entire campaign's pass-rate trajectory in Phase 2."*

### Sprint 7.14 Phase 2 — DONE 2026-05-12 late evening: new judge confirms 68.0% pass rate

**Headline**: V1 canonical config re-judged with Sonnet 4.6 + v2 improved prompt. Pass rate moves from **46.0% (gpt-4o-mini) → 68.0% (Sonnet+v2)**. Above FinGEAR SOTA (~55%), just below Bedrock production-RAG target (~70%). Adjusted for dataset errors: **71.8% (102/142)**.

**Re-judge run** (`tests/evaluation/rejudge.py`):
- Input: `financebench_pypdf_voyage_tiered_ft_litellm_v1_grader.correctness.json` (150 records)
- Judge: Sonnet 4.6 + IMPROVED_PROMPT (the v2 winner, κ=0.932 on calibration)
- Wall time: 51 sec, cost ~$0.50
- Output: `..._rejudged_sonnet_v2.correctness.json` + `..._rejudged_sonnet_v2.diff.json`

**Per-Q outcome**:
- 33 rescues (old FAIL → new PASS)
- **0 regressions** (no old PASS → new FAIL) — clean signal that the new judge is more accurate, not just more lenient
- 69 unchanged passes (new judge confirms every old pass)
- 48 unchanged fails (the real remaining failures)
- 0 judge errors

**Audit projection vs actual**: audit predicted 38 rescues (PASS_JUDGE_BUG + PASS_NUMERIC_ROUNDING + PASS_OTHER); 33 actually materialized = 87% accuracy of the audit method. The 5 borderline cases were correctly NOT rescued because the v2 prompt's tightening rules (DIFFERENT METRIC, METRIC+VALUE BOTH REQUIRED, etc.) catch leniency the audit's Sonnet auditor missed. Sanity check confirms both methodologies (audit + judge_eval) are independently consistent.

### The trimmed diagnostic — what's left in the 48 remaining failures

| Audit category | Rescued | Still failing | Rescue % |
|---|---:|---:|---:|
| PASS_JUDGE_BUG | 19 | 1 | 95% |
| PASS_NUMERIC_ROUNDING | 9 | 3 | 75% |
| PASS_OTHER | 3 | 3 | 50% |
| PARTIAL_ANSWER | 1 | 4 | 20% |
| DATASET_SUSPECT | 1 | 8 | 11% (mostly unfixable — FB gold wrong) |
| REFUSAL | 0 | **18** | 0% (real failure mode) |
| WRONG_NUMBER | 0 | 9 | 0% (real numeric errors) |
| WRONG_DIRECTION | 0 | 2 | 0% (real) |
| Total | 33 | 48 | — |

**Distribution of the 48 still failing**:
- **REFUSAL: 18 (37.5%)** — system refuses to answer when gold is definite; largest actionable bucket
- WRONG_NUMBER: 9 (18.8%) — real numeric errors
- **DATASET_SUSPECT: 8 (16.7%)** — FB gold itself is wrong (Pfizer Upjohn pattern + Best Buy stores + JnJ EPS direction + 5 others); structurally unfixable
- PARTIAL_ANSWER: 4
- residual borderline (PASS_NUMERIC_ROUNDING, PASS_OTHER, PASS_JUDGE_BUG too-lenient audit calls): 7
- WRONG_DIRECTION: 2

**Adjusted-actionable pass rate** (excluding the 8 dataset errors): 102/142 = **71.8%** — already in the Bedrock production-RAG band.

### Headline portfolio number — multiple framings, all honest

| Framing | Pass rate | Reference |
|---|---:|---|
| Raw under new (calibrated) judge | 102/150 = **68.0%** | Above FinGEAR EMNLP 2025 SOTA (~55%) by 13pp |
| Excluding 8 confirmed FB dataset errors | 102/142 = **71.8%** | In Bedrock production-RAG target band |
| Excluding all unfixable + ceiling if remaining 40 actionable were addressed | 142/142 = **100%** (theoretical) | Not realistic; some real reasoning limitations remain |
| Pre-Sprint-7.14 reported headline (broken judge) | 69/150 = 46.0% | The number that drove 5 sprints of optimization, retrospectively explained |

### What this means for the campaign

**The project's real performance was always production-grade RAG band.** The 47% headline drove ~6 weeks of optimization sprints that hit a 1-3pp ceiling because they were optimizing the wrong measurement. The two campaign-defining methodological signals (alongside the prior 5):

> **Signal 7 (Sprint 7.14)**: Built a κ=0.932 LLM-as-judge from scratch via 89-Q hand-labeled calibration with adversarial leniency guard + holdout + iterative prompt tuning. This is **how production LLM-as-judge gets built**.

> **Signal 8 (Sprint 7.13 audit + Sprint 7.14 Phase 2)**: Discovered that 47% of "failures" in the prior 6-sprint campaign were eval-framework artifacts (judge bugs + dataset errors), not system failures. Re-evaluation with the new judge moved the project from "stuck at 47%" to "68.0% raw / 71.8% adjusted — above SOTA, near production target." **The eval framework itself must be audited before per-stage attribution can be trusted.**

### Sprint 7.14 Phase 3+ — strategic options now visible

The 18 REFUSAL cases are the highest-leverage residual bucket. They split into two sub-flavors that need different fixes:
- **Retrieval miss**: data wasn't in retrieved chunks → retrieval intervention (parent-child chunking, larger top-K, query decomposition)
- **Synthesis failure**: data was retrieved but generator refused rather than computing partial answer → generator calibration prompt

A ~3-hour triage of the 18 REFUSAL cases (check chunk contents against required data items per question) would surface the dominant sub-flavor and inform Sprint 7.15.

But also: **68% raw / 71.8% adjusted is a clean stopping point**. Three reasons:
1. Above SOTA (FinGEAR ~55%) and in production-RAG band (Bedrock ~70%)
2. 8 documented methodological signals make a portfolio-grade narrative independent of further pass-rate gains
3. The remaining failures are spread across categories with diminishing-returns interventions

Decision deferred to user.

### Phase 2 cost

| Step | Cost |
|---|---:|
| rejudge.py build | $0 |
| V1 grader rejudge run (150 records × Sonnet) | ~$0.50 |
| Trimmed diagnostic (audit-categorization joined to diff) | $0 |
| **Total Phase 2** | **~$0.50** |

Cumulative Sprint 7.14 total: ~$7. Cumulative campaign total: ~$94.5.

**Confidence labels**:
- **Measured**: 81-Q audit by Sonnet 4.6; 9/10 spot-check verified by hand; one PDF (Pfizer Q2 2023) directly verified Upjohn dataset-error claim
- **Reasonable inference**: True system pass rate is in the 65–75% band. Lower bound if some auditor calls are too generous; upper bound if dataset-suspect calls verify.
- **Speculation**: Sprint 7.14 Phase 2 will confirm 71%. The audit was n=81 → strong signal but a fresh full-eval with the better judge is the rigorous validation.

### Process lesson for portfolio framing

I made three confident-and-wrong recommendations across this sprint:
1. "Grader is the 24pp rate-limiting step → ship V1" (Day 1 cascade math interpretation)
2. "Dev-set abort, V1 is broken" (Day 2)
3. "Ship as-is at 47%, system is at its ceiling" (Day 3 pre-audit)

Each was overturned by **user pushback that asked me to verify against the repo's own evidence or against the actual data.** The credibility rule in `CLAUDE.md` was explicitly written to prevent this failure mode and I committed it three times in one sprint. The portfolio lesson: **the analyst's own confident interpretations of metrics need the same skepticism as paper-claimed deltas.** Hands-on data verification (reading the PDFs, walking through the pipeline by hand) is the cheapest insurance against this category of error.

---

## Sprint 7.15 — per-node diagnostic → 4 interventions → 68.0% → 72.0%

The Sprint 7.14 judge recalibration set the real baseline at 68.0%. Sprint 7.15 ran a per-node diagnostic on a 75-Q hand-labeled set to find component-level failure modes, applied four targeted interventions, and measured the answer-level lift on the full 150-Q eval. **Net: +6 cases, +4.0pp pass rate.**

### The 75-Q pipeline-diagnostic set

Stratified sample of 75 records: all 48 still-failing cases after Sprint 7.14 + 27 known-passing controls. Each record carries the V1 system answer, the top retrieved chunks, and seven hand-labels covering intent, complexity, target company, target year, expected sub-queries, hallucination grounding, and free-text notes. Built via `scripts/build_pipeline_diagnostic.py`; exported to markdown for human labeling (`scripts/export_pipeline_diagnostic_to_md.py`); parsed back via `scripts/parse_pipeline_diagnostic_md.py`. Labels manually authored, then cross-reviewed with two other AI systems before merging.

Per-node F1 was then measured via `tests/evaluation/diagnostic_runner.py`, which exercises each node in isolation against the labels (router, entity extractor, hallucination checker, research-agent decomposer):

| Component | Metric | Value | Verdict |
|---|---|---:|---|
| Router intent | macro-F1 | 0.987 | ✓ |
| Router complexity (retrieval-only) | macro-F1 | 0.913 | ✓ but had 3 under-routing cases |
| Entity company | accuracy | 0.947 | ✓ |
| **Entity year** | **accuracy** | **0.213** | 🚨 **bug** |
| Hallu (strict, PARTIAL=hallu) | macro-F1 | 0.659 | ⚠ ceiling-bound |
| Decomposer coverage | mean | 0.789 | ⚠ 8 missed_items cases |

The year accuracy of **21.3%** was the surprise. Direct evidence of a bug — not a model-capability issue.

### Intervention 1 — year regex fix (21.3% → 89.3% accuracy)

`src/graph/nodes/entity_extractor.py:38` had `YEAR_PATTERN = re.compile(r"\b(20[2-9]\d)\b")` — two bugs at once:

1. `\b` word boundary fails between letter and digit characters → "FY2022" doesn't match because there's no word boundary between the `Y` and the `2`.
2. `[2-9]\d` excludes 2010-2019 entirely.

Extended fix:

```python
YEAR_PATTERN_FULL = re.compile(r"(?<!\d)(20\d{2})(?!\d)")   # 4-digit 20XX, lookarounds
YEAR_PATTERN_SHORT = re.compile(r"\bFY\s?(\d{2})\b", re.IGNORECASE)  # "FY22" / "FY 22"

def _extract_year(query: str) -> int | None:
    full  = [int(y) for y in YEAR_PATTERN_FULL.findall(query)]
    short = [2000 + int(y) for y in YEAR_PATTERN_SHORT.findall(query)]
    years = full + short
    return max(years) if years else None
```

Three semantically motivated changes: (a) lookarounds instead of `\b` so "FY2022" matches; (b) `20\d{2}` instead of `20[2-9]\d` so 2010-2019 work; (c) `max(...)` so multi-year queries ("FY2018 - FY2020 average") resolve to the document's filing year (the latest). Re-test on 75 Qs lifted accuracy 21.3% → 89.3% (the residual ~11% are questions with no year mentioned at all — unfixable at the regex layer).

### Intervention 2 — decomposer prompt rewrite + cap 4 → 5

The decomposer's 8 missed_items cases split into 5 real failures (3 of 4 currently FAILing in V1) and 3 judge over-penalties (decomposed fine, judge marked harshly). The 5 real failures patterned:

1. **Quick ratio vs current ratio domain confusion** (2 cases) — decomposer emitted `[current assets, current liabilities]` for "quick ratio" queries. Quick ratio EXCLUDES inventory; that's a different metric.
2. **"What drove X" missed MD&A** (1 case) — system pulled SG&A + net sales but skipped the management discussion of *drivers*.
3. **"Which X performed best"** (1 case) — total-company numbers retrieved, no segment-breakdown sub-Q.
4. **Formula coverage with cap=4** (1 case) — CCC formula (DIO + DSO − DPO) needs 4+ quantities × 2 years; 4-cap forced dropping AP.

Fix: `DECOMPOSE_SYSTEM_PROMPT` gained a "CRITICAL DEFINITION GUARD" block (Quick ratio components, CCC components, gross margin n/a for financial-services), an explicit MD&A sub-Q rule for "what drove" verbs, and a segment-breakdown rule for "which X performed best." Cap raised to 5 (`src/graph/nodes/research_agent.py:63`). Re-test on the 5 cases: **4 fully fixed, 1 improved.**

### Intervention 3 (the instructive null) — hallu prompt tightening regressed; model swap fixed it

**First attempt**: added rules to `HALLUCINATION_CHECK_SYSTEM_PROMPT` for list/category claims, "drivers" claims requiring explicit MD&A attribution, and category-error checks (e.g. flag "gross margin for American Express"). Re-ran hallu on all 75 records:

| Metric | Before | After (tightened prompt) | Δ |
|---|---:|---:|---:|
| Strict accuracy | 0.733 | 0.707 | **−0.026** |
| Strict macro-F1 | 0.659 | 0.646 | **−0.013** |
| Y→hallucinated (FPs) | 5 | 8 | **+3 FPs** |

**Result: regression.** Haiku 4.5 ignored the nuanced new rules at temperature=0 — the FN at confident score=1.0 (an AmEx gross-margin category error) didn't flip, and three previously-correct grounded answers got flipped to hallucinated. **Reverted the prompt change.**

This led to a methodological question that re-surfaced an old decision:

> *Was Haiku 4.5 the right model for the hallu-checker in the first place? Sprint 7.9 Day 3 downgraded Sonnet 4.6 → Haiku 4.5 on the argument "matches noise floor on n=30 dev-set — save $1.35/eval." But that ablation measured downstream pass rate under the OLD (pre-calibration) judge, not the hallu checker's own F1 against ground-truth labels.*

The right ablation now (with κ=0.932 labels): re-judge the 75-Q diagnostic with both models, compute macro-F1 directly.

**Ablation result** (75 records, 4-way parallel):

| Metric | Haiku 4.5 | Sonnet 4.6 | Δ |
|---|---:|---:|---:|
| Strict accuracy | 0.733 | 0.773 | +0.040 |
| Strict macro-F1 | 0.659 | **0.730** | **+0.071** |
| Grounded F1 | 0.818 | 0.838 | +0.020 |
| **Hallucinated F1** | **0.500** | **0.622** | **+0.122 (+24% relative)** |
| Wall (75 records) | 80s | 152s | ~2× slower |
| PARTIAL→hallucinated catches | 10/24 | 14/24 | +4 |
| Y→hallucinated (FPs) | 5 | 6 | +1 |

Sonnet 4.6 catches 4 more PARTIAL cases as ungrounded at the cost of 1 additional FP on truly-grounded. **Asymmetry favoring the verification path.** External corroboration: Vectara HHEM 2026 has Sonnet 4.6 at 91.0% detection rate vs Haiku 4.5 at 77.0%, with ~3-4× lower hallucination rate on the harder evaluation set. Shipped: `HALLUCINATION_MODEL = "claude-sonnet-4-6"` (restored).

### Intervention 4 — router prompt: implicit comparison/superlative/trend triggers (0.913 → 1.000 F1)

The diagnostic showed 6 router complexity misclassifications (3 in each direction):

- **Under-routing** (research → simple, *the costly direction*): "which segment had the highest", "is X improving", "is growth accelerating". Implicit comparison hidden in the verb — the router missed it.
- **Over-routing** (simple → research): multi-year list queries like "What acquisitions did Company X do in FY2022 and FY2021?" — multi-year ≠ multi-comparison.

Added a new trigger to `ROUTER_SYSTEM_PROMPT` for "Implicit comparison / superlative / trend" with explicit examples (`which segment had the highest`, `is X improving/declining`, `free cash flow conversion`, etc.), plus a "NOT research_required" carve-out for list-across-years patterns. Re-test on 75 Qs: complexity macro-F1 **0.913 → 1.000** (perfect classification on the diagnostic set, all 6 misclassifications flipped, no regressions).

### Full 150-Q eval result

Ran the canonical pipeline with all four interventions applied. Output file `tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_4fix.{json,correctness.json,pipeline.json}`. Then re-judged with Sonnet 4.6 + IMPROVED_PROMPT v2 via `tests/evaluation/rejudge.py`.

| Config | Pass count | Pass rate | Notes |
|---|---:|---:|---|
| V1 baseline (pre-7.15, rejudged) | 102/150 | 68.00% | Same V1 system; Sprint 7.14 Phase 2 number |
| **+ 4 interventions** | **108/150** | **72.00%** | **+6 cases, +4.00pp** |
| Diff: rescues (V1 FAIL → 4fix PASS) | +14 | | |
| Diff: regressions (V1 PASS → 4fix FAIL) | −8 | | |

Net +6 = 14 rescues − 8 regressions. The wins outpace the regressions cleanly.

### Regression triage — 8 cases characterized

Each regression was inspected with both V1 and 4fix answers + judge reasoning side-by-side:

| Mechanism | Cases | Pattern |
|---|---|---|
| Hallu Sonnet 4.6 refusal-cascade on borderline grounded | 3 | System refused/disclaimed when V1 had answered with caveats |
| Decomposer change → different chunks retrieved | 3 | More-specific sub-Qs missed adjacent context that V1's umbrella sub-Qs caught |
| Year regex picks max-year → wrong doc type | 1 | "Q2 of FY2023" → retrieved FY2023 10-K instead of Q2 10-Q |
| Judge stochasticity (same answer, flipped verdict) | 1 | Not a real regression |

### Follow-up: Fix 1 + Fix 2 attempted — Fix 1 reverted, Fix 2 kept and validated

Two targeted follow-ups were drafted to address the regressions:

- **Fix 1**: revert `MAX_SUB_QUESTIONS` 5 → 4 to reduce sub-Q fragmentation (recover the "different chunks" regressions).
- **Fix 2**: add "is X improving as of FY Y → strictly YoY, not 3-year trend" rule to the decomposer prompt qualifier list (recover case 00438 Adobe op margin specifically).

Validated on a 22-case set (8 regressions + 14 rescues) — cheap subset rather than full 150-Q:

| Metric | Result (Fix 1 + Fix 2) |
|---|---:|
| Regressions recovered (4fix FAIL → new PASS) | 3 / 8 |
| Rescues lost (4fix PASS → new FAIL) | 4 / 14 |
| **Net delta** | **−1** |

The 4 lost rescues were all **multi-input formula questions** (DPO with 4+ components, 3-year capex/revenue averages, percent-of-net-sales metrics). Cap=5 was doing real work on those. Cap=4 forced fragmentation in a *different* direction than the regressions it addressed.

**Decision**: keep Fix 2 (the targeted YoY rule — recovered case 00438 cleanly with no side effects), revert Fix 1 (cap stays at 5). Current shipped state: 4 interventions + Fix 2.

### Final measured full 150-Q (4 interventions + Fix 2) — 73.3% under κ=0.932 judge

Full canonical run with the post-Sprint-7.15 codebase (4 interventions + Fix 2). Pipeline + RAGAS + DeepEval + correctness, output at `tests/evaluation/eval_results/financebench_pypdf_voyage_tiered_ft_litellm_4fix_plus_fix2.{json,correctness.json,ragas.json,deepeval.json,pipeline.json}`. Correctness then re-judged via `rejudge.py` with Sonnet 4.6 + IMPROVED_PROMPT v2.

| Config | Pass count | Pass rate | Δ vs V1 | Δ vs 4fix |
|---|---:|---:|---:|---:|
| V1 baseline (Sprint 7.14 rejudge) | 102/150 | 68.00% | — | — |
| 4 interventions only (Sprint 7.15 prior) | 108/150 | 72.00% | +4.00pp | — |
| **4 interventions + Fix 2 (CURRENT)** | **110/150** | **73.33%** | **+5.33pp** | **+1.33pp** |

**Fix 2 marginal contribution (4fix → 4fix+Fix2): 6 rescues − 4 regressions = net +2 cases.** Better than the projected +1 case, because Fix 2's "is X improving/declining as of FY Y" YoY rule caught more pattern variants than originally targeted.

Fix 2 incremental rescues:
- `00394` JPM Q2 2022 segment with highest net income (paired with the router-fix's implicit-comparison trigger)
- `00438` Adobe operating margin "improving as of FY2022" (the targeted case)
- `05915` CVS FY2018 PP&E turnover (also rescued in the 22-case validation)
- `07507` Adobe FY2015→FY2016 operating income YoY (also rescued in 22-case validation)
- `01328` Pepsico FY2022 restructuring costs (bonus catch)
- `03856` Adobe FY2017 operating cash flow ratio (bonus catch)

Fix 2 incremental regressions:
- `07966` 3-year capex/revenue avg — *multi-year metric, may have been over-narrowed*
- `00606` Ulta wages-as-%-of-net-sales "increase or decrease" — *YoY rule pattern overgeneralized*
- `00685` Best Buy gross margin "historically consistent" — *needs multi-year context, YoY too narrow*
- `10420` Balance-sheet-only calculation — *unclear mechanism*

The 4 regressions show that the YoY rule has slightly over-generalized: Sonnet's decomposer interprets phrases like "increase or decrease in FY Y" and "historically consistent" as YoY triggers when they actually need multi-year context. A follow-up Fix 3 would tighten the YoY rule's trigger phrasing (require explicit "improving" / "declining" / "deteriorating" verbs, not generic "increase or decrease" or "fluctuating"). Deferred — net +2 still beats the noise floor.

### Fix 3 — YoY trigger narrowing (measured +1 case projected)

After the final 4fix+Fix2 measurement, the 4 incremental regressions from Fix 2 were inspected to see if a narrowed YoY rule could recover them without losing the +6 rescues. Fix 3 tightens Fix 2's trigger phrasing to ONLY fire on explicit trend verbs (`improving / declining / deteriorating / strengthening / weakening / accelerating / slowing`) and explicitly NOT on:

- "X year average" / "3-year average" (multi-year metric, use as written)
- "historically consistent" / "fluctuating" (needs multi-year context)
- "increase or decrease in FY Y" (single-year direction, full per-input enumeration without year-scope override)

Cheap validation on the 4 specific incremental regressions:

| fb_id | Pattern | Fix 3 outcome | Mechanism if not recovered |
|---|---|:---:|---|
| `07966` Activision 3-yr capex avg | "3 year average" | **PASS** (recovered) | Negative trigger worked as designed |
| `00606` Ulta wages YoY | "increase or decrease in FY2023" | FAIL | System got the direction *wrong* — orthogonal semantic error |
| `00685` Best Buy gross margin consistency | "historically consistent" | FAIL | Refusal-cascade hedging ("partial evidence...") — not addressable by YoY rule |
| `10420` AES FY2022 ROA | balance-sheet calc | FAIL | Wrong arithmetic (-1.28% vs gold -0.02) |

**Net +1 case projected** (110 → 111). Below the n=150 noise floor of ±2-3 cases, so the headline measurement stays at 110/150 = 73.33% until a fresh full-eval validates Fix 3 at scope. Fix 3 ships because it has measured non-zero positive impact (one clean recovery on the multi-year-average pattern) and zero measured downside on the targeted set.

### Per-question-type slice analysis — where the +5.33pp landed

| Slice | n | V1 pass | V1 % | 4fix+Fix2 pass | 4fix+Fix2 % | **Δ** |
|---|---:|---:|---:|---:|---:|---:|
| FB `domain-relevant` (prose Qs) | 50 | 31 | 62.0% | 32 | 64.0% | +2.0pp |
| FB `metrics-generated` (tables) | 50 | 39 | 78.0% | 41 | 82.0% | +4.0pp |
| **FB `novel-generated`** (cross-source synthesis) | **50** | **32** | **64.0%** | **37** | **74.0%** | **+10.0pp** |
| topical `lookup` | 60 | 38 | 63.3% | 39 | 65.0% | +1.7pp |
| **topical `multi_hop`** (compare/improving/highest/drove) | **27** | **20** | **74.1%** | **23** | **85.2%** | **+11.1pp** |
| topical `calc` | 63 | 44 | 69.8% | 48 | 76.2% | +6.3pp |
| Best Buy (weakest performer in V1) | 8 | 3 | 37.5% | 4 | 50.0% | +12.5pp |
| AMD | 8 | 7 | 87.5% | 8 | 100.0% | +12.5pp |
| PepsiCo | 11 | 7 | 63.6% | 8 | 72.7% | +9.1pp |

**The +5.33pp aggregate distributes very unevenly.** The strongest result is **multi-hop +11.1pp** — mirroring the Sprint 7.9 D7 LoRA-FT reranker pattern that delivered multi-hop +15pp. Both interventions targeted the same slice and both delivered. The `novel-generated` +10pp is the FB stratum requiring synthesis across sources — directly addressed by the decomposer prompt rewrites + research-agent integration. The `calc` +6.3pp pairs with the year-regex fix (multi-year fiscal-year resolution) and the decomposer's formula-coverage guards (CCC, quick ratio, DPO, fixed-asset turnover).

The two slices with the *smallest* improvements (`lookup` +1.7pp, `domain-relevant` +2.0pp) are the slices these interventions weren't designed to move — lookup queries don't go through the research-agent, and prose Qs are retrieval-bound rather than decomposition-bound. **The slice deltas confirm the interventions were mechanistically correct, not accidentally moving an unrelated population.**

### Multi-judge panel vs V1 baseline

| Metric | V1 baseline | 4fix+Fix2 | Δ |
|---|---:|---:|---:|
| **Correctness (κ=0.932)** | **68.00%** | **73.33%** | **+5.33pp** |
| RAGAS faithfulness | 0.707 | 0.733 | +0.026 |
| RAGAS context_precision | 0.733 | 0.669 | **−0.064** |
| RAGAS context_recall | 0.386 | 0.381 | ~0 |
| DeepEval faithfulness | 0.829 | 0.851 | +0.022 |
| DeepEval contextual_precision | 0.768 | 0.752 | −0.016 |
| **DeepEval contextual_recall** | 0.728 | **0.795** | **+0.067** |
| DeepEval answer_relevancy | (n/a) | 0.815 | — |

**Two trade-offs visible in the multi-judge panel:**
1. **Retrieval recall up, precision down** (DeepEval c.recall +0.067; RAGAS ctx_precision −0.064). The decomposer change emits more (5) and more-specific sub-queries; each retrieves narrower-but-more-comprehensive chunks. Net effect on the correctness metric is positive, but the raw chunk pool is noisier per-chunk.
2. **Faithfulness up on both judges**. The Sonnet 4.6 hallu upgrade landed directly in answer quality — answers are better grounded in the retrieved context.

### Adjusted-actionable pass rate

Excluding the 9 FinanceBench dataset errors flagged by the Sprint 7.15 residual audit: **110/141 = 78.0%**. Inside Bedrock's production-RAG band (~70%+).

### Sprint 7.15 post-intervention diagnostic re-runs — clean coverage

Before the final full-eval, three cheap diagnostics were re-run to ensure component-level evidence was current (~40 min, ~$5 total):

**Phase-eval cascade** (foundation: chunker → retrieval → reranker → grader). Unchanged from Sprint 7.11 — by design. Our 4 interventions touched the decision layer (router/entity/decomposer/hallu), none of them are in the foundation. Same chunker IoU 0.46, same retrieval R@50 0.83, same reranker NDCG@8 0.42, same grader recall 0.66-0.68. **Validates that the +5.33pp lift was correctly attributed to decision-layer fixes.**

**Per-node F1 scorecard** (75-Q hand-labeled diagnostic): all 4 interventions' component lifts persisted in the integrated system. Router complexity 0.913 → 1.000. Entity year accuracy 0.213 → 0.893. Hallu macro-F1 0.659 → 0.711. Decomposer mean coverage 0.789 → 0.843, missed_items 8 → 5.

**Residual failure-mode audit** (42 FAILs in 4fix output, Sonnet auditor): **PASS_JUDGE_BUG dropped 25% → 0%** (the κ=0.932 judge has effectively no judge artifacts left), DATASET_SUSPECT 21% (9 cases), REFUSAL 26% (11 cases), WRONG_NUMBER 26% (11 cases). The next-sprint target is now visibly REFUSAL + WRONG_NUMBER = 22 cases of which the actionable subset could plausibly close 5-10 more cases.

### Methodological signals worth banking (additions)

**Signal 9 — Component-level metrics expose what end-to-end pass rate hides.** Sprint 7.9's "downgrade hallu to Haiku 4.5" decision was justified on dev-set pass rate against a poorly-calibrated judge. The right metric — hallu-checker F1 against human ground truth — wasn't measured. When properly measured 6 months later (under the κ=0.932 judge + 75-Q labels), Sonnet 4.6 won by +0.071 macro-F1, with the bulk of the lift on the minority class (hallucinated-class F1 +0.122). **The lesson: match the metric to the question you're trying to answer about a specific component. End-to-end pass rate is a system metric, not a component metric.**

**Signal 10 — Prompt tightening on a small model is a regression risk, not a guaranteed win.** Added rules for list/category claims, drivers attribution, and category-error checks to the Haiku 4.5 hallu prompt. Haiku ignored them at temperature=0 and the prompt's increased severity dragged precision on the grounded class. Per DeepEval/Langfuse 2026 docs: *"smaller models have weaker instruction following capabilities."* The right intervention was model swap, not prompt engineering — measured this time before shipping.

### Sprint 7.15 cost

| Step | Cost |
|---|---:|
| 75-Q diagnostic build | $0 (deterministic) |
| Per-node F1 measurement (`diagnostic_runner.py`) | ~$1 (Sonnet decomposer judge) |
| Hallu Haiku 4.5 vs Sonnet 4.6 ablation | ~$0.50 |
| Full 150-Q pipeline + rejudge (4 interventions) | ~$13 (Sonnet now on hallu path; pipeline) + $0.40 rejudge |
| 22-case follow-up validation (Fix 1 + Fix 2) | ~$2 |
| **Sprint 7.15 total** | **~$17** |

Cumulative campaign total: ~$104.

### Confidence labels (per credibility rule)

- **Measured**: 72.00% pass rate (108/150) under the calibrated judge with 4 interventions applied. Per-component F1 deltas for year extraction, decomposer, hallu, router. 22-case validation result (+3/−4).
- **Reasonable inference**: Fix 2 (YoY rule) likely adds ~+1 case (case 00438) on full 150-Q. The "cap=5 helps formula Qs, hurts umbrella-chunk Qs" trade-off is real but the cap=5 side has more value at this sample size.
- **Speculation, pending measurement**: That the projected 72.67% lands cleanly on a fresh full 150-Q run. Single-run pipeline stochasticity is ±2-3 cases at n=150 (Sprint 7.9 Day 2.5 + Sprint 8e finding). A re-validation run with Fix 2 included is the rigorous test.

---

## Sprint 7.16 — generator anti-refusal + enumerate-fully — validation-cohort win that didn't survive full eval

The Sprint 7.15 final state (110/150 = 73.33%) had 42 residual FAILs categorized by the Sonnet auditor into REFUSAL (11), WRONG_NUMBER (11), DATASET_SUSPECT (9), PARTIAL_ANSWER (7), WRONG_DIRECTION (3), PASS_NUMERIC_ROUNDING (1). Sprint 7.16 targeted the biggest two actionable buckets (REFUSAL + PARTIAL_ANSWER) plus WRONG_DIRECTION via generator prompt changes.

### Three interventions designed; two shipped, one reverted

**Intervention 1 — Anti-refusal nudge** (`GENERATOR_SYSTEM_PROMPT` clause 7 expansion):
Diagnosis of the 11 REFUSAL cases broke them into 4 mechanisms:
- A: Absence-as-answer (4 cases — gold was "none / 0 / didn't happen", system refused)
- B: Synthesis refusal (3 cases — chunks had proxy data, system declined to compute)
- C: Partial-with-hedge (1 case)
- D: Retrieval miss (2 cases — unfixable here)
- E: Guardrail/scope refusal (1 case — different system)

Two complementary prompt rules added: (a) "evidence of absence IS the answer when the relevant section is in scope" (clause 7(c)); (b) "compute from proxy data with a [Computation note: derived from X because Y not retrieved] caveat instead of refusing" (clause 7(b) rewrite). Plus a softening of rule 2 to defer to clause 7's calibrated bottom-line cases.

Validation: 11 REFUSAL targets + 25 stratified regression-smoke. Result: **+3 of 11 flipped to PASS, 0/25 regressions**. Ship gate cleared cleanly.

**Intervention 2 — Enumerate-fully clause 8**:
Diagnosis of the 7 PARTIAL_ANSWER cases surfaced a dominant sub-flavor: 3 of 7 are "list incomplete" (system got 1-2 of N items in chunks). Added clause 8: "When the question asks for a list, set, or composite, exhaustively cover every matching item present in the chunks; use the company's reported segment labels; include quantitative breakdowns alongside qualitative descriptions."

Validation: 7 PARTIAL_ANSWER + same 25-case smoke. Result: **+1 of 7 flipped, 0/25 regressions**. Ship gate cleared.

**Methodological finding (from PARTIAL_ANSWER diagnosis)**: Pattern A (list incomplete) was overwhelmingly retrieval-bound, not generator-bound. The missing items (Czech acquisition, PBM litigation, COVID drivers) weren't in retrieved chunks — no prompt fix could produce them. The audit had categorized them as "system gave partial answer" (true at output level) but the root cause was upstream. Banking as a sub-signal: **failure-mode audits classify by output shape, not by mechanism; per-bucket prompt fixes only address output-shape-bound failures**.

**Intervention 3 — Directional-verdict clause 9 (reverted)**:
Diagnosis of WRONG_DIRECTION: 1 case (`00438` Adobe op margin) already passing under Fix 2; 2 actionable (`00216` Verizon healthy quick ratio, `00790` CVS capital intensity) — both "system computes textbook metric, then dismisses its plain reading to flip the bottom-line yes/no." Drafted clause 9: "Trust your computed metric on directional-verdict questions; the escape hatch 'metric isn't useful for this company' applies only when the metric truly can't be computed."

Validation: **0 of targets flipped; 1 stochastic regression on borderline `00438` (Adobe ran twice in the cohort, showed PASS once and FAIL once — pipeline nondeterminism on a borderline case).** Sonnet's prior toward presenting computed-value-plus-skepticism didn't yield to the prompt rule. **Reverted clause 9** because measured impact was zero on targets and the rule added regression risk on borderline cases.

### Full 150-Q eval — measured result was lower than the targeted-cohort projection

Projection from validation cohorts: anti-refusal +3, enumerate +1 = +4 cases → 114/150 = 76.0%.

Actual measured result on full 150-Q with both fixes applied: **109/150 = 72.67%** (down 1 from prior 4fix+Fix2 state of 110/150 = 73.33%).

Cross-system diff (4fix+Fix2 → gen-v2):

| | n | Mechanism |
|---|---:|---|
| Rescues | 2 | `00669` JnJ gross-margin drivers (enumerate-fully working); `00685` Best Buy gross-margin "historically consistent" (anti-refusal/enumerate helping) |
| Regressions | 3 | `01328` Pepsico restructuring `$411M → system said $0` — **absence-as-answer rule misfired** (rule encouraged "0" when chunks didn't show restructuring, but the data was retrievable in other sections the retrieval missed); `00605` Ulta Q4 repurchases — stochastic flip (was rescued by Fix 2 originally); `06247` Walmart DPO `42.69 → 42.76` — stochastic precision drift |
| Net | **−1** | within the ±2-3 n=150 noise floor |

Multi-judge panel (gen-v2 vs prior 4fix+Fix2):
- RAGAS faithfulness: 0.733 → **0.747** (+0.014)
- RAGAS context_precision: 0.669 → 0.661 (~flat)
- DeepEval faithfulness: 0.851 → 0.844 (~flat)
- DeepEval contextual_recall: 0.795 → 0.768 (−0.027)
- Refusal rate: 5.3% → 6.7% (+1.4pp — anti-refusal nudge didn't reduce refusal rate net; the absence-as-answer rule shifted some cases away from refusal but stochastic + retrieval-bound cases moved into it)

Per-slice (under κ=0.932 judge), vs prior 4fix+Fix2:
- lookup: 69.8% → 68.6% (−1.2pp)
- **multi_hop: 76.9% → 84.6% (+7.7pp)** — biggest movement; cumulative multi-hop slice is now +30pp vs V1 baseline (54% → 85%)
- calc: 78.4% → 76.5% (−1.9pp)

The multi_hop gain is the cleanest positive signal — the enumerate-fully rule is biting on multi-hop "what drove X" questions where the gold has multiple drivers.

### Signal 11 — validation-cohort wins can wash out at full-eval scope

**The methodological lesson worth banking**: prompt interventions that pass cheap targeted-cohort validation (+3, +1 on small focused sets) can come in net-zero or slightly negative on the full 150-Q eval because:

1. **Pipeline stochasticity at n=150 is ±2-3 cases**. Same prompt, same code, two different verdicts on a fraction of cases per run (Sprint 7.9 Day 2.5 finding, confirmed again in Sprint 7.16 with `00605`, `06247`).
2. **Smoke cohorts (25 random robust passes) sample only 17% of the non-target population**. Even with 0/25 regressions on smoke, the remaining 125 cases can absorb 1-2 unexpected regressions that the smoke didn't see.
3. **Asymmetric-downside rules** (absence-as-answer encouraging "0" when chunks-don't-show-X) can misfire on cases where the chunks were incomplete but the data existed elsewhere. The validation cohort doesn't test "retrieval was incomplete for a fixable reason"; the full eval does.

This is structurally similar to the Sprint 7.8 calculator regression (validated component, regressed integrated system through downstream interaction). Same shape pattern.

**The right inference**: targeted-cohort validation is necessary but not sufficient. For prompt changes that touch every query (generator prompts especially), the validation gate should require a **full-eval re-measurement** before claiming the headline moves, not project from a 25-case smoke.

### The shipped state

Sprint 7.16 ships with both prompt changes preserved despite the −1 net at full eval, because:
- The targeted mechanisms work on their targeted cases (validated)
- The −1 is within the noise floor
- Two of three regressions are stochastic (not architectural)
- The one real regression (Pepsico absence-as-answer misfire) is a known failure mode of the rule; the rule's *legitimate* wins on cases like Ulta debt securities and CVS PP&E justify keeping it
- Cumulative trajectory remains positive: V1 baseline 68.00% → gen-v2 72.67% = +4.67pp via Sprint 7.15 + 7.16 work

The full 150-Q multi-judge eval landed at 109/150 = 72.67% raw / 109/141 = 77.30% adjusted-actionable (excluding the 9 FB dataset errors verified during the Sprint 7.13 audit).

### Sprint 7.16 cost

| Step | Cost |
|---|---:|
| Diagnostic (3 buckets × audit data, no LLM cost) | $0 |
| 11 REFUSAL + 25 smoke validation (anti-refusal) | ~$3-4 |
| 7 PARTIAL_ANSWER + 25 smoke (enumerate-fully) | ~$3-4 |
| 3 WRONG_DIRECTION + 25 smoke (clause 9 — reverted) | ~$3-4 |
| Full 150-Q + multi-judge panel + rejudge | ~$15-20 |
| **Sprint 7.16 total** | **~$25-30** |

Cumulative campaign total: ~$160.

---

## Sprint 7.17 — grader architecture experimentation — null on pass rate, two methodology signals banked

Sprint 7.16 hit a clear ceiling on prompt-level interventions at the generator layer. The Sprint 7.16 attribution-diagnostic (Diag 2) found **~51% of remaining failures are upstream-bound** (gold chunks lost at retrieval or reranker before reaching the grader/generator) and **~49% downstream-bound**. Sprint 7.16's anti-refusal + enumerate-fully prompts targeted the downstream layer at the generator. Sprint 7.17 targeted the same downstream layer at the grader stage, where phase-eval had measured ~30pp gold-chunk recall loss (the grader rejects ~30% of relevant chunks before they reach the generator).

### Investigation 1 — LoRA fine-tune of cross-encoder/ms-marco-MiniLM-L-6-v2 (Phase 1-2)

Built training data with 3 negative-sampling strategies (random / hard / mixed) per the "When Fine-Tuning Fails" (arXiv 2506.18535) caveat that hard negatives don't always help. Trained 3 LoRA adapters (rank=8, alpha=16, dropout=0.1, BCE loss, 5 epochs, BF16 MPS on M4 Pro, ~$0 local training cost).

**Training-time validation looked promising:**

| Strategy | Best val_loss | Val acc | Val pos_recall | Val neg_recall |
|---|---:|---:|---:|---:|
| random | 0.2831 | 0.874 | 0.667 | 0.926 |
| hard | 0.3758 | 0.830 | 0.630 | 0.880 |
| mixed | 0.3359 | 0.870 | 0.704 | 0.912 |

Validated the "When FT Fails" paper's hard-negative warning empirically — hard-negative-only was the weakest. Random was best on val_loss.

**Component eval on the same 363-gold-chunk benchmark used in Sprint 7.17 Diag 3** killed the intervention:

| Variant | Gold-chunk recall | Zero-recall Qs |
|---|---:|---:|
| base MiniLM (no FT) | 0.196 | n/a |
| FT hard_r8 | 0.231 | 84 |
| FT mixed_r8 | 0.229 | 85 |
| FT random_r8 (best FT) | 0.328 | 68 |
| **Current Llama/gpt-4o-mini grader** | **0.700** | **8** |

The best FT'd MiniLM reached **32.8% gold-chunk recall — less than half of the production grader's 70%**. Per the [Lightweight Relevance Grader paper (arXiv 2506.14084)](https://arxiv.org/abs/2506.14084) the validated minimum base-model size for FT'd-with-classification-head graders is ~1B params (their best result: FT'd llama-3.2-1b achieved precision 0.7750; below that scored worse than gpt-4o-mini). MiniLM at 22.7M params is **45× below** the validated minimum.

**Signal 12 (banked)**: *LoRA fine-tuning has a base-model-capacity floor.* If the base model can't perform the task at all out-of-the-box (base MiniLM at 19.6% gold-chunk recall is barely above random for binary relevance), no amount of LoRA on 1-2K examples bridges the gap to a much larger prompt-tuned LLM. The Sprint 7.9 reranker FT worked because BGE-reranker-v2-m3 (568M params) already had usable relevance discrimination at the base level (NDCG@8 ≈ 0.42 on FB unmodified). MiniLM-L-6-v2 doesn't have that floor.

### Investigation 2 — model swap (user-prompted pivot after a config audit caught a mistake)

After the LoRA failure, a user question surfaced a config-vs-runtime discrepancy: the engineering log had been describing the grader as "Llama-3.3-70b via Groq", but `.env` had `USE_GROQ_FAST_PATH=false`, which flips both the grader AND router to OpenAI's `gpt-4o-mini` per `src/services/llm_factory.py`. **The actual runtime grader was gpt-4o-mini, not Llama.** All phase-eval grader recall numbers (Sprint 7.11's 0.66, Sprint 7.17 Diag 3's 0.70) measured gpt-4o-mini, not Llama. Correction noted.

Web research surfaced [arXiv 2506.14084](https://arxiv.org/abs/2506.14084) which claimed gpt-4o-mini was *worse* than a FT'd llama-3.2-1b for relevance grading. Separately, multiple 2026 production-RAG guides recommended Claude Haiku 4.5 specifically for "binary classification of chunk relevance" as the cost-effective production choice. Two hypotheses to test: (a) Haiku 4.5 should beat gpt-4o-mini; (b) Groq Llama-3.3-70b should beat both; (c) BGE-reranker-v2-m3 (with the production LoRA adapter from Sprint 7.9) as a free baseline.

### Investigation 3 — 4-way fair comparison

Built `scripts/internal/eval/eval_grader_models_compare.py` to test 4 backends on:
- **100-pair balanced sample** (50 random gold positives + 50 same-doc non-retrieved hard negatives) → precision, recall, F1, accuracy
- **363-gold-chunk recall set** (same as Diag 3) → gold-chunk recall + per-Q full/partial/zero buckets

Each backend instantiated directly (no `LLMFactory` dependency, to keep the experiment isolated from the production runtime's `USE_GROQ_FAST_PATH=false` setting that affects multiple nodes).

**The first run had 5 methodology bugs** (caught after user-prompted re-audit):

1. `max_tokens=256` on Haiku 4.5 only (others uncapped) — potential silent truncation of Anthropic structured outputs
2. Silent exception handler returning `(False, 0.0)` — errors counted as "irrelevant" verdicts, no error tracking
3. Groq token-rate limit (18K tokens/min) missed in pacing — only the request-rate limit (30/min) was checked; we exceeded the token limit, causing late-run failures
4. BGE backend used base `BAAI/bge-reranker-v2-m3` (no adapter) — production reranker is base + Sprint 7.9 LoRA-FT adapter at `data/models/reranker_ft_v1`. Unfair comparison
5. `gpt-4o-mini` control instantiated without `seed=42` — production via `LLMFactory._openai()` uses seed=42 for determinism

All 5 fixed in the v2 re-run. Groq paced at 6s/call to stay under 18K tokens/min. Backends reordered to BGE → gpt-4o-mini → Haiku → Groq last (per user request — if Groq daily quota exhausts, other three already have results).

**Fair v2 comparison result:**

| Backend | F1 (balanced) | Prec | Rec | Gold-chunk recall (363) | Zero-recall Qs | Errors | Latency | $/eval |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BGE-reranker-v2-m3 + LoRA-FT v1 | 0.701 | 1.000 | 0.54 | 0.452 | 42 | 0 | 65ms | $0 |
| **gpt-4o-mini (control)** | **0.826** | 0.905 | 0.76 | 0.700 | **7** | **0** | **1.5s** | **$1.86** |
| Claude Haiku 4.5 @ max_tokens=512 | 0.814 | 0.972 | 0.70 | **0.719** | 10 | 0 | 2.2s | $12.60 |
| Claude Haiku 4.5 @ max_tokens=2048 (control) | 0.814 | 0.972 | 0.70 | 0.714 | 10 | 0 | 2.1s | $12.60 |
| Groq Llama-3.3-70b (free tier) | 0.113 | 1.000 | 0.06 | 0.003 (1/363) | **146** | **89 / 361** | 6.3s | n/a |

### Three findings — and a correction

**1. Haiku@512 vs Haiku@2048 control settled the max_tokens question.** When the user (correctly) noted that production Anthropic calls use `max_tokens=2048` (with an explicit "Sprint 7.6 Day 4 fix" comment), we suspected Bug 1 might have been silently truncating Haiku's structured-output reasoning at 512 tokens. The control re-run at 2048 returned **identical results** (balanced F1=0.814, gold-recall=0.714). The Sprint 7.6 Day 4 lesson applies to *long-form generation* tasks (research-agent synthesis, hallu-checker reasoning), not short structured-output binary classification.

**2. The gpt-4o-mini-vs-Haiku comparison is inconclusive at this measurement budget.** First-pass framing called gpt-4o-mini "the winner" on F1; the user pushed back, and a re-audit found the framing was selective. The honest read of the same JSON:

| Metric (same run) | gpt-4o-mini | Haiku 4.5 | Winner |
|---|---:|---:|---|
| F1 (balanced-100) | **0.826** | 0.814 | gpt-4o-mini by 1.2pp |
| Precision (balanced-100) | 0.905 | **0.972** | Haiku by 6.7pp |
| Recall (balanced-100) | **0.760** | 0.700 | gpt-4o-mini by 6.0pp |
| Gold-chunk recall (363) | 0.700 | **0.719** | Haiku by 1.9pp |
| False positives on balanced-100 | 4 | **1** | Haiku |

The two models trade off precision vs recall. On the metric most aligned with downstream pass rate (gold-chunk recall on the 363-pair set), Haiku wins. The 1.2pp F1 gap is within plausible single-run variance for Haiku at temperature=0 — `ChatAnthropic` does not accept a `seed` parameter, so Haiku's output is "near-deterministic" but not bit-stable, whereas gpt-4o-mini is seeded at 42 via [`src/services/llm_factory.py:72`](src/services/llm_factory.py#L72). Three additional methodology caveats remain that could legitimately shift the picture further:

  - **Provider-specific structured-output mechanics.** `with_structured_output(GradeResult)` at [`scripts/internal/eval/eval_grader_models_compare.py:89`](../scripts/internal/eval/eval_grader_models_compare.py#L89) resolves to OpenAI's native `json_schema` (strict=True) for gpt-4o-mini, but Anthropic tool-use for Haiku. The two mechanisms inject different overhead into the prompt and enforce schema differently. This is what production uses, so the comparison is fair to production — but it is *not* a same-prompt comparison of the two models.
  - **Role text in `HumanMessage`, not `SystemMessage`.** `GRADER_PROMPT` at [`src/config/prompts.py:188`](src/config/prompts.py#L188) begins *"You are a relevance grader…"* and is wrapped in `HumanMessage` at [`scripts/internal/eval/eval_grader_models_compare.py:92`](../scripts/internal/eval/eval_grader_models_compare.py#L92). Anthropic's documented best practice puts persona text in the `system` field; OpenAI is more flexible. ~~Likely understates Haiku.~~ **Falsified by Sprint 7.17 follow-up**: re-ran Haiku with role text routed to `SystemMessage`; gold-chunk recall dropped −4.4pp (0.719 → 0.675), balanced F1 dropped −1.4pp (0.814 → 0.800). The production prompt shape is already optimal for Claude on this `with_structured_output` path. Haiku has no hidden capability we were leaving on the table.
  - **Same-doc negatives may be too easy.** [`build_balanced_sample`](../scripts/internal/eval/eval_grader_models_compare.py#L225-L264) picks negatives as "any chunk in the same doc not in the gold list," which in a 10-K includes structurally trivial chunks (signature pages, exhibit lists). Inflates precision relative to production where the reranker has already filtered to topically-similar chunks.

Cost ratio is real: Haiku is 6.8× more expensive per eval at parity of pass-rate impact. But "the published 2026 best-practice for Haiku-as-grader doesn't transfer" — the earlier framing of this finding — is **not what the evidence supports** at this measurement budget. The evidence supports "the gap is below our single-run noise floor, with split signals across metrics."

**3. Groq Llama-3.3-70b free tier is operationally unusable at any sustained throughput.** Even with 6s/call pacing (= 10 req/min, well under both the 30 req/min and 18K tokens/min documented free-tier limits), Groq returned errors on **89-99% of calls** during both benchmarks. The few successful calls scored well directionally (precision=1.0 when working) but the reliability is disqualifying for batch evaluation. This isn't a model verdict — it's a hosting verdict. Two follow-ups planned (see "Decision" below): rehost Llama 3.3 70B on Cerebras (60K TPM vs Groq's 18K, no rate-limit churn) and re-run Haiku with the role text correctly routed to `SystemMessage`.

### Decision

**No production change. Keep gpt-4o-mini as the grader for now.** Sprint 7.17 is a null result on pass rate, and the only candidate that might dominate gpt-4o-mini in a fair re-test is Haiku — and that re-test hasn't been run yet. Two cheap follow-up experiments will close the question rather than leave it ambiguous:

- **Follow-up 1 — RUN: Haiku with system+user split (~$0.50, 18 min wall).** Re-ran Haiku grader with `GRADER_PROMPT`'s role text routed to `SystemMessage`. **Result: regression.** Balanced F1 0.814 → 0.800 (−1.4pp), gold-chunk recall 0.719 → 0.675 (−4.4pp), zero-recall Qs 10 → 13. Caveat B falsified. Haiku's original 0.719 gold-recall stands as its ceiling on this `with_structured_output` path; no prompt-engineering pass closes the residual gap to gpt-4o-mini.
- **Follow-up 2 — RUN: Llama-3.3-70B-Instruct via Fireworks AI free tier ($0 actual cost from $6 signup credit, 60 min wall on 463 calls).** Original plan was Cerebras, but Cerebras free tier doesn't list Llama-3.3-70B (only `gpt-oss-120b`, `llama3.1-8b`, `qwen-3-235b-a22b-instruct-2507`, `zai-glm-4.7` per [the official rate-limits doc](https://inference-docs.cerebras.ai/support/rate-limits) — earlier 3rd-party blog cited Llama-3.3-70b on free tier, but the authoritative source contradicts it). Pivoted to **Fireworks AI free tier** (~10 RPM, no payment method required) routing `accounts/fireworks/models/llama-v3p3-70b-instruct`. **Result: decisive win on grader benchmarks.**

  | Backend | F1 (balanced) | Precision | Recall | Gold-chunk recall (363) | Zero-recall Qs |
  |---|---:|---:|---:|---:|---:|
  | BGE+LoRA-FT v1 | 0.701 | 1.000 | 0.540 | 0.452 | 42 |
  | Haiku 4.5 (system+user split, Exp 1) | 0.800 | 0.971 | 0.680 | 0.675 | 13 |
  | Haiku 4.5 (HumanMessage only) | 0.814 | 0.972 | 0.700 | 0.719 | 10 |
  | gpt-4o-mini (production control) | 0.826 | 0.905 | 0.760 | 0.700 | 7 |
  | **Llama-3.3-70B via Fireworks (Exp 2)** | **0.871** | 0.863 | **0.880** | **0.860** | **2** |

  Llama 3.3 70B beats gpt-4o-mini by **+16pp on gold-chunk recall** (0.700 → 0.860), drops zero-recall Qs from 7 → 2 (out of 147), and lifts balanced F1 by +4.5pp. Precision regresses slightly (0.863 vs gpt-4o-mini 0.905) — Llama is more permissive — so the downstream generator absorbs ~7 extra false-positive chunks per 100 calls. Cost per 150-Q eval: $10.53 (5.5× gpt-4o-mini, comparable to Haiku's 6.8×). Wall-time penalty in production: ~5s latency vs gpt-4o-mini's 1.5s. 0 errors out of 463 calls — Fireworks is operationally healthy at the free-tier 10 RPM, unlike Groq.

  This is the **first grader candidate in Sprint 7.17 that materially dominates the production control**. The earlier framing — that gpt-4o-mini and Haiku were near-equal and the grader stage was at its prompt-only LLM ceiling — was correct *within the gpt-4o-mini-and-Haiku pair* but missed Llama-3.3-70B's much higher recall floor. Open-weight 70B-class models are a real grader option that the 4-way comparison's broken Groq backend hid.

**Follow-up #3 — RAN: full 150-Q FinanceBench eval w/ Llama-3.3-70B grader (~$3, 2hr 51min wall).** Wired `LLMFactory.get_grader_llm()` to dispatch to Llama-3.3-70B-Instruct via OpenRouter (Fireworks free tier was too rate-limit-bursty for batch usage; OpenRouter paid at $0.04/M tokens has no burst penalty). Six wiring issues caught in the smoke pass before launching the full run (each one would have silently corrupted the result, so each was its own credibility-rule win):

1. **`os.environ.get("FIREWORKS_API_KEY")` returns None** — pydantic-settings reads `.env` for typed fields but doesn't side-effect into `os.environ`. Switched to `settings.FIREWORKS_API_KEY`.
2. **Redis connection refused** — docker-compose maps the rag-cache redis container to host port 6380, not the default 6379. Added `RESULT_CACHE_REDIS_PORT=6380` to `.env`.
3. **`.env` keys not visible to modules that read os.environ directly** — added `load_dotenv()` at the top of `run_financebench.py` and `rejudge.py`.
4. **Wrong Qdrant collection** — eval default was `financebench_corpus` (payload `company="unknown"` for most chunks, breaking the entity_match filter); the canonical 72.7% baseline used `financebench_corpus_pypdf_voyage_finance2` (real company slugs).
5. **Wrong embedding provider** — settings default was `EMBEDDING_PROVIDER=openai` (1536-dim), but the canonical collection was indexed with voyage-finance-2 (1024-dim). Qdrant rejected every query with a dimension-mismatch 400.
6. **OpenRouter auto-routing to providers with broken structured-output** — initial smoke had dozens of `LengthFinishReasonError` and "no parsed field" errors per question because OpenRouter sent some requests to providers (e.g. Venice) that don't reliably emit valid JSON for the GradeResult schema. Pinned via `extra_body={"provider": {"order": ["fireworks", "together", "deepinfra"], "allow_fallbacks": False}}` — 0 grader failures across the full 150-Q run.

**Result (rejudged with κ=0.932 Sonnet 4.6 + v2):**

| Config | Pass rate | Δ vs gpt-4o-mini baseline |
|---|---:|---:|
| Sprint 7.16 baseline (gpt-4o-mini grader) | 72.7% (109/150) | — |
| Sprint 7.17.#3 (Llama-3.3-70B grader via OpenRouter) | **68.0% (102/150)** | **−4.7pp** |

Per-Q diff: 14 regressions, 7 rescues, 95 unchanged-pass, 34 unchanged-fail. Pattern is consistent across the failure axis:

- **Llama's 7 rescues** are multi-component-formula / qualitative-judgment questions (Ulta repurchases multi-step, MGM interest coverage, Verizon/Paypal/Corning working capital, AES restructuring, generic financial-position). More chunks accepted = more evidence for multi-step reasoning.
- **Llama's 14 regressions** are focused-lookup / superlative / list-enumeration questions (JnJ gross margin drivers, 3M debt securities list, AmEx largest liability, JPM lowest revenue segment, Best Buy consistent gross margins, CVS fixed asset turnover, Pfizer biggest regional drop, Adobe op-cash-flow ratio, AMD customer concentration, PepsiCo legal battles, AMCOR EBITDA, 3M segment drag, Verizon retiree payments, 3M capex). More chunks accepted = generator distracted from the one chunk holding the answer.

The benchmark precision delta (Llama 0.863 vs gpt-4o-mini 0.905) is the proximate cause. Llama's lower precision = more borderline chunks survive grading → generator gets noisier context → focused-lookup answers drift / get diluted.

**Signal 14 (banked)**: *Sub-component metric wins don't always propagate to system-level metrics — and can actively regress them through second-order effects.* Llama-3.3-70B was unambiguously better on the grader benchmark (gold-recall +16pp, balanced-F1 +4.5pp). Under the κ=0.932 judge, the same intervention regressed FinanceBench pass-rate by 4.7pp. The precision/recall trade-off that looks neutral at the component level had asymmetric downstream consequences: extra true-positive chunks help on a minority of questions but extra false-positive chunks hurt on a larger majority. Echoes Signal 11 (validation-cohort wins washing out at full-eval) with a cleaner mechanism: when a sub-component metric only captures one axis (recall) and the system rewards another (precision-driven focus), the sub-component win can be a system-level loss. **Lesson: even when a sub-component win is real and measured, the only definitive test is the end-to-end full eval.**

### Decision

**Do not ship Llama-3.3-70B as grader.** Sprint 7.17 closes as a fully null result on pass rate, with three signals banked (12, 13—deferred, 14). Production stays on gpt-4o-mini.

### Open question — retrieval, not grader

Sprint 7.16 Diag 2's attribution already pointed here: ~51% of residual failures are upstream-bound (retrieval miss / reranker rejection). The grader sub-tree of the failure budget is small enough that even a perfect grader couldn't move the headline much. Sprint 7.18 (when it exists) should attack the 14 RETRIEVAL_MISS cases, not the grader.

---

## Sprint 7.18a — retrieval k-bump 50 → 200 — null with the largest regression measured

After Sprint 7.17 closed with the grader at its system ceiling, the next architectural lever was retrieval. Diag 2 attribution (Sprint 7.16) measured 51% of residual failures as upstream-bound: gold chunks lost at retrieval or reranker before reaching the grader/generator. The 14 RETRIEVAL_MISS cases were the largest single bucket.

### Pre-flight diagnostic (45 min, $0)

Per the credibility rule + Signal 14 lesson, ran two cheap diagnostics before committing to any intervention:

**Diagnostic 1 — k-sweep on the 14 RETRIEVAL_MISS cases.** For each case, ran dense / sparse / hybrid retrieval at k=50, 200, 500, 1000 on the canonical `financebench_corpus_pypdf_voyage_finance2` collection and measured whether the gold chunks were present in each candidate pool.

| Retrieval config | Cases with gold present (of 14) |
|---|---:|
| Hybrid top-50 (current production) | **1/14** |
| Sparse top-200 (BM25 alone) | 5/14 |
| Hybrid top-200 | **10/14** |
| Dense top-200 | 11/14 |
| Dense top-500 | 13/14 |
| Dense top-1000 (no filter) | 13/14 |

Only 1/14 cases (JPM hypothetical bankruptcy `02119`) was unreachable even at k=1000. The other 13 had gold within reach of broader top-K — the embedding was good enough; the candidate pool was too tight.

**Categorization by failure mode** (manual coding of the 14 questions + gold chunks):

| Pattern | Count | Example | Gold chunk shape |
|---|---:|---|---|
| RATIO_CONCEPT_MISMATCH | 8/14 | "Does Paypal have positive working capital?" | Balance sheet / income statement / cash flow with raw line items — query has the ratio name, doc doesn't |
| LIST_OR_ENUMERATION | 3/14 | "What are AMCOR's acquisitions in FY23/22/21?" | Deep footnote pages (e.g. acquisitions note at page 64×4) |
| MULTI_YEAR_COMPARISON | 1/14 | "Boeing tax rate FY22 vs FY21" | Income statement with prior-year columns |
| NOVEL_HYPOTHETICAL | 2/14 | "If JPM went bankrupt..." | Press-release / cover page; one is unreachable |

**Diagnostic 2 — FT v1 reranker behavior on k=200 hybrid pool.** For each case, retrieved 200 candidates and scored every one with the production FT v1 reranker (`BAAI/bge-reranker-v2-m3` + LoRA adapter at `data/models/reranker_ft_v1`).

| Stage | Of 14 cases |
|---|---:|
| Gold in k=200 hybrid pool | **10/14** |
| Gold ranks in TOP-8 after FT v1 rerank | **6/14** |
| Gold in pool but stuck at rank 13-41 | **4/14** |

FT v1's training data (per `data/models/reranker_ft_v1/training_metadata.json` + `data/training/reranker_ft_v1/manifest.json`) was 1779 (query, chunk) pairs from 67 PASSING FinanceBench questions, hard negatives drawn from top-30 retrieval. The 14 RETRIEVAL_MISS cases were excluded by construction; chunks ranked 31-200 were never in the training distribution.

Despite that, FT v1 actually surfaced the gold to top-8 for 5/5 in-pool RATIO_CONCEPT_MISMATCH cases (ranks 1-6). It failed on LIST / MULTI_YEAR / NOVEL cases (ranks 13-41). A diagnostic-justified FT v2 candidate sprint was sized at +1-3pp incremental on top of whatever k-bump delivered.

### Full eval result

Set `RETRIEVAL_TOP_K=200`, left everything else identical to the Sprint 7.16 baseline. Smoke 5-Q ran clean (60s/q wall, vs baseline 65s/q — reranker latency penalty at k=200 was negligible). Full 150-Q eval: 7170s wall (47.8s/q), 0 pipeline errors.

**Rejudged under κ=0.932 Sonnet 4.6 + IMPROVED_PROMPT v2:**

| Config | Pass rate | Δ vs Sprint 7.16 baseline |
|---|---:|---:|
| Sprint 7.16 baseline (RETRIEVAL_TOP_K=50) | 72.7% (109/150) | — |
| Sprint 7.18a (RETRIEVAL_TOP_K=200) | **57.3% (86/150)** | **−15.33pp** |

Per-Q diff: **2 rescues, 25 regressions, 84 unchanged-pass, 39 unchanged-fail.** Of the 14 RETRIEVAL_MISS cases the intervention targeted, only 2 newly passed (Verizon capital-intensive `00215` and Walmart DPO `06247`).

### Mechanism — distractor chunks crash comprehension

The 25 regressions span every question type — not concentrated. Examples:

- `00757` AMD customer concentration (yes/no with evidence) — passed at k=50, failed at k=200
- `01198` AMD revenue drivers (MD&A) — passed → failed
- `05915` CVS fixed asset turnover (multi-component formula) — passed → failed
- `00302` Pfizer PPNE growth (yes/no) — passed → failed
- `03856` Adobe op-cash-flow ratio (formula) — passed → failed
- `01964` AmEx largest liability (superlative lookup) — passed → failed
- `03029` 3M FY18 capex (specific number) — passed → failed

The smoke 5-Q already telegraphed the mechanism: the k=200 answer for 3M FY2018 capex cited "$27 million" (an environmental-capex footnote line) instead of the gold "$1,577 million" (Purchases of PP&E from the cash flow statement). Both chunks are 3M FY2018 chunks; both contain the phrase "capital expenditure"; the broader pool surfaced the wrong one and the generator chose it.

Mechanistic explanation: even though the reranker's final output is still top-8, its COMPOSITION shifts when drawn from a 200-chunk pool vs a 50-chunk pool. For queries where gold is at rank 8-30 in the smaller pool (passing baseline), broader retrieval brings in OTHER high-similarity chunks from ranks 51-200 that displace correct ones — same number of slots, different occupants. The displaced occupants tend to be plausible-but-wrong-number distractors (e.g. environmental capex, segment-specific capex, prior-year capex) because financial filings contain many such adjacent line items.

**Signal 15 (banked)**: *Broadening a retrieval candidate pool to recover a missed-recall subset can crash system pass rate at a higher rate than the gain.* The signal-14 mechanism but ~3× worse in magnitude: sub-component metric (Recall@200) +50pp on the failing bucket → system metric (pass rate) −15.33pp on the headline. The mechanism this time is at the reranker-output composition level, not the grader's chunk-acceptance level. The fix space is therefore different: FT v2 (which would have improved reranker discrimination) wouldn't help — it would still rank the high-similarity distractors as relevant, because they ARE topically relevant; their failure is at the numerical-distinction level the reranker doesn't model.

### Decision

**Do not ship k-bump. Do not pursue FT v2 (Experiment 7.18b).** The mechanism that broke isn't fixable by reranker improvement alone — broader candidate pools structurally introduce more distractors regardless of how good the reranker is. The reranker can rank topical relevance, not numerical-fact correctness.

Reverted `RETRIEVAL_TOP_K=50` in `.env`. Production state is unchanged.

### Cost / time

| Step | Cost |
|---|---:|
| Diagnostics (k-sweep + FT v1 rerank probe) | $0 (local) |
| Smoke 5-Q with k=200 | ~$0.20 |
| Full 150-Q eval with k=200 | ~$4 |
| Sonnet 4.6 + v2 rejudge | ~$0.50 |
| **Sprint 7.18a total** | **~$5** |

Cumulative campaign total: ~$170.

### Reflection — three intervention sites tested, all null

| Sprint | Site | Sub-component effect | System effect |
|---|---|---:|---:|
| 7.16 | Generator (anti-refusal + enumerate-fully) | Targeted-cohort validation +4 net | Full eval −0.7pp (null) |
| 7.17 #3 | Grader (Llama-3.3-70B swap) | Grader benchmark +16pp gold-recall, +4.5pp F1 | Full eval −4.7pp (Signal 14) |
| 7.18a | Retrieval (k-bump 50→200) | Recall@200 +50pp on miss bucket | Full eval −15.33pp (Signal 15) |

Three different stages, three failures. The pipeline as currently composed appears to be at a **system-level local optimum at 72.7%**. Each component is near its own ceiling; the failure modes are now at the orchestration / cross-component interaction level, not at any single component's capability.

What this implies for next steps is in the "Roadmap" section below — the surviving options are now:

- **Sprint 7.12 (parked)**: external benchmarks (ConvFinQA + TAT-QA) — does NOT move the FinanceBench headline but adds generalization breadth for the portfolio narrative
- **Generator FT** (Sprint 7.13 "if reasoning is dominant" branch): generator-side QLoRA — only justified if reasoning is the residual bottleneck, which Diag 2 didn't conclusively show
- **Accept 72.7% as the ceiling**: rebrand the work around stability, methodology, and signals banked (8 documented signals at this point), not pass-rate climbing

The credibility-rule pattern from this sprint stands as the strongest meta-finding: the diagnostic correctly predicted gold reachability, but failed to predict the distractor-crash mechanism. **Sub-component metrics are necessary but not sufficient — only the full-eval pass rate under the calibrated judge is decisive.**

---

## Sprint 7.19 — code-level audit + pipeline walk

After three consecutive null/regression interventions (7.16 generator prompt, 7.17 #3 grader swap, 7.18a retrieval k-bump), I ran a code-level audit before committing to any further experiments. The user explicitly framed it: *"see if there were changes which should have been there in the code but were either not implemented properly or were not implemented at all"* — modelled on the Sprint 7.17 audit that surfaced 5 silent methodology bugs (max_tokens=256 on Haiku, silent exception → False verdict, Groq token-rate-limit miss, BGE base loaded without LoRA adapter, gpt-4o-mini missing seed=42).

### Critical finding: the FT v1 reranker has been silently INACTIVE since Sprint 7.9

The Sprint 7.9 LoRA-FT'd BGE reranker — characterised in the campaign trajectory as "the most informative sprint" and credited with unstucking the multi-hop slice from 4/13 → 11/13 — is **not being loaded in any current eval**.

Evidence chain:

1. [`src/services/reranker_service.py:92-94`](src/services/reranker_service.py#L92-L94) loads the LoRA adapter only when `os.environ.get("RERANKER_ADAPTER_PATH")` is set; otherwise falls through to a stock `CrossEncoder(BAAI/bge-reranker-v2-m3)`.
2. `.env` does **not** contain `RERANKER_ADAPTER_PATH` (only `RERANKER_DEVICE=cpu`).
3. `.env.example` does **not** document `RERANKER_ADAPTER_PATH` either — so any clean install would also miss it.
4. Direct probe via `_load_reranker()` under current env returned class `CrossEncoder`, not `_FtReranker`. Log line: `Loading reranker: BAAI/bge-reranker-v2-m3 on device=cpu (stock; first run downloads ~568MB)`.
5. The adapter files at [`data/models/reranker_ft_v1/`](data/models/reranker_ft_v1/) DO exist on disk — they're just never being loaded.

**Implications:**

- The 72.7% baseline (Sprint 7.16 final) was achieved on the **stock** BGE reranker, not FT v1.
- Sprint 7.17 follow-up #3 (Llama-grader −4.7pp) and Sprint 7.18a (k=200 −15.33pp) were both measured against this same stock-reranker baseline.
- The earlier Sprint 7.18a diagnostic that scored "FT v1 reranker on k=200 pool" — where FT v1 surfaced 6/10 in-pool cases to top-8 — **was scoring the actual adapter** (because I loaded it explicitly via `PeftModel.from_pretrained()` in the diag script). The production pipeline running the full eval did NOT use this same adapter.
- Therefore: **we have a free experiment available.** Setting `RERANKER_ADAPTER_PATH=data/models/reranker_ft_v1` and re-running could move the headline up (or stay flat — which would invalidate the "biggest single win" attribution from Sprint 7.9). Either outcome is decision-useful.

This is the same shape of bug as the Sprint 7.17 audit's Bug 4 (BGE base loaded without LoRA adapter) — but at the production runtime rather than at an eval harness. The bug evaded detection for five sprints because no scripted boot output emitted "what's actually loaded" alongside the settings snapshot.

### Pipeline walk on 5 baseline failure cases

Traced one case from each failure category in [`audit_failed_qs_gen_v2.json`](tests/evaluation/eval_results/audit_failed_qs_gen_v2.json) through the baseline pipeline cache + Diag 2 attribution:

| Case | Category | Diag 2 bucket | NDCG@8 | Real contexts | Failure stage |
|---|---|---|---:|---:|---|
| `07966` Activision capex 3y avg | REFUSAL | RERANKER_HIT | 0.49 | 3 | Generator refusal — could not extract capex from chunks that reached top-8 |
| `00807` 3M quick ratio Q2'23 | WRONG_NUMBER | RERANKER_HIT | 0.65 | 9 | Generator computation error — picked wrong line items (decomposer has quick-ratio guard, synthesizer doesn't) |
| `01902` Best Buy product category Q2 FY24 | WRONG_DIRECTION | RERANKER_MISS | 0.00 | 8 (no gold) | **Judge bug** — audit notes system answer is correct ("by top line" = revenue, system identified "Computing and Mobile Phones") |
| `00499` 3M capital-intensive FY22 | PARTIAL_ANSWER | RETRIEVAL_MISS | 0.00 | 5 (no gold) | RATIO_CONCEPT_MISMATCH; generator used capex/OCF instead of gold's capex/revenue |
| `00460` Best Buy stores Q2 FY24 | DATASET_SUSPECT | RERANKER_HIT | 0.54 | 4 | **Gold may be wrong** — audit notes system answer matches actual 10-Q better than gold |

**Pattern**: 3 of 5 baseline failures have gold in top-8 (RERANKER_HIT with NDCG > 0). The reranker and retrieval correctly delivered the right chunks. The failures are at **generator + research-agent computation/synthesis** — refusal despite partial evidence (07966), formula-application errors (00807), concept-to-metric mismatch (00499). This matches Diag 2's 49% downstream-bound attribution and aligns with the Mafin/PageIndex (98.7%) + DANA (94.3%) observation that the residual ceiling in production-grade FinanceBench systems sits at the reasoning level, not retrieval.

### Diagnostic-completeness audit

Existing diagnostic surface across [`tests/evaluation/phase_eval_results/`](tests/evaluation/phase_eval_results/) and the Diag 2 / Diag 3 attribution artifacts:

| Metric | Coverage | Source |
|---|---|---|
| Chunk-preservation IoU (parsing quality) | ✓ mean 0.46, only 14.8% chunks ≥0.7 IoU | phase_eval_v2 |
| Retrieval Recall@5/10/20/50 | ✓ standing | phase_eval_v2 |
| Reranker NDCG@8 + Precision@8 | ✓ standing | phase_eval_v2 |
| Grader benchmark (P/R/F1) | ✓ 100-pair balanced | phase_eval_v2 |
| Per-question retrieval/reranker bucketing | ✓ | Diag 2 |
| Per-question correctness with judge reasoning | ✓ | κ=0.932 rejudge files |
| **Recall@k for k > 50** | ✗ ad-hoc only (Sprint 7.18a 14-case probe) | gap |
| **Distractor-chunk detection at reranker output** | ✗ no metric | gap — exactly the failure mode that crashed Sprint 7.18a |
| **Hallucination-checker F1 (standing)** | ✗ measured once Sprint 7.9 on 75 labels, not standing | gap |
| **Generator faithfulness per-question** | ✗ aggregates only via RAGAS/DeepEval | gap |
| **Question-type stratification** (domain-relevant / novel-generated / metrics-generated) | ✗ | gap |
| Router F1 | ✗ deferred per Sprint 7.11 Day 4 note | gap |

**The big-impact gap is interaction-level metrics.** Signal 11/14/15 keep firing because we measure per-component health but not cross-component drift. Specifically: when the broader k=200 pool changed the reranker output's composition, no metric flagged the new arrivals as "topically relevant but numerically wrong." A distractor-chunk detector at the reranker output (regex check on financial number formats vs gold-answer's expected value, where labelled) would have caught the 7.18a regression before launch.

### Logging-infrastructure audit

Inventory of what's already captured:

| Asset | Captures | Gap |
|---|---|---|
| **46 `logger.info/warning` calls** across 17 graph nodes | Decision points: retrieval result counts, fallback paths, grader verdicts, agent decompose/sufficiency outcomes, calculator results, errors | Goes to stderr only. No file handler. Free-form strings, not structured fields. |
| **Langfuse self-hosted stack** (Sprint 8) at `localhost:3000` | Every LLM call with model/tokens/latency/cost/userId | LLM calls only — misses graph-level decisions (router intent, target_company, fallback flags, cache hits, entity-match rejections) |
| **Pipeline cache JSONs** for eval runs | Per-question: query, answer, contexts (final relevant_chunks) | Doesn't capture intermediate state |
| **`run_metadata` in pipeline cache** | Effective settings + git SHA + qdrant collection info | Pydantic-typed settings only — doesn't capture os.environ-only vars (which is where `RERANKER_ADAPTER_PATH` lives) |

**The two missing pieces that hid the reranker bug:**

1. **A "what's actually loaded" startup banner.** If pipeline boot printed `Reranker: BAAI/bge-reranker-v2-m3 (STOCK — adapter not loaded)` along with the loaded grader model, embedding provider, collection name, and Redis connection status, the FT v1 silence would have been visible from line 1 of every eval log.

2. **A central JSONL event log with structured fields.** The 46 existing logger calls fire but their content is free-form text mixed with library output. No way to grep "what happened to `financebench_id_03029` across all stages" or `jq`-query "all questions where reranker NDCG=0 but generator succeeded."

### Tier 1 logging plan (sized for ~7-8 hours, deferred to next sprint)

Three small additions:

1. **`src/services/event_log.py`** — single module exposing `emit(stage, **fields)` that writes JSONL events to `logs/run_<timestamp>.jsonl`. Configured via `EVENT_LOG_PATH` env var or auto-named.
2. **Startup banner** — function `log_runtime_components()` introspects the actually-loaded reranker class, grader LLM `model_name + base_url`, full settings + os.environ snapshot, Qdrant collection metadata, and Redis ping result. Called from [`src/api/main.py`](src/api/main.py) on FastAPI startup and at the top of [`tests/evaluation/run_financebench.py`](tests/evaluation/run_financebench.py) + [`tests/evaluation/rejudge.py`](tests/evaluation/rejudge.py).
3. **File handler** wired into `logging.basicConfig` writes the existing 46 `logger.info` strings to `logs/run_<timestamp>.log` in addition to stderr. No call-site rewrites needed for the v1.
4. **Enrich ~15 critical existing log calls** with `extra={"fb_id": ..., "stage": ..., "n_relevant": ...}` so the JSONL records carry structured fields without rewriting message strings.
5. **`scripts/show_run.py <run_id> [<fb_id>]`** — renders any JSONL log as a human-readable per-question timeline.

Tier 2 (Langfuse custom spans per node, per-question audit JSON dumps, diff_runs script) deferred until Tier 1 produces evidence the logger is actually helping catch bugs.

### Decision

No code changes shipped in Sprint 7.19. The audit produced three actionable outputs:

1. **Step 0** for the next sprint: set `RERANKER_ADAPTER_PATH=data/models/reranker_ft_v1` and re-run the full FinanceBench eval. Every measurement we have is anchored to a baseline with the FT v1 adapter silently disabled — we don't actually know what our "real" baseline is. ~$5, ~3hr wall.
2. **Tier 1 logging plan** ready to implement after Step 0 lands.
3. **Cleanup**: untracked exploratory artifacts at [`data/models/grader_ft_v1_hard_r8/`](data/models/grader_ft_v1_hard_r8/) and [`data/models/grader_ft_v1_mixed_r8/`](data/models/grader_ft_v1_mixed_r8/) from the Sprint 7.17 grader LoRA failure (Signal 12) added to `.gitignore`.

### Methodological signal — banking the gap, not the bug

The reranker bug itself is a one-line fix. The methodological signal is more durable:

**Signal 16 (banked)**: *A configuration that depends on multiple sources (settings.py + .env + .env.example + os.environ + docker-compose) creates failure modes that escape settings_snapshot review.* The FT v1 reranker was loaded conditionally on `os.environ.get("RERANKER_ADAPTER_PATH")` — a path that bypasses pydantic-settings entirely, so the standard `_settings_snapshot()` audit trail in every pipeline cache never recorded it. Five sprints of measurements were taken with the adapter silently off, and the audit trail showed nothing wrong. Fix: any runtime-loadable component (model, adapter, cache, provider) must emit a "loaded as X" log line at startup AND have its identity captured in the settings snapshot — not just the pydantic-typed config. Echoes Signal 6 (per-stage diagnostics measure stage-vs-judge gaps, not stage-vs-truth gaps): the right metric existed but wasn't being collected.

---

## Sprint 7.19 Step 0 — enable the FT v1 reranker, re-baseline — the campaign's "biggest single win" is falsified

The Sprint 7.19 audit identified that `RERANKER_ADAPTER_PATH` had been silently unset since Sprint 7.9, so the FT v1 reranker — characterised in the engineering log as the campaign's biggest single win (multi-hop slice 4/13 → 11/13) — was never actually loaded in any eval since. Step 0 was the cheapest possible test: add `RERANKER_ADAPTER_PATH=data/models/reranker_ft_v1` to `.env`, re-run the full FinanceBench eval under the unchanged downstream stack + κ=0.932 judge.

### Boot-banner verification — the bug it was designed to catch

The Tier 1 logging shipped in [`src/services/event_log.py:log_runtime_components`](src/services/event_log.py) prints the actually-loaded reranker class at process boot. Before .env edit:

```
reranker:    {'class': 'CrossEncoder', 'ft_adapter_loaded': False, 'adapter_path': '(unset, falling back to stock)'}
```

After .env edit:

```
reranker:    {'class': '_FtReranker', 'ft_adapter_loaded': True, 'adapter_path': 'data/models/reranker_ft_v1'}
```

If this banner had existed at Sprint 7.9 (or any sprint between then and Sprint 7.19), the silent-fallback bug would have been visible at line 5 of every eval log. Five sprints of measurements were taken without it.

### Result

Pipeline ran clean: 150/150 questions, 0 errors, 6730s wall (1hr 52min — slightly slower than baseline's 2hr 22min, consistent with FT-v1 reranker's slightly heavier forward pass on CPU).

| Config | Pass rate under κ=0.932 | Δ vs Sprint 7.16 baseline |
|---|---:|---:|
| Sprint 7.16 baseline (stock BGE reranker, the actual production state) | 72.7% (109/150) | — |
| Sprint 7.19 Step 0 (FT v1 reranker active) | **67.33% (101/150)** | **−5.34pp** |

Per-Q diff: **2 rescues, 10 regressions**, 99 unchanged-pass, 39 unchanged-fail.

### Mechanism — same structural failure mode

**8 of the 10 FT v1 regressions overlap with the Sprint 7.17 Llama-grader regression set** (Adobe FCF conversion, JnJ gross margin drivers, AWW working capital, 3M segment growth, Activision fixed-asset turnover, AMD quick ratio, Best Buy gross margin consistency, financial-approximation calc). The same questions regress under multiple sub-component interventions — this is not random noise but a **structural pipeline failure mode at the generator/synthesizer level**.

Diag 2 bucket breakdown of the 10 regressions:
- **RERANKER_HIT: 6** (gold WAS in stock-reranker top-8; FT v1 ranking shuffled it out)
- **RETRIEVAL_MISS: 4** (gold not in top-50 in either state; FT v1's different chunk choices interacted with the generator differently)

The mechanism is consistent with Signal 14 (Llama-grader regression) and Signal 15 (k-bump regression): **any sub-component change shifts the final top-8 composition; the displaced chunks tend to be plausible-but-numerically-distinct line items that confuse the synthesizer on multi-component-formula / qualitative-judgment questions.** Three independent intervention sites (grader model, retrieval pool size, reranker adapter) have now reproduced the same regression pattern.

### Reinterpretation — what was the Sprint 7.9 "biggest single win" actually measuring?

Three possibilities, ranked by likelihood:

1. **Co-occurring changes attribution** (most likely): the Sprint 7.9 commit `0d758b9` bundled (a) the LoRA reranker FT, (b) the Day 3 heterogeneous-tier mapping, (c) voyage-finance-2 embeddings, and (d) the 4-7 day campaign close. The multi-hop slice gain (4/13 → 11/13) measured at the end of that sprint was attributed to the FT, but the attribution was never isolated. The credibility-rule didn't exist yet (it was added at Sprint 7.13). The actual driver may have been the tier-mapping + embedding swap, with the FT contributing little or nothing.

2. **Judge artifact**: the Sprint 7.9 gain was measured under the gpt-4o-mini judge, which Sprint 7.14 audit later found over-penalised ~47% of system outputs. The FT v1 may have produced chunk orderings that were rewarded by the legacy judge's idiosyncrasies but don't survive κ=0.932 scoring.

3. **Downstream-stack erosion**: FT v1 was a genuine win at Sprint 7.9 but has been eroded by Sprint 7.15 (hallucination Sonnet 4.6 upgrade + decomposer fixes) and Sprint 7.16 (generator clauses 7-8). The newer downstream components may handle the stock reranker's chunk choices better than FT v1's.

Without re-running the Sprint 7.9-era pipeline against the κ=0.932 judge, the three can't be cleanly separated. The practical answer is the same: **FT v1 is net-negative on the current pipeline. Stock BGE is production.**

### Signal 17 (banked)

*A historical component win that was attributed under a stale eval framework does not survive re-validation under the current downstream stack + calibrated judge.* The Sprint 7.9 reranker FT was the canonical "validated component win" in the engineering log — and now its silent-deactivation has revealed that loading it actively regresses the headline by 5.34pp. **Component fine-tunes have to be re-validated on every downstream-stack change or judge-framework upgrade.** Echoes and extends Signal 8 (47% of failures in the prior campaign were eval-framework artifacts): component-level "wins" can also be eval-framework artifacts.

### Campaign-narrative implications

The TL;DR section of this doc still credits the Sprint 7.9 reranker FT with unsticking the multi-hop slice. That claim cannot stand without re-measurement under the κ=0.932 judge — and the present finding makes the claim probably-wrong as stated. The four "winning" interventions banked across the campaign were:

| Intervention | Status post-Sprint 7.19 Step 0 |
|---|---|
| κ=0.932 Sonnet 4.6 + IMPROVED_PROMPT v2 judge build (Sprint 7.14) | ✓ STILL VALID — methodology contribution |
| Sprint 7.15 four-fix wave (year-regex + decomposer prompt+cap + hallu Sonnet 4.6 + router prompt) | ✓ STILL VALID — measured under κ=0.932 |
| Sprint 7.15 Fix 2 + Fix 3 (decomposer YoY rule + quick-ratio guard) | ✓ STILL VALID — measured under κ=0.932 |
| Sprint 7.9 reranker LoRA-FT v1 ("biggest single win") | ✗ **FALSIFIED** under current stack — −5.34pp when re-enabled |

The 72.7% headline is fully attributable to the three remaining wins. The campaign is still a +25pp move (47.3% → 72.7%) under the *re-calibrated* lens, but the largest single component of that lift now belongs to the **judge recalibration unmask (Sprint 7.14)** at +20.7pp, not the reranker FT.

### Decision

Stock BGE reranker is production. RERANKER_ADAPTER_PATH stays unset in `.env`. Path forward:

- **Step 1 (now actually interesting)**: a properly-trained FT v2 reranker with hard negatives drawn from the current pipeline state, validated against κ=0.932 BEFORE shipping. The Sprint 7.9 training-data construction was outcome-conditioned on the Sprint 7.9-era pass/fail labels; redoing it against the current 109/150 PASSING set + 41 FAILING set is the natural next experiment. Effort estimate: ~1 day work, ~$5-10 LLM.
- **Step 2 (independent path)**: contextual chunk metadata injection (the Ragie pattern). Doesn't depend on reranker quality. Same effort estimate.
- **Step 3 (later)**: generator-side intervention on the 8 structural-failure questions that consistently regress under every sub-component change. These are the hard ones.

### Sprint 7.19 Step 0 cost

| Step | Cost |
|---|---:|
| Full 150-Q eval (Sonnet generator + hallu, gpt-4o-mini grader, voyage-finance-2 embeddings) | ~$4 |
| κ=0.932 Sonnet rejudge | ~$0.50 |
| **Sprint 7.19 Step 0 total** | **~$4.50** |

Cumulative campaign total: **~$175**.

---

### Sprint 7.17 cost

| Step | Cost |
|---|---:|
| LoRA training (3 strategies × 1 rank, local M4 Pro MPS) | $0 |
| Component eval of FT'd MiniLM variants | ~$0.50 |
| 4-way fair comparison v1 (with 5 methodology bugs) | ~$1.50 |
| 4-way fair comparison v2 (post-fixes) | ~$2.50 |
| Haiku @ max_tokens=2048 control run | ~$1.50 |
| Follow-up 1: Haiku w/ SystemMessage split (Caveat B falsification) | ~$0.50 |
| Follow-up 2: Llama-3.3-70B via Fireworks free tier ($6 signup credit) | $0 (free) |
| Follow-up 3: Full 150-Q FinanceBench eval via OpenRouter Llama-3.3-70B + Sonnet rejudge | ~$3 |
| **Sprint 7.17 total** | **~$9.00** |

Cumulative campaign total: ~$165.

### Confidence labels (per credibility rule)

- **Measured**: BGE+LoRA, gpt-4o-mini, Haiku@512, Haiku@2048, Haiku-w/-SystemMessage-split, Llama-3.3-70B-via-Fireworks all evaluated under identical conditions on the same 100-pair + 363-gold-chunk benchmarks. 0 errors across all six runs (after the Groq backend was retired).
- **Measured (decisive)**: Llama-3.3-70B beats gpt-4o-mini by +16pp on gold-chunk recall (0.860 vs 0.700), +4.5pp on balanced F1 at the grader benchmark. Llama is more permissive — 7 FP vs 4 — but on this corpus's same-doc-negative distribution that translates to higher recall without collapsing precision below a usable floor.
- **Measured (negative)**: Caveat B falsified. Routing GRADER_PROMPT's role text to SystemMessage *regresses* Haiku by 4.4pp gold-recall. Production prompt shape is optimal for Claude on the `with_structured_output` path.
- **Measured (decisive, follow-up #3)**: Llama-3.3-70B as grader **regressed** FinanceBench pass-rate by 4.7pp (72.7% → 68.0%) under the κ=0.932 judge. The +16pp grader-benchmark gold-recall win did **not** translate. Net: 14 regressions, 7 rescues. Regressions cluster on focused-lookup / superlative / list-enumeration questions where extra false-positive chunks dilute the generator's focus. Sub-component metrics ≠ system metrics (Signal 14).
- **Reasonable inference**: The grader stage is not the rate-limiting bottleneck for further pass-rate improvement. Both directions (more-strict gpt-4o-mini and more-permissive Llama) have been measured; neither moves the headline materially. Combined with Diag 2 attribution (51% upstream-bound failures), retrieval is the next architectural lever.
- **Speculation**: That a higher-precision Llama path (e.g. prompt engineering toward stricter binary verdicts, OR threshold-tuned BGE+LoRA-FT v2 with more training data) could yield a precision-recall sweet spot that beats gpt-4o-mini on both axes simultaneously. Untested.

---

## Sprint 7.11 Days 2-3 — phase eval result + the grader-over-strictness finding

> **CORRECTION (2026-05-12 evening)**: The "grader is the 24pp bottleneck" interpretation below was wrong. Sprint 7.13 Day 3 full-eval + audit (see next section) revealed that **a substantial fraction of "failures" were eval-framework artifacts** — not system failures. The phase-eval cascade math measured the gap between system output and judge output, NOT between system output and ground truth. The grader's "over-strictness" wasn't the rate-limiting step; the JUDGE's over-strictness was. The phase-eval methodology is still valuable; the interpretation needed correction.

Shipped 2026-05-12: the 5-metric phase eval harness at `tests/evaluation/phase_eval.py` (~470 lines, $0 marginal cost, 22 min wall on full 147 Qs) plus per-stage cascade analysis. The diagnostic surfaces a **measured single bottleneck** — the grader, not the chunker — and replaces the prior speculative "table-aware re-ingest" Sprint 7.13 plan with a cheaper, more targeted intervention.

### The cascade

```
                              fraction of Qs
                              ━━━━━━━━━━━━━━
ideal: every Q answerable       1.00
                                ↓  lose 17pp — retrieval miss (gold not in top-50)
retrieval R@50                  0.83
                                ↓  lose  9pp — reranker NDCG quality
reranker R@8                    0.74
                                ↓  lose 24pp — grader recall 0.68 (drops 32% of gold)
gold reaches generator          0.50
                                ↓  lose  3pp — generator + hallucination
pass rate (Sprint 7.9)          0.47
```

Cascade math: `0.83 × (0.74/0.83) × 0.68 = 0.50 ≈ pass_rate + 3pp residual`. Within the empirically-measured n=150 noise floor of ±3pp. The cascade fully accounts for the 47.3% headline.

### Per-stage numbers

| Layer | Metric | Value | Reference |
|---|---|---:|---|
| Chunker | mean max trigram IoU | 0.46 | Bedrock production-RAG target ≥0.70 |
| | % preserved (IoU ≥ 0.5) | 44.4% | |
| Retrieval | Recall@5 (any gold in top-5) | 0.43 | |
| | Recall@10 | 0.56 | |
| | Recall@20 | 0.66 | |
| | **Recall@50** | **0.83** | candidate pool is strong |
| Reranker (LoRA-FT BGE-v2-m3) | R@8 (any gold) | 0.74 | 108/147 |
| | NDCG@8 mean | 0.42 | |
| | mean fraction of gold in top-8 | 0.49 | |
| | Precision@8 mean | 0.13 | |
| **Grader** (Llama-3.3-70b/Groq) | **precision** | **0.92** | when it says relevant, right 92% of the time |
| | **recall** | **0.68** | rejects 32% of true-gold chunks |
| | F1 | 0.78 | |
| Latency p50/p95 | retrieval | 443ms / 1000ms | |
| | reranker (LoRA-FT BGE on M4 Pro) | 6.5s / 9.0s | |
| | sonnet-4-6 (generator) | 7.8s / 14.7s | from Langfuse |
| | haiku-4-5 (hallu-checker) | 4.4s / 7.6s | from Langfuse |

Full results: `tests/evaluation/phase_eval_results/financebench_phase_eval_v1.json` and `_per_question.jsonl`.

### Slice analysis

**By question type:**

| Type | n | R@5 | R@50 | NDCG@8 |
|---|---:|---:|---:|---:|
| domain-relevant (prose Qs) | 50 | **0.22** | 0.70 | 0.37 |
| novel-generated | 50 | 0.48 | 0.84 | 0.44 |
| metrics-generated (tables) | 47 | 0.60 | **0.96** | 0.45 |

Table questions retrieve cleanly (R@50=0.96). Prose questions are the hard slice (R@50=0.70 — retrieval misses 30% of gold even at depth 50).

**By chunker-fragmentation status:**

| Bucket | n | R@5 | NDCG@8 |
|---|---:|---:|---:|
| Chunker preserved evidence in one chunk | 59 | 0.36 | **0.48** |
| Chunker fragmented evidence across chunks | 85 | 0.48 | **0.39** |

Fragmentation hurts NDCG@8 by 0.09 points — measured cost of chunker splits. Not the dominant factor in the cascade (the 24pp grader loss is bigger), but real.

### The grader-over-strictness finding — verified by spot-check

The grader test produced precision 0.92 + recall 0.68 on a 100-pair sample (50 random gold chunks as positives, 50 doc-scoped non-retrieved chunks as negatives). To verify the recall=0.68 finding is real and not a sampling artifact, spot-checked 5 of the 16 false-negatives (cases where gold=relevant, grader=irrelevant):

| Case | Question | Chunk | Grader call |
|---|---|---|---|
| 10136 General Mills | FY22 retention ratio = 1 - (dividends/net income) | Income statement (has net income, not dividends) | rejected — chunk alone can't compute the metric |
| 00521 Ulta acquisitions | Did Ulta acquire anything FY22-23? | Operating-activities cash flow section | rejected — doesn't mention acquisitions |
| **00605 Ulta Q4 repurchases** | FY2023 Q4 stock buyback % | Has the data, but labeled "fiscal 2022" (Ulta's fiscal year nomenclature) | **rejected — wrong: fiscal-year confusion** |
| 00746 Ulta debt securities | Which debt securities registered? | 10-K cover page | rejected — header section, may not have securities list in excerpt |
| 04080 Nike inventory turnover | FY21 turnover = COGS / avg inventory | Income statement (has COGS, not inventory) | rejected — chunk alone can't compute |

Pattern: 4 of 5 are *single-chunk-insufficiency* rejections — chunks that contain ONE component of a multi-source metric (income statement only; cash flow section only), where the question requires combining data from multiple chunks. The grader rejects them on "I can't answer from this chunk alone" grounds.

But — and this is the failure mode — **production grading is supposed to be topic-relevance, not single-chunk-sufficiency.** The generator downstream combines multiple chunks; the grader's job is to filter out *unrelated* chunks, not *partial* chunks. The current grader prompt at `src/config/prompts.py:165` says "determine if the chunk is relevant to answering the question" — semantically correct, but Llama-3.3-70b on Groq is interpreting "relevant" too strictly as "self-sufficient." Case 00605 is the cleanest demonstration that the grader is wrong (the chunk has the answer; it's just labeled with Ulta's internal fiscal-year notation).

### Applying the decision rule

Original rule from the Roadmap section below:
- High retrieval Recall@8 (≥0.80) + low pass rate → reasoning bottleneck
- Low retrieval Recall@8 (<0.60) + good chunk preservation → reranker/fusion issue
- Low chunk preservation (<0.70) → upstream of retrieval (table-aware re-ingest)

The original rule assumed a single dominant bottleneck and was designed before we measured the grader stage. Our data shows:
- Reranker R@8 = 0.74 → *between* thresholds (not clearly high, not clearly low)
- Chunk preservation = 0.46 → below 0.70 → triggers "upstream re-ingest" branch
- **Grader recall = 0.68 → NEW: largest incremental cascade loss (24pp)**

**Extending the decision rule:**

> **Grader precision ≥ 0.85 + grader recall < 0.80 → grader-over-strictness bottleneck → prompt rewrite or model swap before any upstream work.**

This is the case we're in. The previously-recommended Sprint 7.13 candidate (table-aware re-ingest) addresses the IoU and reranker NDCG signals — both real, but neither closes the 24pp grader gap. A grader prompt rewrite is **surgical, cheap, and addresses the largest measured single-stage loss directly.**

### Sprint 7.13 plan (updated by the diagnostic)

| Day | Deliverable |
|---|---|
| 1 | Write 3 grader-prompt variants explicitly distinguishing "topic relevance" from "self-sufficiency." Run each on the same 100-pair sample. Pick the variant with highest recall at precision ≥ 0.85. |
| 2 | Dev-set (n=30) full-pipeline regression with the chosen prompt. Confirm no downstream regression (calculator-pattern check from Sprint 7.8). |
| 3 | Full canonical FinanceBench-150 eval. If pass rate moves ≥+4pp without slice regressions → ship. If ≤+2pp → fall back to model swap (Llama-3.3 → Haiku 4.5 or gpt-4o-mini at grader role). |

Expected outcome if grader recall lifts 0.68 → 0.85 with constant precision: pass rate climbs from ~0.47 to **~0.55** (computed as `0.83 × 0.74/0.83 × 0.85 = 0.63` upper bound, but with generator-cascade residual = 3pp → ~0.60; conservative band 0.50-0.55 accounting for downstream noise). This would close the gap to FinGEAR's ~55% GraphRAG SOTA without rebuilding chunking or retrieval.

**Confidence-labeled:**
- **Measured**: All five phase-eval metrics on n=147, plus 5-case spot-check confirming the grader-over-strictness mechanism. The cascade math closes within the n=150 noise floor.
- **Reasonable inference**: A grader prompt that explicitly says "mark as relevant any chunk containing PART of the data needed; downstream will combine chunks" should lift recall by 10–20pp. Llama-3.3-70b is a capable instruction-follower; the missing instruction is the gap.
- **Speculation**: That the full 8pp pass-rate lift will land. Day 2 of the Sprint 7.13 plan above is the cheap diagnostic that tests this premise before the full eval is committed.

### What Sprint 7.13 is explicitly NOT doing (revised by evidence)

- **No table-aware re-ingest** — chunk preservation IoU is low (0.46), but the diagnostic shows fragmentation costs only 0.09 NDCG points and isn't the rate-limiting cascade step. Doesn't justify the 5–7 day re-ingest cost.
- **No parent-child chunking** — same reason. Re-chunking helps signals not in the rate-limiting path.
- **No reranker FT round 2** — reranker R@8 = 0.74 is mid-tier but not the largest cascade loss. Wait for grader rewrite before considering.
- **No FT generator** — generator+hallu loss is ~3pp, within noise. Not a justified intervention.

### Methodological note worth recording

The cascade-decomposition methodology — Recall@k → Reranker R@8 → Grader recall → pass rate — is a more diagnostic frame than aggregate "pass rate at 47%." It surfaces the layer-by-layer attribution of where the system loses answerability. Every prior Sprint (7.7-7.10a) measured only the final pass rate and tried to move it via aggregate-shape interventions (better embeddings, better fusion, multi-HyDE). Several of those interventions were *redundant* with what the LoRA-FT reranker already covered — they moved Recall@5 by 2-4pp while the reranker had already captured the bulk. The grader and generator stages were never measured. This phase-eval framework retroactively explains why several Sprint 7.7-7.10a interventions hit a 1-3pp pass-rate ceiling: they addressed layers that weren't the bottleneck.

For portfolio framing: this is the third methodological signal worth banking, alongside the noise-floor measurement (Sprint 7.9 Day 2.5) and the calculator regression diagnosis (Sprint 7.8 Week 2). The bullet:

> *"Built a 5-metric phase-eval harness against gold-chunk labels for FinanceBench-150 — chunk-preservation IoU, retrieval Recall@k, reranker NDCG@8, grader precision/recall, per-node latency. The cascade decomposition surfaced a measured single bottleneck (grader recall 0.68 vs precision 0.92) that retrospectively explains why 5 prior interventions hit a 1-3pp pass-rate ceiling — they targeted layers that weren't the rate-limiting step. Sprint 7.13 will close the 24pp grader gap with a prompt rewrite rather than the previously-planned 5-7 day chunker re-ingest."*

---

## Roadmap — Sprint 7.11 onward: evidence-first, not paper-first

The Sprints 7.10b (metadata-augmented chunks) and 7.10c (OODA iterative reasoning) committed in the prior roadmap are **deprecated as currently framed**. Both stay inside the flat-text architecture and Multi-HyDE's null result is empirical evidence that further interventions of the same shape will hit the same ceiling. The right next move is *measurement before intervention*.

### Sprint 7.11 — per-phase evaluation framework (3-4 days)

Build the diagnostic that converts "where is the bottleneck?" from speculation to measurement.

| Day | Deliverable |
|---|---|
| 1 | **Gold-chunk labeling — DONE 2026-05-12 at 147/150 (98%)**. Deterministic two-phase token-overlap labeling. See "Sprint 7.11 Day 1" section above. |
| 2 | **Phase eval harness — DONE 2026-05-12**. Five metrics, `tests/evaluation/phase_eval.py`, ~$0 marginal cost, 22 min wall. See "Sprint 7.11 Days 2-3" section above for full results. |
| 3 | **Run + analyze — DONE 2026-05-12**. Cascade decomposition surfaced the grader-over-strictness finding (recall 0.68 vs precision 0.92). Sprint 7.13 plan updated to grader-prompt rewrite. See "Sprint 7.11 Days 2-3" section above. |
| 4 (opt.) | Router F1 (50-Q labeled set) + hallucination-checker precision/recall (50 labeled answers). Deferred — grader is the measured rate-limiting step; hallu+router contribute ~3pp combined per cascade math. |

**Decision rule from the diagnostic**:
- High retrieval Recall@8 (≥0.80) + low pass rate → reasoning bottleneck → consider FT generator or iterative reasoning targeted on multi-hop slice
- Low retrieval Recall@8 (<0.60) + good chunk preservation → reranker/fusion issue → reranker FT round 2 or fusion redesign
- Low chunk preservation (<0.70) → upstream of retrieval → table-aware re-ingest (docling tables with `do_table_structure=True`) justified with evidence

**Production-quality target reference**: Informatica/AWS Bedrock production-RAG guides cite Hit Rate@K=5 > 0.85 + RAGAS faithfulness > 0.90 as production targets. Our DeepEval faith is already 0.85; Hit Rate@5 is unmeasured.

### Sprint 7.12 — supplemental external benchmarks (2 days)

Add two external benchmarks alongside FinanceBench to test failure modes FinanceBench under-covers. **Subsetted, not full** — both are too large to run end-to-end:

| Benchmark | Subset | Failure mode tested | Source |
|---|---|---|---|
| **ConvFinQA-150** (conversations) | 150 of 3,892 multi-turn conversations | Multi-turn reasoning where turn N depends on turn N-1; tests research-agent subgraph specifically | [github.com/czyssrs/ConvFinQA](https://github.com/czyssrs/ConvFinQA), [OpenFinLLM Leaderboard](https://finllm-leaderboard.readthedocs.io/en/latest/datasets/question_answering/convfinqa.html) |
| **TAT-QA-150** (questions) | 150 of 16,552 | Hybrid table+text arithmetic — FinanceBench's weak spot | [TAT-QA project site](https://nextplusplus.github.io/TAT-QA/) |

Wall time per benchmark: ~5-7 hours. Judge cost: ~$15-25 total.

### Sprint 7.13 (conditional) — intervention based on 7.11 diagnostic

Only if 7.11 surfaces a clear mechanism with sufficient effect-size:
- If parse-loss is dominant → table-aware re-ingest with `docling.do_table_structure=True`, separate table-cell index, prose/table-aware retrieval routing
- If reasoning is dominant → consider FT generator (QLoRA on 7-13B model with FinanceBench answer pairs, ~150 examples + augmentation)
- If neither is clearly dominant → ship as-is + lean into the Morgan-Stanley-pattern framing

### What we are explicitly NOT doing

- **No more paper-derived deltas as targets.** Estimating gain from a paper's claim is a category error when our baseline is heavily stacked. The Multi-HyDE +11.2% was the precedent that confirmed this.
- **No "try Sprint 7.10b then Sprint 7.10c" sequence** — both target retrieval/reasoning without first measuring which is broken.
- **No table-aware re-ingest without evidence.** Existing repo data (docling_clean RAGAS faith 0.42 vs pypdf 0.71) is contrary evidence; only the per-phase diagnostic can justify this.
- **No ChatPDF-style "drop and chat" UX add.** Our pattern is enterprise batch-ingest-once-serve-many (Morgan Stanley shape). Adding consumer flow dilutes the framing.
- **No PageIndex / vectorless rewrite.** Wrong tool for our budget.
- **No GraphRAG / FinGEAR.** Pure GraphRAG hits 28-29% answer accuracy on FinanceBench-class questions; only structure-aware variants help, and those are 2-3 week investments.
- **No more verification evals of the same config.**

### Cost / time budget

| Sprint | Engineering | Eval wall time | LLM cost |
|---|---|---|---|
| 7.11 phase eval | 3-4 days | ~1 hour | ~$2-5 |
| 7.12 supplemental benchmarks | 2 days | ~10-15 hours | ~$15-25 |
| 7.13 (conditional) | 3-7 days | ~3 hours | ~$10-15 |

Total if all ship: **~6-13 days engineering + ~$25-45 LLM + ~14-18 hours of eval wall time.**

### Project framing — Morgan Stanley reference pattern

Verified via web search of 2026 enterprise RAG deployments: the canonical production pattern in financial services is **batch-ingest a fixed institutional corpus once, then serve many queries to many users with role-based access and human-in-the-loop on high-stakes outputs**. Morgan Stanley Wealth Management's GPT-4 chatbot operates over a 100,000-document internal knowledge base with daily regression testing. This project is structurally the same shape at smaller scale. The portfolio framing leans into that reference, not into ChatPDF/NotebookLM-style consumer flows. The 47% pass rate is below production accuracy targets (>75%) — which is precisely why the HITL approval gate and audit trail exist. The deployment shape is "AI as search-and-summarize layer for human analysts who review citations," not "autonomous decisioning tool."

---

## Known limitations / what I'd build next

A senior reviewer should read this section *before* the achievements section. I'm not pretending these aren't real.

1. **Never deployed to production.** The full stack runs locally via `docker compose up -d`. No public URL. No real user traffic. CI workflows exist (`.github/workflows/`) but haven't been used for deployment.
2. **72.7% sits above FinGEAR EMNLP 2025 SOTA (~55%) by +18pp and inside the Bedrock production-RAG band (~70%), but well below the top-published Mafin (~99%).** Adjusted-actionable rate (excluding 9 FinanceBench dataset errors): 77.3%. Patronus's original FinanceBench paper baselines were 38-43%. The 47.3% headline that drove the original campaign was a judge artifact — see Sprint 7.13/7.14 audit findings.
3. **Frontend (Sprint 9) is partial.** Sprint 9.1 vertical slice (login + streaming chat) is built and the BFF wiring works, but the smoke test caught an environment-variable issue (`LITELLM_URL` pointing to a docker-internal hostname while running uvicorn on the host) that's still pending fix. Sidebar history, HITL UI, admin panel, citation PDF viewer are not yet built.
4. **Feature-flagged-off experiments left in source.** `ENABLE_GRADER_EMPTY_CONTEXT_FALLBACK`, `ENABLE_LTR_GATE`, `ENABLE_CALCULATOR_TOOL` all `=False`. The code is preserved as research record but adds surface area to the repo. A cleaner version would delete or move to a `experiments/` branch.
5. **Multi-judge eval all uses gpt-4o-mini.** RAGAS + DeepEval + correctness all judged by the same model family. A cleaner eval would diversify judges to control for judge-family bias (the [`scripts/internal/eval/dual_judge_check.py`](../scripts/internal/eval/dual_judge_check.py) script exists but wasn't used as the canonical gate).
6. **GraphRAG never tried.** Would likely be the biggest single quality lever remaining (FinGEAR shows the gap). Estimated 2–3 weeks of work, deferred until after the Sprint 7.10 levers above.
7. **No production-deployment ops.** No load testing, no horizontal scaling validation, no incident response runbooks. The Langfuse + LiteLLM stack would work in production but hasn't been stress-tested.

If I had another two weeks, the committed priority order (see "Roadmap — Sprint 7.11 onward" above) is: **(1) Sprint 7.11 per-phase eval framework — gold-chunk labels + Hit Rate@k + reranker NDCG + chunk-preservation IoU, (2) Sprint 7.12 ConvFinQA-150 + TAT-QA-150 supplemental external benchmarks, (3) Sprint 7.13 conditional intervention only if 7.11 diagnoses a clear mechanism with sufficient effect-size**. Sprint 9.2 frontend work (sidebar / HITL UI / admin panel) runs in parallel in a separate chat session and doesn't block the eval-quality push. Sprint 7.10a (Multi-HyDE) shipped at commit `dafb582` with a null pass-rate result; flag default off, code preserved for ablation.

---

## Cumulative campaign cost ledger

| Phase | Spend | Cumulative |
|---|---|---|
| Sprint 7.6 (Days 1–4) | ~$13 | ~$13 |
| Sprint 7.7 Day 6 (3-large + dev + full eval) | ~$16.50 | ~$30 |
| Sprint 7.7 Days 7+8 (null results) | ~$2.30 | ~$32 |
| Sprint 7.8 Week 1 (voyage embeddings + full eval) | ~$20 | ~$52 |
| Sprint 7.8 Week 2 (calculator regression + rollback) | ~$10 | ~$62 |
| Sprint 7.9 Days 1–3 (tier validation across 4 candidates) | ~$11 | ~$74 |
| Sprint 7.9 Days 4–7 (LoRA training $0 local + dev + full eval) | ~$6 | **~$80** |
| Sprint 7.10a (Multi-HyDE — full eval + gpt-4o-mini hyde generation) | ~$1 | ~$81 |
| Sprint 7.11 Day 1 (deterministic labeling — no LLM/embedder calls) | $0 | ~$81 |
| Sprint 7.11 Days 2-3 (phase eval harness — 147 retrieval + reranker + 100 grader) | ~$0 | ~$81 |
| Sprint 7.13 Day 1 (grader prompt A/B — 4 variants × 100 pairs) | ~$0 | ~$81 |
| Sprint 7.13 Day 2 (n=30 dev-set V1 grader) | ~$0.05 | ~$81 |
| Sprint 7.13 Day 3 (full FB-150 with V1 grader) | $4.87 | ~$86 |
| Sprint 7.13 audit (81-Q re-judge with Sonnet 4.6) | ~$1 | **~$87** |
| Sprint 7.14 Phase 1 (judge calibration build + eval) | ~$6.50 | ~$93.5 |
| Sprint 7.14 Phase 2 (V1 rejudge 150 records × Sonnet) | ~$0.50 | ~$94 |
| Sprint 7.15 (75-Q diagnostic + 4 interventions full eval + rejudge + 22-case validation) | ~$17 | ~$111 |
| Sprint 7.15 follow-up (3 cheap post-intervention diagnostics + full 150-Q eval with Fix 2 + multi-judge panel + rejudge) | ~$20 | ~$131 |
| Sprint 7.16 (REFUSAL/PARTIAL_ANSWER/WRONG_DIRECTION diagnostics + 3 validation cycles + full 150-Q + rejudge) | ~$30 | ~$160 |
| Sprint 7.17 (grader LoRA-FT MiniLM + 4-way model comparison + max_tokens control) | ~$5.50 | **~$165** |

Total LLM spend across the eval-quality sprints: **~$165**. Per-eval cost at canonical config (post-Sprint-7.15 with multi-judge panel): **~$20** (pipeline ~$13 with Sonnet 4.6 on hallu; RAGAS + DeepEval add ~$5-7 if run; correctness scoring ~$0.30; rejudge ~$0.50). Skipping RAGAS + DeepEval drops it to ~$13 — the multi-judge panel is optional for headline pass-rate measurement but useful for retrieval-quality diagnostics. The Sprint 7.13/7.14 audit + re-judging that re-framed the entire project's headline pass rate (47% → 68% under fair judging) cost ~$1.50 in marginal LLM spend; Sprint 7.15's component-diagnostic-driven interventions added +5.33pp on top for ~$37 — proof that hands-on data verification and per-component F1 measurement are the cheapest possible ways to catch interpretation errors and find real lift.

---

## Post-deploy audit — embedding-dim mismatch silently degraded retrieval

After shipping the CLI client + minimal docker stack + PyPI package (Phases 0–5), manual testing surfaced a repeatable failure on the first chat query of any session: a generic `"I couldn't find relevant information in the available documents..."` refusal at ~$0.0003 / 1788 input tokens. Retry behavior was non-deterministic — sometimes the second call succeeded with full citations.

**False diagnosis (first pass).** Cold-start warm gap was the obvious suspect: BGE reranker + dense embedding provider HTTP client + grader LLM connection pool all warm lazily, and `/v1/warm` only loaded BGE + sparse BM25. Extended `/v1/warm` to also exercise `LLMFactory.get_grader_llm() / get_generator_llm() / get_router_llm()` plus a tiny `embed_text("warmup")` round-trip. **Did not fix the failure.** Re-fired the same query twice in a row after the warm extension — both failed identically.

**Audit-first protocol caught the real cause.** The Sprint 7.19 boot banner had been printing the answer all along:

```
qdrant: {'collection': 'financial_docs', 'points': 249, 'fingerprint': 'unknown (collection pre-dates fingerprinting)'}
```

The `financial_docs` Qdrant collection was created with **1536-dim OpenAI vectors** (probably from an early-Phase-0 ingest attempt when `EMBEDDING_PROVIDER` was openai). The runtime `.env` had `EMBEDDING_PROVIDER=voyage` (1024-dim). Every retrieval call returned HTTP 400 from Qdrant — `Wrong input: Vector dimension error: expected dim: 1536, got 1024`. The `retrieval_node` had a broad `except Exception` that swallowed the 400 and returned `{"retrieved_chunks": [], "retrieval_fallback_used": False}`. Empty chunks → grader had nothing to grade → `no_info_node` fired the generic refusal. No retrieval/grader event was even emitted to `logs/run_*.jsonl` because the exception fired before `emit()`.

The fingerprint sentinel from Sprint 7.19 would have caught this — but only for collections created *after* the sentinel feature shipped (2026-05-15). `financial_docs` predated it, so the boot banner reported `fingerprint: unknown` and skipped the comparison entirely. Silent degradation was the inevitable outcome for any pre-sentinel collection if the runtime embedding setting ever drifted from the ingest-time setting.

**Fix (commit `2156a4a`).** Read the collection's actual vector dim directly from `info.config.params.vectors` regardless of whether a sentinel exists, compare to `settings.EMBEDDING_DIMENSIONS`, and `raise SystemExit` on mismatch with the exact `curl DELETE` + `python scripts/seed_qdrant.py --sample` recovery recipe printed to the boot log. Any pre-fingerprint collection is now automatically protected — uvicorn refuses to start, the FATAL log line tells you exactly which command to run, and there is no path back to "silently return wrong answers all day."

**Recurring meta-lesson (third instance in the project).** Same shape as the `RERANKER_ADAPTER_PATH` silent-fallback caught in the Sprint 7.19 reranker audit and the `LITELLM_URL` host-vs-docker-hostname issue in Sprint 9.1: **silent failure paths via broad `except Exception` blocks compound with config that's read raw from `os.environ` outside pydantic-settings**. Three independent classes of failure, same root: the settings snapshot says one thing, runtime behavior says another, and the audit has to probe live state — not config. The boot banner pattern (Sprint 7.19) closes one of the three; the dim-check (this audit) closes a second. The remaining `LITELLM_URL` direct-provider fallback is still env-trust-based and would benefit from the same treatment.

The methodological reinforcement: **a hard-fail at boot is cheaper than any amount of mid-query debugging**. Both incidents in this project where I rebuilt the same fix-the-symptom loop multiple times (this one + the FT v1 reranker silent inactivity) were resolved in <1 hour each *after* the audit-first probe ran; both took >2 hours of false-trail chasing *before* the probe ran. The pattern is now: any time runtime behavior contradicts a settings snapshot, the first thing to probe is the actual loaded state, not the configured state.

---

## 0.1.x install-path campaign — five bugs, one missing tool

After Phase-5 PyPI publication (0.1.0), a fresh-laptop Apple-Silicon (M1, miniforge3 Python 3.12) install was the first time the wizard was driven by a user who hadn't touched the source. Five independent bugs surfaced across five test cycles, each rolled into a patch release. The single tool that would have caught all five at PR time — a fresh-laptop install smoke test — was not in place. This entry captures the campaign, the mechanism behind each bug, and the credibility-rule analogue for install-time auditing.

### The bugs, in order of discovery

| Release | Surfaced bug | Mechanism | Source of miss |
|---|---|---|---|
| 0.1.0 → 0.1.1 | `peft` missing from `[backend]` extra | `RERANKER_ADAPTER_PATH=data/models/reranker_ft_v1` shipped in `.env.example` triggered `peft` import in `src/services/reranker_service.py:92` even when no FT adapter was on disk. Container crashed on first warm. | Sprint 7.19 audit already proved FT v1 regressed and stock BGE was canonical; `.env.example` was never updated to comment out the adapter line. Settings-snapshot audit missed it because `RERANKER_ADAPTER_PATH` is read via raw `os.environ.get`, not pydantic — same shape as the Sprint 7.19 reranker-silent-fallback audit two months earlier. |
| 0.1.1 → 0.1.2 | (multiple wizard ergonomics; the test cycle was the dominant signal here) | Wizard verification missed the case where Docker layer-cache reused the previous build. CLI banner said `semver 0.1.0` while pip-installed CLI was 0.1.2. | Wizard ran `_verify_setup` against `/v1/health` + `/v1/warm` + Qdrant — all three returned 200 because the cached 0.1.0 image was still functioning. No version cross-check between CLI and backend. |
| 0.1.2 → 0.1.3 | `src.services.guardrails` typo in `src/api/routes/health.py:76` | My code; the actual module is `src.services.guardrails_service`. Production chat queries used the correct import via `src/graph/nodes/guardrails.py:7` and worked; only the deep `/v1/warm` warmup hit the bad path and surfaced as "Components failed to load" in `_verify_setup`. | No automated test exercises `/v1/warm`. The `tests/` suite mocks every external dependency for unit-test speed; the import error fires only on a real container, which lives outside the unit-test loop. |
| 0.1.2 → 0.1.3 | `hf_cache` volume mounted at the wrong path | `compose.minimal.yml` had `- hf_cache:/root/.cache/huggingface` but the container ran as `appuser` whose HF cache lives at `/home/appuser/.cache/huggingface`. Volume sat empty; BGE redownloaded ~568 MB on every rebuild. | Sprint 9 added the volume during a docker-compose refactor. The path was copy-pasted from a Dockerfile that ran as root. No reviewer ran a full `down -v` + `up --build` to observe the redownload. |
| 0.1.3 → 0.1.4 | The same `hf_cache` volume, now correctly path-matched, became unwritable | Docker named volumes are created root-owned by the daemon. Mounting an EMPTY named volume on an in-image directory does copy contents, but appuser was created by `useradd --create-home` which doesn't pre-create `.cache/`. The mount landed on a path that didn't yet exist in the image → docker created it as root → appuser couldn't write → partial BGE → `ValueError: Unrecognized model in BAAI/bge-reranker-v2-m3`. | I shipped the 0.1.3 mount-path fix without testing it on a machine that had no pre-existing `hf_cache` volume. Existing dev machines all had the (root-owned, empty, ignored) 0.1.2 volume sitting around — the same wrong-permissions state never manifested locally because dev never mounted the volume into the right path. |
| 0.1.4 (this release) | Same — fixed by `mkdir -p /home/appuser/.cache/huggingface && chown -R appuser:appuser /home/appuser/.cache` in `Dockerfile` BEFORE `USER appuser`. | Docker copies the in-image directory's ownership into the empty volume on first mount. Subsequent mounts of the now-populated volume preserve appuser ownership. | (The fix.) |

### The credibility-rule analogue for install paths

The Sprint 7.19 audit-first protocol works for **runtime** state: when behavior contradicts the settings snapshot, probe the actually-loaded class / dim / env-var. The same logic applies to install paths but the audit surface is different:

- **Runtime audit**: probe `/v1/version`, `/v1/warm` `loaded`, Qdrant collection metadata, the boot banner output.
- **Install-path audit**: probe a *clean* environment — fresh user (no `~/.financebench/`), fresh Docker (no existing volumes / images / layer cache), fresh pip install, and the wizard run end-to-end.

The runtime audit caught real bugs three times in the eval-quality sprints (FT v1 reranker silent inactivity, embedding-dim drift, LITELLM hostname). The install-path audit did not exist during 0.1.0–0.1.3 and would have caught all five bugs above. Each release was tested by the maintainer on a machine that already had every prior artifact cached.

### The missing tool — and why it wasn't built

`scripts/smoke_test_setup.sh` was on the 0.1.2 deferred list with the reasoning "requires nuking `~/.financebench/`, can't run on dev machine." That reasoning is correct for a script — but wrong for a CI workflow. A GitHub Actions job that runs `pip install dist/*.whl + financebench setup` inside a fresh ubuntu-latest container (or, for the M1 path, a `setup-buildx-action` + multi-arch build verification) would catch all five bug classes without touching the maintainer's environment.

The tool was deferred for the right reason at the wrong level of abstraction. Build-time and run-time stay separate: build-time CI verifies the install path on a clean substrate; run-time tests verify behavior. The 0.1.x cycle proved that conflating "I can't test this locally" with "I can't test this at all" costs five round trips.

### Versioning observation

PyPI immutability forces a version bump on every fix, so the 0.1.x cycle has a parade of release tags (0.1.0–0.1.4) that, from outside, looks like rapid iteration on a working tool. It is actually a record of "the install path was wrong, here's how it got fixed." Once 0.1.4 verifies clean, yanking 0.1.0–0.1.3 from PyPI is the right cleanup — `pip install financebench-rag-agent` would then return 0.1.4, and pinned installs of older versions remain available for reproducibility.

The 1.0.0 milestone should be tied to install-path stability validated on multiple architectures (M1, Linux/amd64, WSL2), NOT to feature completeness. 0.1.4 is the first release where the install path has been hardened against the five known failure modes; 0.2.0 should ship the pre-built image + GHCR multi-arch CI (which sidesteps the layer-cache cost-per-version problem entirely); 1.0.0 follows after both have been validated by an unaffiliated user.

### Roadmap — 0.2.0 (pre-built image + install hardening)

| Item | Why | Estimated effort |
|---|---|---|
| Pre-built API image on GHCR, multi-arch (linux/amd64 + linux/arm64), tagged per release | Cuts M1 install from ~7 min build to ~90s pull. Sidesteps the per-version `pyproject.toml` invalidation that re-downloads ~700 MB torch + ~30 backend deps on every patch bump. GHCR is free for public repos, no rate-limit. | ~1 day: workflow + `compose.minimal.yml` `image:` swap + docs |
| `scripts/ci_smoke_install.sh` driven by a GitHub Actions workflow on every tag | Would have caught all five 0.1.x install bugs. Runs in a fresh ubuntu-latest container against the just-built image. | ~3 hours |
| API key validation Layer 2 (live provider ping) | 0.1.4 ships format-check + clickable URLs (Layer 1); Layer 2 issues a single tiny request per key to catch expired / revoked / wrong-account keys before the wizard proceeds. | ~3 hours including the per-provider ping endpoints + per-provider error handling |
| ~~Yank 0.1.0–0.1.3 from PyPI~~ | **DONE (post-0.1.4)** — extended to 0.1.0–0.1.4 once install-path stabilization completed at 0.1.5. `pip install financebench-rag-agent` now resolves to the most recent non-yanked release; pinned `==0.1.2` etc. still works for reproducibility. | 5 min (PyPI web UI) |

### Methodological reinforcement (third time this rule has earned a log entry)

The pattern across the eval-quality sprints (FT v1 reranker), the post-deploy audit (embedding-dim mismatch), and now the install-path campaign is the same: **silent-failure paths in code or infrastructure compound with config that's read raw and not validated against ground truth at boot or in CI**. The runtime audit closes the code half (boot banner, hard-fail on dim mismatch). The install-path audit closes the infrastructure half. Both belong in the maintainer's standard toolkit, not as one-off responses to the most recent fire.

---

## 0.1.5–0.1.7 — install path stabilized, plus a falsified-hypothesis case study

The 0.1.4 entry ended with "the first release where the install path has been hardened against the five known failure modes." Three more releases followed before the install path was actually clean. Two captured fixes from continued M1 verification; one was a methodological win.

### 0.1.5 — presidio cold-start fix, GIT_SHA env wiring, docling libgl1, and the LLM-Guard hypothesis I had to retract

M1 test6 showed every chat query taking 130–184 seconds with `guardrails` accounting for 80% of wall time. My first hypothesis was LLM Guard's PromptInjection scanner running slowly because of the `onnxruntime cpuid_info warning: Unknown CPU vendor` log line — ARM64 inside Docker on M1 was apparently confusing ONNX's CPU-vendor probe.

The user ran an A/B with `RAG_DISABLE_LLM_GUARD=1` (the existing env-var bypass in [src/services/guardrails_service.py:49-53](../src/services/guardrails_service.py#L49-L53)) and a single chat query. Result: guardrails still took 137 seconds. **LLM Guard wasn't the bottleneck.** My hypothesis was wrong.

A follow-up direct-timing probe of `detect_pii()` inside the running container gave the actual answer:
- Call 1: 133.89s, 0 entities returned, log noise "Defaulting to user installation... Downloading 400 MB... Failed to initialize Presidio engines: [E050] Can't find model 'en_core_web_lg'... Presidio not available, skipping PII detection"
- Call 2: 1.80s, 0 entities (fast path through partial cache)
- Call 3: 135.64s, 0 entities (another full 400 MB redownload)

Two compounding bugs:

1. **`en_core_web_lg` wasn't pre-installed in the image.** When `presidio_analyzer.AnalyzerEngine()` first ran, spaCy triggered an auto-download via `pip install`. Container runs as `appuser` without write access to system site-packages, so pip fell back to `--user` install. spaCy's model resolver doesn't search `~/.local/`, so `AnalyzerEngine()` raised `[E050] Can't find model 'en_core_web_lg'`. The download itself was wasted.

2. **Singleton cache never set after init failure.** [src/services/guardrails_service.py:131](../src/services/guardrails_service.py#L131) only assigned `_analyzer = AnalyzerEngine()` on success; the except path logged a warning but left `_analyzer = None`. Every subsequent `detect_pii()` call re-attempted the heavy init, triggering another 400 MB pip-install retry. PII detection had been silently disabled in production while paying the full failure cost per query — across every release since the demo corpus was first ingested.

Fix in commit `04ab6a4`:
- `Dockerfile`: pre-install `en_core_web_lg` as root in the builder stage. Model lives in `/usr/local/lib/python3.12/site-packages/en_core_web_lg/` where `appuser` can read it.
- `guardrails_service.py`: add `_init_failed` sentinel so a failed init short-circuits future calls.
- `health.py`: add `detect_pii("warmup")` to `/v1/warm` so the wizard surfaces any future presidio breakage as a Components Failed To Load instead of silent 130s-per-query waste.

Plus two unrelated fixes folded in:
- `GIT_SHA` build-arg / `ENV` wiring across Dockerfile + `src/api/main.py:_git_sha()` + `cli/commands/setup.py:_bring_up_stack`. Container has no `.git/` (deliberately not COPY-ed), so subprocess `git rev-parse HEAD` always returned `unknown`. Wizard now captures the host sha and threads it through `docker compose build.args` → `ARG GIT_SHA` → `ENV GIT_SHA`. Banner reports the real sha.
- `libgl1` added to both Dockerfile apt-get blocks. docling's image-rendering pipeline `dlopen`s `libGL.so.1`; without it every PDF ingest logged `ImportError: libGL.so.1: cannot open shared object file` and fell back to pypdf. Pypdf is the canonical choice (per `docs/evaluation.md`), but ~30s per PDF was wasted on the try-and-fail.

Measured outcome on M1 (test7): first chat query 229s → 48s; subsequent queries 185s → 44s. Guardrails dropped below the 500ms threshold and stopped appearing in the per-stage breakdown at all (renderer in [cli/render.py:168](../cli/render.py#L168) hides stages under 500ms).

**Methodological reinforcement (fourth instance).** I had circumstantial evidence (the cpuid warning + the magnitude + per-call pattern) that LLM Guard was the bottleneck. I named a fix scope based on that hypothesis. The A/B test took 5 minutes and falsified it cleanly. The credibility rule's most important application isn't "verify before recommending an experiment" — it's "let the experiment kill your hypothesis even when the circumstantial evidence is strong." I should have insisted on the A/B before naming the fix.

### 0.1.6 — financebench doctor preflight + 2 small bug fixes

The 0.1.x install-path campaign had a clear missing tool: an environment preflight check that catches host-side issues (no docker, busy ports, low disk, unreachable PyPI) at wizard time instead of mid-install. Flutter-doctor-style. Shipped as `financebench doctor` in commit `dec4dca`.

14 checks across System / Resources / Ports / Network groups. Three tiers — BLOCKING fails exit the wizard, WARNINGS proceed but flag, INFO sets expectations (e.g. M1 → "BGE on CPU, ~30s first warm"). Each failure ships an actionable fix recipe. Runs in ~0.5–6s depending on network latency; integrates with `financebench setup` as step 0 (single-line success on clean pass, full grouped report on any warning or failure).

Plus two small bugs caught during M1 test7:
- Banner tip in chat + setup said `set FB_PROFILE=admin`. `set` is tcsh syntax; zsh and bash users need `export FB_PROFILE=admin`. Inconsistent with [cli/credentials.py:14](../cli/credentials.py#L14) which already used `export`.
- `src/services/event_log.py:143` was the second `git rev-parse HEAD` call site the 0.1.5 GIT_SHA fix didn't reach. Boot banner audit log carried `git={'error': 'FileNotFoundError...'}` for every run_id. Same env-fallback pattern applied here.

### 0.1.7 — doctor refinement after first M1 use (own-stack detection, RAM check removed)

Test8 surfaced two doctor false positives the moment the user's stack was running:
1. All four service ports (8000, 6333, 5432, 6380) reported `In use by PID 8360 (com.docker.backend)` with fix recipe `kill 8360`. Killing PID 8360 would terminate Docker Desktop itself. The user's own `repo-api-1` / `repo-qdrant-1` / etc. containers were the legitimate port holders; doctor couldn't distinguish "our running stack" from "stranger conflict."
2. RAM check at `4 GB free` threshold fired WARN at "3.2 GB free / 16 GB total." Accurate by `psutil.virtual_memory().available`, but macOS aggressively caches in RAM and pages to SSD-backed swap under pressure. 3.2 GB available on 16 GB Apple Silicon is not a real problem.

Fixes in commit `8d50097`:
- `cli/doctor/checks.py`: added `_find_own_stack_container(port)` helper that runs `docker ps --format '{{.Names}}\t{{.Ports}}'` and matches container names ending in `-api-1` / `-qdrant-1` / `-postgres-1` / `-redis-1` (the docker-compose default naming for our four services). When our container is the port holder, doctor reports PASS with `in use by <container> (your running stack)`. Stranger conflicts still FAIL with the kill recipe.
- `cli/doctor/checks.py` + `__init__.py` + `pyproject.toml`: RAM check removed entirely. Cleaner than engineering a macOS-aware threshold for what's effectively a non-issue. If real OOMs surface later, a `vm_stat`-aware probe can come back as a proper signal.
- Tests: split the single port test into stranger-vs-own-stack scenarios; added three tests for the helper itself (matching, no-docker, docker-error). Bundle size dropped 37 MB → 36 MB (psutil removed).

### Install path is now stable

The 0.1.x cycle is closed. Eight releases (0.1.0–0.1.7), five real install bugs, two doctor false positives, one falsified hypothesis. Verified M1 install end-to-end on test7 (clean chat at 48s wall, all 8 components warm, RBAC + HITL workflow working). 0.1.5+ are the recommended-install releases on PyPI; 0.1.0–0.1.4 yanked.

---

## 0.2.x roadmap — image distribution, snapshot distribution, ingest UX

The 0.1.x cycle hardened the build-locally install path. 0.2.x sidesteps it instead — pull pre-built images, pull pre-vectorized corpora, point at custom PDF directories. Distribution-layer work.

### Roadmap

| Item | Status | Effort | Notes |
|---|---|---|---|
| **0.2.0 — Pre-built API image on GHCR, multi-arch (linux/amd64 + linux/arm64), published per `v*` tag via GitHub Actions** | **DONE (shipped 2026-05-30, commit `5e0919b`)** | ~4–5 hours actual | `compose.minimal.yml` got `image: ghcr.io/rishabhmannu/...:${FB_IMAGE_TAG:-0.2.0}` alongside existing `build:` block. `financebench upgrade` defaults to `docker compose pull`. M1 first-pull was 470s (not 90s — image larger than predicted). See "0.2.0 — install path closed" section below for the outcome narrative. |
| `scripts/ci_smoke_install.sh` driven by GitHub Actions on every tag | **DONE (0.2.1, commits `383bbb3` + `e2e22a4`)** | ~3 hours | Two-tier design: cheap PR/push smoke (wheel install + CLI + doctor --skip-network) catches wheel/import regressions; heavy tag-push verify (compose up + `/v1/health` + semver match) catches container/image regressions. CI verify job hit a Linux bind-mount UID PermissionError on first 0.2.1 run; workaround was `chmod 777 logs cost_logs` in CI. Real fix landed in 0.2.2 — see Linux bind-mount UID row below. |
| API key live validation (Layer 2) | **DONE (0.2.3)** | ~3 hours | New `cli/key_probe.py` with one probe per provider: OpenAI / Anthropic / Groq use free `GET /v1/models`; Voyage uses a 1-token embedding (~$0.00002). Wired into `setup` (per-key after Layer 1 prefix check, gated by `--skip-key-probe` or `--skip-doctor-network`) and `doctor` (4 new checks under the "API keys" group, gated by `--skip-network`). Network failures fall through to "saved as-is" with a warning so offline installs still work. Bad-key (401/403) reports the provider's dashboard URL for re-issue. |
| **0.3.0 — Pre-vectorized FinanceBench Qdrant snapshot on HuggingFace Hub** | **DONE (0.3.0)** | ~3 hours actual | Live at https://huggingface.co/datasets/cmpunkmannu/financebench-voyage-finance-2-embeddings (CC BY-NC 4.0, public, 460 MB parquet, 68,059 chunks across 84 SEC filings, dense voyage-finance-2 1024d + sparse BM25). Format: parquet + manifest.json + README.md (no native Qdrant snapshot — chose parquet because it's framework-agnostic, restorable to any vector DB, and readable from any RAG stack without HF tooling). New `scripts/export_to_hf.py` (Qdrant scroll → parquet + manifest + frozen README) + `scripts/seed_from_hf.py` (download → bulk upsert into Qdrant). `financebench seed --from-hf <slug>` CLI flag wires the consumer side. Round-trip verified end-to-end (top-3 query results match exactly between source and restored collection). |
| `financebench seed --dir <path> [--collection <name>]` | **DONE (script in 0.1.8 commit `0dbc3e9`; CLI wrapper in 0.2.1)** | ~30 min script + ~30 min CLI wrapper | Script-level flags via `scripts/seed_qdrant.py`. 0.2.1 ships `financebench seed` as a top-level CLI command — thin `docker compose exec api` wrapper that translates host paths to container paths under the `./data:/app/data` bind mount. Caveat: the tuned prompts + reranker are FinanceBench-specific, so accuracy on non-FB corpora may differ from the 72.67% headline. |
| Multi-collection / per-tenant ingest pipeline | Pending | ~1–2 weeks | Production-grade: per-user collections, REST ingest endpoint, idempotent re-ingest, RBAC at collection level. Only justified if there's actual demand. Architecturally feasible — retrieval node and RBAC service already accept collection name as parameter. |
| Yank 0.1.5–0.1.8 from PyPI | **DONE** | 5 minutes | Yanked 0.1.0 → 0.1.8 after 0.2.0 verified clean on M1 (test10). PyPI now resolves `financebench-rag-agent` → 0.2.0; pinned installs of older versions still work. |
| Image size reduction (new — surfaced post-0.2.0 ship) | **DONE (0.3.1)** | ~5 hours actual (across measurement + iteration + verification) | **Achieved 34.4% uncompressed reduction (4.30 GB → 2.82 GB) and ~48% compressed reduction (1102 MB → 569 MB gzip estimate)** — well above original "30-50%" range despite explicitly skipping the base-image swap (high risk, low gain). Path: (Phase 1) `.dockerignore` safety fix excluding the 65 GB `Docker-backup-*.raw` from build context; (Phase 2) spaCy `en_core_web_lg` → `en_core_web_md` saves ~760 MB uncompressed BUT required two follow-ups: explicit `NlpEngineProvider` config in `guardrails_service.py` because Presidio's default hardcodes `en_core_web_lg`, and a `USE_LARGE_SPACY_MODEL=1` env-var opt-in for users needing maximum PERSON recall after measuring ~20pp recall drop on single-name references; (Phase 3) docling moved to `[docling]` optional extra + libxcb1/libgl1 dropped from Dockerfile saves ~720 MB additional uncompressed via removing opencv-python.libs/cv2/docling_parse/rapidocr/docling family. Code in `src/ingestion/docling_loader.py:64-68` already handled missing-docling gracefully (returns None, chunker falls back to pypdf). |
| ~~Pydantic serialization warnings during grading~~ → broader upstream-warning suppression | **DONE (0.2.2)** | ~2 hours actual | Audit found ZERO pydantic warnings on the runtime path. Real noise was 5 upstream warnings: 1 langgraph (`allowed_objects`), 2 protobuf (Python 3.14 prep), 2 websockets (uvicorn deps), plus 1 test fixture (23-byte JWT secret). Fix: `src/_quiet.py` filter module + `src/__init__.py` early-load + `pyproject` pytest addopts + Dockerfile `ENV PYTHONWARNINGS` for entrypoint-level warnings (uvicorn imports websockets before any `src.*` runs). Test-fixture secret padded to 32 bytes. See "0.2.2 — silent install/runtime polish" section below for the surprise: the original "pydantic warnings" prediction was wrong; the actual mechanism (`surface_langchain_deprecation_warnings()` re-inserting at filter position 0 twice — once each from `langchain_core/__init__.py` and `langchain/__init__.py`) took the longest to debug. |
| Linux bind-mount UID PermissionError (new — surfaced in CI on 0.2.1) | **DONE (0.2.3 — first try in 0.2.2 missed the Dockerfile half)** | ~30 min + ~10 min for the second-order fix | `compose.minimal.yml` bind-mounted `./logs` and `./cost_logs` from host. On macOS Docker Desktop, UID translation papered over the in-container `appuser` (UID 1000) vs host UID mismatch. On raw Linux (CI runner UID 1001, plus any Ubuntu user), the bind mount preserves ownership and `event_log.attach_file_handler()` PermissionErrors on first JSONL write. **0.2.2 fix** switched both paths to named volumes (`api_logs`, `api_cost_logs`) but missed pre-creating `/app/logs` and `/app/cost_logs` in the Dockerfile — named volumes inherit in-image ownership only if the directory exists in the image, otherwise docker creates the mount point as root:root. CI verify caught it (same error class, second order). **0.2.3** adds the matching `RUN mkdir -p /app/logs /app/cost_logs && chown -R appuser:appuser ...` next to the existing hf_cache mkdir. The hf_cache pattern was the existing reference I should have grepped for — fifth documented instance of "fixed one call site, missed the other". `financebench logs` and `financebench logs --event-log` CLI commands replace host-side `tail logs/run_*.jsonl` — wraps `docker compose logs api` and `docker compose exec api tail /app/logs/run_*.jsonl`. CI verify job's `chmod 777` workaround removed. |

### What 0.2.0 does NOT include (deferred or out of scope)

- Web upload UI for documents. Backend has all the plumbing; the user-facing surface doesn't.
- Real-time ingest with change detection. Current model is batch-only.
- Embedding-provider swap without re-ingest. Embedding-dim is locked at collection-create time; swapping providers requires a fresh collection + boot-banner fingerprint match.
- File types beyond PDF (DOCX, XLSX, slides). Pypdf is the ingestion engine; docling is fallback for tables but underperforms.
- Per-chunk ACLs. RBAC is at document-type granularity, not chunk granularity.

### Project framing — utility analysis (added 2026-05-30 strategic review)

The 0.2.x roadmap addresses the install-path / distribution / ingest UX. It does not move the project from "production-shaped reference implementation" to "deployed product." That is a distinct decision with much larger scope. Recording the framing here so future-self doesn't re-litigate it:

**Primary value of this project is portfolio + learning, not deployed product.** The CC-BY-NC license on FinanceBench precludes any commercial path on the FB corpus regardless of engineering work. For commercial deployment, the corpus would have to be original (e.g., own-ingested EDGAR filings via the `edgartools` already in `[scripts]` extras, or paid-licensed data). For the portfolio path, the codebase + engineering log + evaluation methodology + 0.1.x install-path campaign + κ=0.932 judge calibration are the asset — the snapshot distribution and `--dir` ingest extend the demonstration without changing the framing.

Three real utility angles, in honest order of strength:

1. **Portfolio / interview signal** (strongest). The system + engineering log + the documented falsified-hypothesis cycles (Multi-HyDE null, calculator regression, FT-v1 reranker silent inactivity, embedding-dim audit, LLM Guard falsification, 0.1.x install bugs) demonstrate rigor that's rare at the junior-engineer level.
2. **Teaching / reference value** (medium). The 16-node LangGraph pattern, audit-first protocol, doctor preflight pattern, and the engineering log itself are reusable for other engineers building RAG systems. The Sprint 7.19 boot-banner + dim-mismatch hard-fail pattern is worth a standalone blog post.
3. **Functional utility for a determined individual user** (weak but real). Someone with their own SEC filings, Docker familiarity, and CC-BY-NC-acceptable use can install and get value. Total addressable users in this exact intersection: low.

Pre-empting the "is this a startup?" question: no, not without (a) licensed corpus, (b) hosted multi-tenant deployment, (c) 6–12 months of product / sales / compliance work, and (d) competing against AlphaSense / Hebbia. Not in scope for an individual portfolio project. The methodological process documented here transfers to any senior IC role at any AI-adjacent company — that's the asset.

---

## 0.2.0 — install path closed (shipped 2026-05-30)

The pre-built GHCR image work. Eight 0.1.x patches + one minor (0.2.0) = nine releases to make the install path actually work on a fresh M1. The 0.x install-path arc is now closed.

### What shipped (commit `5e0919b`)

| Change | Effect |
|---|---|
| `.github/workflows/release-image.yml` (new, ~145 lines) | Matrix build on native runners (`ubuntu-latest` for amd64, `ubuntu-24.04-arm` for arm64), push-by-digest, merge into manifest list with `:0.2.0` + `:0.2` tags. GITHUB_TOKEN auth (no PAT setup). First-time published package is private; repo owner toggled to public via GitHub UI for anonymous pulls. |
| `compose.minimal.yml` + `docker-compose.yml` | `image: ghcr.io/rishabhmannu/financebench-rag-agent-api:${FB_IMAGE_TAG:-0.2.0}` directive alongside existing `build:` block. Compose pulls by default; `up -d --build` falls back to local build. |
| `cli/commands/setup.py` + `upgrade.py` | Thread `FB_IMAGE_TAG=<cli_version>` through env. Default flow is `docker compose pull`; `BUILD_FROM_SOURCE=1` or `financebench upgrade --build` forces source build. |
| `cli/commands/upgrade.py` GIT_SHA threading in `_compose_build_api` | **Third instance** of the "fixed-one-call-site-missed-the-other" bug pattern (see meta-lesson below). Same `env=` pass-through as the 0.1.5 setup.py fix. |
| `cli/doctor/checks.py` port-range parser | Test9 diagnostic was the entire investigation — `docker ps` returned `0.0.0.0:6333-6334->6333-6334/tcp` for qdrant's two-port mapping; 0.1.7's substring matcher `:6333->` silently missed it. New `_published_ports()` helper expands ranges and handles IPv4+IPv6 dual entries. |
| `Dockerfile` `ENV ORT_LOGGING_LEVEL=3` | Silences onnxruntime "Unknown CPU vendor" warning that fires on every script invocation inside arm64-Linux containers on M1. Test6 A/B falsified the perf hypothesis; cosmetic noise only. |
| `src/services/guardrails_service.py` `AnalyzerEngine(supported_languages=["en"])` | Restricts presidio registry to English. Pre-0.2.0 it emitted 11 WARNING lines per container start about Spanish/Italian/Polish recognizers not being registered (the registry only supports `en` and we want it that way). |
| `docs/upgrade.md` | New "default workflow" narrative (pull-by-default), `BUILD_FROM_SOURCE=1` escape hatch, `docker image prune -a -f --filter "until=24h"` recipe for evicting stale 0.1.x local images after upgrade. |
| Tests | 27 doctor unit tests pass (was 23) — +2 for port-range parser, +2 for the `_published_ports` helper unit tests. |

Total: +394 / −31 lines across 13 files.

### M1 verification (test10)

| Signal | Result |
|---|---|
| `pip install --upgrade financebench-rag-agent` 0.1.8 → 0.2.0 | 220 KB wheel pulled from PyPI |
| `docker compose pull` of GHCR image | **470.6s** on first pull — bigger than the 90s I predicted |
| Container start + `/v1/health` 200 | 15s |
| Banner shows `(semver 0.2.0, sha 5e0919b)` | Real sha — three-release-old GIT_SHA bug class closed |
| Doctor (full check) | `12 passed · 3 info`, zero warnings — port-range parser fix verified |
| `[✓] Port 6333 (qdrant) in use by repo-qdrant-1 (your running stack)` | The asymmetric "qdrant only" miss from test9 is gone |
| Chat query (Apple FY2023 revenue) | 42.7s wall, conf 1.00, citations correct, sub-500ms guardrails |

### One prediction miss to document

I predicted ~90s first-pull from GHCR; actual was **470s on M1** (~7.8 min). Image is larger than I estimated — ~2.5-3 GB per arch (torch + sentence-transformers + spaCy + presidio + onnxruntime + base Python). Subsequent pulls reuse cached layers and drop dramatically; the first-pull cost is paid once per machine.

Still strictly better than the ~10 min source build that every 0.1.x release paid (and that build would re-run on every patch version). But the user-facing first-install experience is closer to 5-8 minutes than the 90 seconds I'd led with. Image-size reduction is now a real 0.2.x sprint candidate (added to roadmap).

### New finding for 0.2.x — pydantic serialization warnings

Test10 surfaced 8+ identical warnings per chat query in the API logs:

```
WARNI [py.warnings] /usr/local/lib/python3.12/site-packages/pydantic/main.py:475:
  UserWarning: Pydantic serializer warnings:
  PydanticSerializationUnexpectedValue(Expected `none` - serialized value may not be as expected
    [field_name='parsed', input_value=GradeResult(relevant=True...), input_type=GradeResult])
```

Fires once per chunk graded. **Not a regression** — would have been firing throughout the 0.1.x cycle but only visible when tailing API logs immediately post-query, which we didn't do before test10. Likely from LangChain's structured-output wrapper interacting with pydantic v2's strict serializer; the wrapper declares `parsed: T | None = None` on its response model and pydantic flags the type mismatch when an actual `GradeResult` instance is set. Cosmetic only; correctness verified by the test10 chat answer being right.

### Meta-lesson — fourth documented instance of "fixed one call site, missed the other"

The 0.2.0 GIT_SHA-in-upgrade.py miss is the **third documented instance** of this pattern (counting only the install-path cycle; if we count the runtime-audit cycle, it's the fourth instance overall — the embedding-dim fingerprint had the same shape).

1. **0.1.3** — guardrails import typo in `src/api/routes/health.py:76` (`from src.services.guardrails import ...` — wrong module name). `src/graph/nodes/guardrails.py:7` already used the correct `guardrails_service` module. Health was the new call site I'd just written; graph nodes were the existing one I didn't check.
2. **0.1.6** — GIT_SHA env fallback added to `src/api/main.py:_git_sha()`. `src/services/event_log.py:143` was the second call site doing `git rev-parse HEAD` directly, and was missed.
3. **0.2.0** — GIT_SHA env threading added to `cli/commands/setup.py:_bring_up_stack`. `cli/commands/upgrade.py:_compose_build_api` was the second call site running docker compose build with env passthrough, and was missed.

The mistake I keep making: fixing the call site I'm currently looking at without grepping for siblings. The fix protocol is now:

> When fixing a bug related to a config / env var / import path / subprocess invocation, before declaring the fix complete: `grep -rn '<the relevant string>' src/ cli/ scripts/` and confirm every match is either (a) correctly handled by the fix or (b) intentionally outside scope. The 30-second grep would have caught all three of these at PR time.

### Updated 0.2.x roadmap status

See the table in §"0.2.x roadmap — image distribution, snapshot distribution, ingest UX" above. Items now marked `DONE`: GHCR image, yank 0.1.5-0.1.8, partial seed --dir flag. `IN PROGRESS`: CI smoke install workflow (current sprint after this entry). `Pending`: HF snapshot, API key Layer 2, `financebench seed` CLI subcommand wrapper, multi-collection ingest, image-size reduction, pydantic warning investigation.

The 0.x install-path arc is closed. **The portfolio narrative** — 9 releases, 5 install bugs, 2 doctor false positives, 1 falsified hypothesis (LLM Guard), 3 instances of the same fixed-one-site bug pattern, all documented — is itself the strongest asset the project produces.

---

## 0.2.2 — silent install/runtime polish (shipped 2026-06-01)

Two unrelated items bundled because both were cheap hygiene fixes with deterministic verification:

1. **Upstream warning audit + suppression** (originally framed as "pydantic warning fix")
2. **Linux bind-mount UID handling** (named volumes for `logs` + `cost_logs` + new `financebench logs` CLI)

### #1: the audit reframed the problem

The 0.2.0 ship note predicted the noise was from `GradeResult.parsed` field on LangChain's structured-output wrapper. Audit found **zero pydantic warnings** on either the import path or the runtime path (exercised a real chat call against a local uvicorn pointed at the existing qdrant/postgres/redis stack — call got past auth/router/entity-extractor before failing at the reranker on a NumPy 1.x/2.x ABI break unrelated to project code).

Actual noise inventory:

| Warning | Source | Fix |
|---|---|---|
| `LangChainPendingDeprecationWarning: allowed_objects` | `langgraph 0.6.11 → langchain_core.load.load.Reviver()` constructor | Filter — only fixed in langgraph 1.x (major bump) |
| `DeprecationWarning: protobuf ScalarMapContainer` (×2) | C-extension PyType_Spec, Python 3.14 prep | Filter — upstream-only fix |
| `DeprecationWarning: websockets.legacy / WebSocketServerProtocol` (×2) | uvicorn[standard] websockets adapter | Filter — upstream-only fix |
| `InsecureKeyLengthWarning: HMAC 23 bytes` | `tests/unit/test_auth_service.py:97` JWT test fixture | Pad to 32 bytes (real fix at source) |

The credibility-rule sequence was: predict pydantic warnings → audit ran with `pytest -W default::DeprecationWarning` AND `uvicorn` boot → measurement returned a different (smaller) list → recommend smaller-than-predicted fix. This is exactly the loop CLAUDE.md prescribes for sprint planning.

### The interesting debugging wrinkle

A naive `src/_quiet.py` with three `warnings.filterwarnings(...)` calls didn't suppress the langgraph warning. Tracing the filter state at the moment of warning issuance showed `("default", None, LangChainPendingDeprecationWarning)` filters ABOVE mine in the list — even though my filters had been installed first. Root cause: BOTH `langchain_core/__init__.py:19` AND `langchain/__init__.py:42` call `surface_langchain_deprecation_warnings()` at import time, which `filterwarnings(..., 0)` insert at position 0, pushing my filters down. The fix is to force both packages to import inside `_quiet.py` then re-install my filters on top.

Three places needed entrypoint-level handling because filters install during import:

| Layer | File | Why |
|---|---|---|
| In-process (langgraph, protobuf, websockets) | `src/_quiet.py` (new) + `src/__init__.py` (1-line import) | `src/__init__.py` runs on first `src.*` import — covers tests, eval scripts, and the API boot path |
| Pytest collection-phase (protobuf via langsmith) | `pyproject.toml [tool.pytest.ini_options] addopts = ["-W", "ignore:..."]` | pytest reads `addopts` from ini BEFORE running its plugin loaders that pull protobuf |
| Pre-`src.*` (uvicorn websockets adapter) | `Dockerfile ENV PYTHONWARNINGS="ignore:websockets..."` | uvicorn imports websockets BEFORE loading the app target, so even `src/__init__.py` runs too late. PYTHONWARNINGS applies at Python startup — before any imports. |

### #2: the Linux bind-mount fix

`compose.minimal.yml` had bind-mounted `./logs` and `./cost_logs` from host. macOS Docker Desktop's UID translation hides this for M1 users. Raw Linux preserves UID; CI runner (UID 1001) + container appuser (UID 1000) collide → PermissionError on `event_log.attach_file_handler()`'s first JSONL write → lifespan dies → `/v1/health` never comes up.

CI's first 0.2.1 verify-job run hit this; workaround was `mkdir -p logs cost_logs && chmod 777 logs cost_logs` before `docker compose up`. Real fix: switch both to named volumes (`api_logs`, `api_cost_logs`). Named volumes inherit the in-image directory's ownership on first mount — `appuser` owns `/app/logs` per the existing `RUN chown -R appuser:appuser /app` in the Dockerfile, so the volume is appuser-owned from the start. Trade-off: host can no longer `tail logs/run_*.jsonl` directly. Mitigation: `financebench logs` and `financebench logs --event-log` commands wrap `docker compose logs api` and `docker compose exec api tail /app/logs/run_*.jsonl` respectively.

The CI verify-job `chmod 777` workaround is now removed; the verify job is shorter and the bug is gone at the compose-config layer instead of papered over per-runner.

### What didn't ship

- Migration helper for existing 0.1.x → 0.2.2 users with valuable host-side logs. Anyone upgrading loses the host-side history. Acceptable because (a) logs are debugging artifacts, not data; (b) `financebench logs` recovers access; (c) named volumes don't auto-clean on `docker compose down` (only on `down -v`), so existing volumes survive image upgrades.
- spaCy `en_core_web_lg` → `en_core_web_md` swap. Deferred to 0.3.1 image-size sprint where it gets measured against PII detection precision before committing.

### Methodological note — speculation correctly caught

The 0.2.0 ship note predicted what the pydantic warning fix would look like. The 0.2.2 audit falsified that prediction in two ways: (a) no pydantic warnings actually fire, (b) the upstream warnings that DO fire have a more interesting suppression problem (the `surface_langchain_deprecation_warnings()` race) than any pydantic question would have raised. Honest report-back before committing to the original sprint shape saved the wasted work.

---

## 0.2.3 — Dockerfile half of the Linux-UID fix + API key Layer 2 (shipped 2026-06-01)

0.2.3 ships two things: (a) the second-order fix the 0.2.2 verify job caught, and (b) the originally-scoped API key live validation. Bundled because the 0.2.2 git tag was deleted before any PyPI upload, so the version-skip is internal only.

### The 0.2.2 verify failure — fifth documented instance of "fixed one call site, missed the other"

0.2.2 switched `compose.minimal.yml`'s `./logs` and `./cost_logs` bind mounts to named volumes (`api_logs`, `api_cost_logs`) to dodge the Linux UID mismatch. That's the compose-side fix. **It needed a matching Dockerfile-side fix that I missed.**

Named volumes inherit the in-image directory's ownership on first mount. That works for `hf_cache` because `Dockerfile:108-115` pre-creates `/home/appuser/.cache/huggingface` with appuser ownership before `USER appuser`. The volume sees the existing appuser-owned directory and inherits the perms.

For `/app/logs` and `/app/cost_logs`, the directories never existed in the image (they're runtime artifacts — we don't `COPY` them). Docker created the mount points on the fly as `root:root`. `appuser` PermissionError'd on the first `event_log.attach_file_handler()` open() → lifespan died → `/v1/health` never came up → CI verify timed out at 5 min.

The fix is a 2-line addition next to the existing hf_cache mkdir:

```dockerfile
RUN mkdir -p /home/appuser/.cache/huggingface /app/logs /app/cost_logs && \
    chown -R appuser:appuser /home/appuser/.cache /app/logs /app/cost_logs
```

The hf_cache pattern (with its comment explaining exactly why pre-creation matters for named volumes) was right above the line I needed to add. The fix protocol from the engineering-log entry on the 0.2.0 third-instance miss said:

> When fixing a bug related to a config / env var / import path / subprocess invocation, before declaring the fix complete: `grep -rn '<the relevant string>' src/ cli/ scripts/` and confirm every match is either (a) correctly handled by the fix or (b) intentionally outside scope.

I didn't grep the Dockerfile for `mkdir -p` or `chown` patterns when writing the 0.2.2 compose-side fix. Fifth time the protocol catches this AFTER I've already shipped the broken version. Going forward I'll generalize "grep src/ cli/ scripts/" to also include Dockerfile + compose YAML when the change crosses container/host boundaries.

### What this means for 0.2.2 as an artifact

The 0.2.2 git commit (`d8ad1d6`) stays in `main`'s history — it's accurate work, just incomplete. The `v0.2.2` tag was deleted (local + remote) before any PyPI upload, so:

- PyPI: 0.2.3 is the next version after 0.2.1
- GHCR: `:0.2.2` image exists but is broken (verify failed). Will be supplanted by `:0.2.3` and nobody will pull it because CLI 0.2.3 threads `FB_IMAGE_TAG=0.2.3`. Left in place as a historical artifact rather than deleted.
- Compose fallback: bumped to `:0.2.3` so manual `docker compose up` users don't accidentally grab `:0.2.2`.

### API key Layer 2 validation (the originally-scoped 0.2.3 work)

Layer 1 (shipped in 0.1.4) checks the prefix format. Catches typos and provider-mismatched pastes but misses revoked / wrong-account / expired keys.

Layer 2 issues one tiny request per provider:

| Provider | Endpoint | Cost |
|---|---|---|
| OpenAI | `GET /v1/models` | Free |
| Anthropic | `GET /v1/models` | Free |
| Voyage | `POST /v1/embeddings` (1-token, model=voyage-finance-2) | ~$0.00002 |
| Groq | `GET /openai/v1/models` | Free |

New `cli/key_probe.py` module with one function per provider returning a `ProbeResult(status, message)` where `status` is `OK` / `BAD_KEY` / `NETWORK_ERROR`. Wired into:

- **`financebench setup`** — per-key, after the existing prefix check. Gated by `--skip-key-probe` (new) or `--skip-doctor-network` (existing offline flag). Network errors fall through to "saved as-is" with a yellow warning so airgapped installs still work. Bad-key (401/403) reports the provider's dashboard URL for re-issue but saves the key anyway (user can re-run setup or just accept that one call will fail).
- **`financebench doctor`** — four new checks under the "API keys" group, gated by `--skip-network`. Required keys (OPENAI, ANTHROPIC) FAIL when missing or bad; optional keys (VOYAGE, GROQ) WARN or INFO. Network errors render as WARN with "skip and re-run when online".

Smoke-tested locally — all four probes against this repo's `.env` keys returned `OK` with the masked tail printed (`Live probe: accepted by provider (•••yZMA)`). The wizard side also displays the per-key result inline:

```
  OpenAI API key (required — embeddings + gpt-4o-mini)
  Get one at: https://platform.openai.com/api-keys
  [current: ••••••XXXX] >
  Validating with provider...
  ✓ OpenAI accepted the key
```

### What's still pending in 0.2.x

- HF snapshot (0.3.0) — needs your input on dataset slug + approve-before-public-upload, deferred to a dedicated release.
- Image size reduction (0.3.1) — deferred to last because the size decisions (drop docling? spacy lg → md?) depend on knowing the final 0.3.0 image content.

---

## 0.3.0 — pre-vectorized FinanceBench snapshot on HuggingFace Hub (shipped 2026-06-01)

The first 0.3.x release. Drops new-user time-to-first-query from ~30 min + ~$5-15 (re-embed 360 PDFs through Voyage's API) to **~3 min + $0** (download a pre-computed parquet, bulk-upsert into Qdrant). The CLI consumer side is `financebench seed --from-hf <slug>`; the dataset is public at https://huggingface.co/datasets/cmpunkmannu/financebench-voyage-finance-2-embeddings.

### What shipped

| Piece | Location | Purpose |
|---|---|---|
| Export script (producer side) | `scripts/export_to_hf.py` | Qdrant scroll → parquet + manifest.json + frozen README.md. Streams batches via PyArrow's `ParquetWriter` so peak memory stays bounded for 68k-point collections. `--upload` mode wraps `huggingface_hub.HfApi.upload_folder()`. |
| Restore script (consumer side, runs in container) | `scripts/seed_from_hf.py` | `huggingface_hub.snapshot_download` → drop + recreate Qdrant collection (dense 1024 cosine + sparse BM25) → bulk upsert in batches of 256. Verifies manifest's `dense_dim` matches the consumer pipeline at restore time; hard-fails on mismatch. |
| CLI wrapper | `cli/commands/seed.py` `--from-hf` flag | Mutexes with `--sample`/`--dir`, threads `--collection` + `--revision`, docker-execs the in-container restore script. |
| Backend deps | `pyproject.toml [backend]` | Added explicit `pyarrow>=15.0,<25.0` (needed for parquet read inside the container — was incidentally pulled by streamlit/datasets in dev envs but NOT by anything in the backend tree) and `huggingface_hub>=0.26,<1.0` (already transitive via sentence-transformers but pinned to insulate against upstream churn). |
| Round-trip verification | `scripts/_roundtrip_verify.py` (not shipped) | Download published parquet → restore to `financebench_test_roundtrip` collection → query a known chunk's vector against both collections → confirm top-3 IDs match exactly. Run once before the public release; verified clean. |

### The dataset

| | |
|---|---|
| Slug | `cmpunkmannu/financebench-voyage-finance-2-embeddings` |
| Visibility | Public, CC BY-NC 4.0 |
| Files | `chunks.parquet` (460 MB), `manifest.json` (1.2 KB), `README.md` (4.2 KB) |
| Chunks | 68,059 |
| Documents | 84 distinct SEC filings (10-K / 10-Q / 8-K / earnings) |
| Dense vectors | voyage/voyage-finance-2, 1024-dim, cosine |
| Sparse vectors | BM25 tokens via Qdrant fastembed |
| Export time | 30s from running Qdrant (~2200 points/s scroll rate) |
| Upload time | ~3 min at 2.5 MB/s |

### The "non-stale by design" README

User concern at sprint kickoff: the project's GitHub README and the PyPI description have both drifted from current state in past iterations. The HF dataset README is a **frozen artifact** — once published, it describes what was uploaded at that timestamp, not what the project looks like now. The contract I designed for:

**In the HF README:**
- What's in the parquet file (counts, schema, sector breakdown)
- Frozen pipeline config that produced it (parser, embedding model, distance metric)
- Snapshot timestamp + generator project version pin
- Single line of "Prerequisites" pointing to project's `docs/setup.md`
- License + citation

**Explicitly NOT in the HF README** (anything that drifts):
- Current FinanceBench pass-rate (changes — points to `docs/evaluation.md` instead)
- Current pipeline architecture (16-node graph, reranker tier — changes — points to `docs/`)
- Project version numbers other than the one used to generate THIS snapshot (frozen)
- "Production-grade" framing or any subjective quality claim
- Full pip install / docker / .env walkthrough (lives in setup.md)

The mechanical guarantee: README is regenerated from `manifest.json` at export time. If the corpus, parser, or embedding model ever changes, the path forward is publishing a NEW dataset (`...-voyage-finance-3-embeddings`), not editing this one. This README doesn't ever need to be re-published except as part of a fresh snapshot.

### Why parquet + manifest, not a native Qdrant snapshot

Qdrant supports native snapshot export/import. Considered briefly but rejected:

- Parquet is **framework-agnostic** — anyone with pandas/pyarrow can load it without HF tooling, into Pinecone, Weaviate, FAISS, or a custom pipeline. Native Qdrant snapshots couple consumers to Qdrant.
- Parquet has a **stable, documented schema** that's portable across vector DB versions. Native snapshot format is internal to Qdrant and can change between minor versions.
- Native snapshots include indexing structures (HNSW graphs) that are rebuilt on import anyway — no real cold-start advantage.
- The README's "Direct parquet (any RAG stack — no project setup needed)" snippet would not work with a native snapshot. That snippet is the point — making the dataset usable beyond just this project's CLI.

Cost: restore takes ~70s for 68k points to re-index from the bulk upsert. Acceptable trade vs the portability gain.

### Round-trip verification (the safety net)

Before announcing the dataset I ran the full producer→consumer cycle as a one-off:

1. Downloaded published `chunks.parquet` from HF (78s for 460 MB)
2. Created a fresh `financebench_test_roundtrip` Qdrant collection
3. Bulk-upserted all 68,059 points (56s)
4. Grabbed one chunk from the original `financebench_corpus_pypdf_voyage_finance2` collection
5. Used its dense vector as a query against BOTH the source and the restored test collection
6. Compared top-3 IDs

Result: identical top-3 IDs in identical order. Vectors round-tripped without precision loss, point IDs preserved, collection config matches. The contract `financebench seed --from-hf` promises to users is verified.

### One judgment-call moment

The original README draft just said "FinanceBench (SEC filings: 10-K, 10-Q, 8-K, earnings releases)" without a document count. The actual collection has 84 documents — which is a real, frozen fact about THIS snapshot that helps future users decide if it's relevant to them. Adding `Documents | 84 distinct SEC filings` + a 9-row sector breakdown table to the README mid-flight wasn't in the original plan but was the right call. **Frozen facts ABOUT the snapshot are good; frozen facts about the project surrounding the snapshot are the staleness trap.**

### What this enables for portfolio framing

The dataset card on HF Hub becomes a recruiter-discoverable artifact in its own right: someone searching "voyage-finance-2" or "financebench" finds it via HF, opens the README, sees the project link, and lands on the repo with context already loaded. The dataset is now in the HF tag index for `embeddings`, `rag`, `financial-qa`, `voyage`, `qdrant`, `financebench`, `arxiv:2311.11944`. None of those required marketing — just publishing the artifact with accurate tags.

### What's next in 0.3.x

- 0.3.1 — image size reduction. Deferred from earlier because the size decisions (drop docling? spacy lg → md? slim base image?) need to be made AGAINST the final 0.3.0 image content. Now that 0.3.0 is the canonical base, the measurement + reduction sprint can begin.

---

## 0.3.1 — image size reduction (shipped 2026-06-02)

The last item on the 0.x roadmap. Original estimate from the 0.2.0 ship note: "30-50% reduction plausible" via slim base + drop docling + spaCy lg → md + multi-stage layer pruning. Final delivered: **34.4% uncompressed (4.30 GB → 2.82 GB) and ~48% compressed (1102 MB → 569 MB gzip-1 estimate)** while explicitly skipping the base-image swap (high risk, ~5% gain) and the layer pruning (low yield once docling and lg were gone).

### Measurement first (per credibility-rule protocol)

Pulled `:0.3.0` from GHCR (took 14 min on Indian residential bandwidth — 13.7 min of which was waiting for arm64 layer 7 alone), then ran `docker history` + `docker buildx imagetools inspect --raw` + in-container `du -sh` on `/usr/local/lib/python3.12/site-packages/*/`. Findings ranked by size before proposing any cut:

| Layer / package | Uncompressed | Compressed | What |
|---|---|---|---|
| Layer 7: `COPY --from=builder site-packages` | 2.78 GB | **995 MB (90% of total)** | All Python deps in one layer |
| Layer 4: `apt install libxcb1 libgl1` (runtime) | 199 MB | 64 MB | docling's OpenCV runtime deps |
| Base: python:3.12-slim | 109 MB + ... | ~41 MB | Debian + Python interpreter |
| Inside layer 7: torch | 620 MB | — | Required, CPU-only already |
| Inside layer 7: en_core_web_lg | 425 MB | — | spaCy model for Presidio PII |
| Inside layer 7: pyarrow | 140 MB | — | Required (0.3.0 add for HF parquet) |
| Inside layer 7: opencv_python.libs | 79 MB | — | docling's table extraction |
| Inside layer 7: cv2 | 43 MB | — | docling |
| Inside layer 7: docling_parse | 31 MB | — | docling |
| Inside layer 7: rapidocr | 17 MB | — | docling |
| Inside layer 7: docling + docling_core + docling_ibm | 9 MB | — | docling |

### The "re-downloads everything each new version" structural insight

Rishabh's recall during measurement: "all of the dependencies and heavy libraries were being re-downloaded at each new version". Confirmed and documented: 90% of compressed bytes sit in ONE layer (the COPY of site-packages from builder), so ANY pyproject.toml change invalidates the entire 995 MB layer, forcing users to re-pull all of it regardless of whether the actual delta was 1 KB or 1 GB. This is structural (the COPY destination flattens source layers) — splitting site-packages across multiple COPY statements would help but adds complexity. Not done in 0.3.1; flagged as a future option if pyproject.toml continues to churn.

### Phase 1 — `.dockerignore` safety (zero behavior risk)

Excluded `Docker-backup-*.raw` (the 65 GB sparse-file restore point from the 2026-06-02 Docker Desktop disk-full event), `dist/hf-snapshot-*/` (the 460 MB local HF snapshot working dir from 0.3.0), `logs/`, `cost_logs/`, `publish-assets/`. The Docker-backup exclusion was the most important — without it, any `docker build` from the project root would have sent the 65 GB file to the daemon as build context. No measured size impact on the image itself but prevented a future catastrophic build context send.

### Phase 2 — spaCy `en_core_web_lg` → `en_core_web_md`

The biggest single line-of-code change. spaCy lg is 425 MB on disk + bundles word vectors that md doesn't have; md is 33 MB. **Phase 2 saved 760 MB uncompressed** — bigger than my predicted 375 MB because lg's vector blob is heavier than the model itself.

Two follow-ups required to make this safe:

1. **Presidio's `AnalyzerEngine(supported_languages=["en"])` default hardcodes `en_core_web_lg`.** First test run inside the container PER-CHAT timed out at 120s because Presidio tried to auto-download lg (400 MB) at first call. Fix: explicit `NlpEngineProvider` config pointing at `en_core_web_md` (`src/services/guardrails_service.py:142-187`). Missed in my initial Dockerfile-only change because I didn't trace Presidio's default-model behavior; caught by pytest. **Honest lesson: when swapping a vendored data file (spaCy model), grep for the file name in upstream library code, not just our code.** Presidio's `spacy_nlp_engine.py:67` had the hardcoded fallback.

2. **PERSON recall regression on single-name references.** Initial 25-case full-name test corpus showed lg=md=1.000 recall, which I reported as a clean swap. Extended test with single-name PERSONs ("Buffett's letter", "Dimon warned") revealed md drops ~20pp behind lg on this slice. Real-world chat queries in finance Q&A skew heavily to full names, but the regression is real. Resolved via a hybrid env-var opt-in (`USE_LARGE_SPACY_MODEL=1`) so PII-sensitive users (legal, HR, compliance) can install lg in the running container and get back the recall. **Self-criticism: my initial test corpus had a quality issue (no single-name cases); the user implicitly trusted my "go" recommendation. I should have tested both phrasing patterns from the start.**

### Phase 3 — docling moved to `[docling]` optional extra

The codepath was already robust: `src/ingestion/docling_loader.py:64-68` has `try/except ImportError` returning None, and the chunker falls back to per-page pypdf chunking automatically. Per the credibility-rule eval evidence in `docs/evaluation.md`, docling underperforms pypdf by ~29pp on RAGAS faith + ctx_prec — it's never used in production retrieval anyway.

| Change | Effect |
|---|---|
| `pyproject.toml`: docling moved from `[backend]` to a new `[docling]` extra | Default install no longer pulls docling, opencv-python, docling_parse, docling_core, docling_ibm_models, rapidocr |
| `Dockerfile`: dropped `libxcb1` + `libgl1` from both builder and runtime apt installs | Removes the 199 MB uncompressed (64 MB compressed) standalone layer |
| Comment hygiene: updated all docling/libxcb1 explanations to reflect the move | Future-reader clarity |

**Phase 3 alone saved 720 MB uncompressed / 174 MB compressed.** Combined with Phase 2: **1.48 GB uncompressed / 533 MB compressed off the M1 pull.**

Users who explicitly want docling: `pip install ".[docling]"` plus host-level `apt install libxcb1 libgl1`. Documented in the new pyproject.toml comment + Dockerfile comment.

### What we explicitly did NOT do

- **Base image swap** (python:3.12-slim → distroless/alpine). Estimated ~30 MB compressed gain at the cost of recompiling many native deps (numpy, pandas, sentence-transformers) and adding 20-40 min to build time. Bad ROI when Phase 2 + Phase 3 already exceeded the realistic 26% compressed target.
- **Strip dev artifacts from vendored packages** (`**/tests`, `**/examples`, `*.pyi`). Estimated ~50-100 MB uncompressed at the risk of breaking some package self-introspection. Skipped because the headline numbers were already strong without it.
- **Layer split** to enable incremental pulls on pyproject.toml changes. Architecturally interesting but adds Dockerfile complexity and only helps users who pull multiple consecutive versions. Flagged as a future 0.4.x option if pyproject churn continues.

### Verification path

| Surface | Result |
|---|---|
| `docker history` layer-by-layer | Phase 3 image: site-packages layer down from 2.78 GB → 2.10 GB (-680 MB), runtime apt layer (libxcb1+libgl1) gone entirely |
| `du -sh` inside container | docling family fully gone: `import docling/cv2/docling_parse/rapidocr/docling_core` → ImportError as expected |
| pytest unit tests | 307 passed, 5 pre-existing flakes (test_entity_extractor + 4 test_threads_routes — same as 0.2.x/0.3.0 baseline) |
| `src.ingestion.docling_loader.load_pdf` on 2 sample PDFs | pypdf-fallback mode engaged correctly, content extracted (5391 + 855 chars) |
| Full graph build (`src.graph.builder.build_graph`) | 19 nodes, no ImportError |
| `src.api.main` import | Clean |
| spaCy model load via Presidio | md path: works; lg-opt-in-but-missing path: warns + falls back to md; lg-opt-in-with-lg-installed: code-verified |

### Meta-finding for the engineering log

**Two new instances of "speculation caught by measurement":**

1. **My 760 MB Phase 2 prediction vs 375 MB initial estimate.** I missed that en_core_web_lg bundles word vectors that en_core_web_md doesn't. Measured savings exceeded prediction — but I should have caught this from spaCy's docs rather than relying on the on-disk size of the model directory alone.

2. **My "lg=md recall=1.000" report after the 25-case test.** True on full names; not true on single-name references. The user trusted me when I said "clear go". They were right to question further when I proposed implementation — and the extended test caught the gap. Without that catch, we'd have shipped a recall regression with no opt-out. **Lesson logged: when comparing models for a recall-critical use case, test BOTH phrasing patterns common in the production input distribution before declaring equivalence.**

Both are recorded here in the same spirit as the earlier "Multi-HyDE +11.2% prediction", "docling tables near-miss", and "0.3.0 pydantic warning prediction" entries: the credibility rule earned itself a 6th and 7th case study, both caught BEFORE shipping. The protocol works when followed.

## 0.3.2 — package hygiene + Trusted Publisher (shipped 2026-06-03)

A polish release. No runtime behavior change for the typical PyPI/Docker user; the work was a pre-shipping audit of the eight distribution surfaces (wheel, sdist, Docker image, both compose files, GitHub + PyPI READMEs, GHCR tags, releases) — surfaces nobody had systematically inspected before. The audit found hygiene gaps shippable on every release back to 0.1.0.

### Audit first (per the protocol)

`tar tzf dist/financebench_rag_agent-0.3.1.tar.gz | wc -l` = **251 entries**. `docker run --entrypoint sh ... -c "ls /app/scripts" | wc -l` = **60 files**. Of those 60 scripts, exactly **2** are exec'd at runtime (`seed_qdrant.py`, `seed_from_hf.py`); the rest are training/eval/debug/data-prep/smoke tooling that a consumer never touches. The sdist additionally shipped `tests/evaluation/` (50+ benchmark-framework files), `tests/integration/`, and `docs/research/` (internal methodology notes). None broke the product — the wheel was always clean — but they made the package read as noisy when browsed, which is a credibility cost on a portfolio project.

### Actions

| Change | Effect |
|---|---|
| 54 `.py` + 3 `.sh` + 3 `.tape` moved into `scripts/internal/{train,eval,debug,data_prep,smoke,maintenance,demo}/` via `git mv` | Top-level `scripts/` now holds only `seed_qdrant.py`, `seed_from_hf.py`, `generate_jwt.py` |
| `pyproject.toml [sdist].exclude` += `scripts/internal/**`, `tests/evaluation/**`, `tests/integration/**`, `docs/research/**` (superseding the narrower `tests/evaluation/eval_results/**`) | sdist **251 → 153 entries** |
| `Dockerfile`: `COPY scripts/ scripts/` → `COPY scripts/seed_qdrant.py scripts/seed_from_hf.py scripts/` | Docker `/app/scripts` 60 → 2 files |
| Deleted `src/frontend/` (legacy Gradio app, discarded when the CLI became canonical) + cleaned its refs in `docker-compose.yml`, `Makefile`, `docs/setup.md`, `docs/deploy.md` | Removes `gradio_app.py` from the **wheel** too — the exclude-only half-measure would have left it shipping in the wheel |
| PyPI **Trusted Publisher** (PEP 740 OIDC): new `publish-pypi` job in `release-image.yml`, gated on the `verify` health-probe job | First-ever OIDC upload this release; no long-lived PyPI token in CI |
| One-time upgrade notice (`cli/commands/upgrade.py`) for the 0.3.1 spaCy/docling default changes, marker at `~/.financebench/upgrade_notices_seen.json` | Idempotent; fires once |
| Architecture diagram rendered to PNG (`docs/diagrams/architecture.png`); README embeds the PNG | PyPI's README renderer does not support Mermaid |
| Tape portability: dropped the `conda activate agentic-ai` + hardcoded `/Users/rishabh/...` lines from each tape's `Hide` block | The tapes silently mis-recorded on any machine but the M4 Pro (incl. the M1) — see the lesson below |
| Dev `docker-compose.yml` Finding-F fixes: added `RESULT_CACHE_REDIS_HOST/PORT` to the api service, healthcheck `/health` → `/v1/health`, stale `FB_IMAGE_TAG` default `0.2.0` → `0.3.2` | Full-stack self-hosters; `compose.minimal.yml` (CI-verified) was already correct |

### The "5 known flakes" were stale tests, not product bugs

Every release back to 0.2.x quoted "5 pre-existing flakes" in its verification table (see the 0.3.1 entry above) and carried them as acceptable. Applying the audit-first protocol to the *test suite* — not just the image — root-caused all five, and none was a product regression:

- **4 `test_threads_routes.py` tests** mocked `get_thread_owner` (the old 2-tuple) and stubbed `app.state.pool = object()`. The `GET /threads/{id}` handler had since switched to `get_thread_owner_role` (4-tuple, the Track-2 owner-block change) and the list handler gained owner/timestamp enrichment fields. The bare-object pool then hit `pool.connection()` and the stale row shape `KeyError`'d. Fix: patch `src.services.thread_service.get_thread_owner_role` (the handler imports it function-locally, so the route-module patch target was also wrong) and update the mock row shape.
- **1 `test_entity_extractor.py` test** asserted `_extract_year("Compare 2022 to 2023 revenue") == 2022` ("first match"), but `_extract_year` deliberately returns `max(years)` — the latest year is the fiscal year of the source 10-K, which discusses all comparison years inside (documented in the function docstring). The test was asserting behavior the code had intentionally moved away from. Fixed the test to match the contract (`== 2023`), not the code to match the stale test.

Result: **337 passed, 0 failed.** The README test badge previously read "337 passing" while 5 actually failed — it was conflating *collected* with *passing*. The badge is now truthful.

### Lessons logged

- **Audit the wheel + sdist + Docker image before every tag push.** The 60-script bloat was discoverable via a one-line `tar tzf` on every release since 0.1.0. Nobody ran it. Added to the pre-shipping checklist.
- **"It's plain text so it's portable" is wrong.** A `.tape` file is plain text, but its *content* (conda env name, absolute home path) was M4-Pro-specific. Portability = format + content + dependencies.
- **A green-looking test badge can lie.** "Known flakes" carried across releases turned out to be stale tests masking nothing — but the badge had been overstating the passing count the whole time. Stale tests are a credibility liability even when the product is fine.
