# Engineering Log

This is the condensed engineering narrative behind the project — the things that aren't obvious from reading the code. It's written for someone who wants to understand *how* the system got to **47.3% pass rate on FinanceBench** and **+16.6pp across four sprints**, not just *what* the final state looks like.

The full source-of-truth lives in commit messages. This document picks out the non-obvious findings, the failed interventions, and the methodology decisions that informed them.

---

## TL;DR

Across four sprints (7.6 → 7.9), pass rate on the 150-Q FinanceBench benchmark moved **30.7% → 47.3% (+16.6pp)**. Per-eval cost dropped **46% ($9.70 → $5.28)**. Refusal rate halved (14.0% → 7.3%). The multi-hop question slice — stuck at 4/13 across three retrieval interventions — finally moved to 6/13 after a LoRA-fine-tuned reranker on FinanceBench correctness labels.

**Six interventions tested. Three shipped. Three rolled back behind feature flags with the failure mechanism documented.** The methodology caught the failures cleanly and preserved the wins.

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

## Roadmap — Sprint 7.10: eval-quality push

**State of FinanceBench leaderboard (2026)**: Mafin 2.5 + PageIndex reports 98.7%, DANA reports 94.3%, iterative-reasoning-on-fine-tuned-RAG (Nguyen et al. 2024) reports 85%. Original FinanceBench paper baselines were 38–43%. Our 44–47% sits credibly above the published baselines but well below current SOTA — the gap isn't incremental tuning, it's architectural.

[The Hidden Cost of 98% (Tao An)](https://tao-hpu.medium.com/the-hidden-cost-of-98-accuracy-a-practical-guide-to-rag-architecture-selection-6883adc5289c) documents that PageIndex publishes no latency/cost numbers and that per-query LLM reasoning makes it "slower and pricier" — so 98% isn't the right target for our budget. The realistic ceiling on our architecture (selective agentic + LoRA reranker + Voyage embeddings) before requiring a structural rewrite is probably the 55–70% range.

Three concrete levers ranked by expected ROI, **committed in order until quality target is achieved**:

| # | Lever | Claimed gain | Effort | Source |
|---|---|---|---|---|
| **7.10a** | **Multi-HyDE** — generate 3–5 hypothetical answers per query, search with each, dedupe top-K | **+11.2% accuracy, −15% hallucinations** | ~2 days | [Enhancing Financial RAG with Agentic AI and Multi-HyDE (arXiv 2509.16369)](https://arxiv.org/abs/2509.16369) |
| 7.10b | **Metadata-augmented chunks** — re-ingest with LLM-extracted entities / quantities / topics embedded alongside chunk text | +12pp retrieval F1 | ~3 days + 90-min re-ingest | [RAG Chunking Strategies 2026 (Premai)](https://blog.premai.io/rag-chunking-strategies-the-2026-benchmark-guide/) |
| 7.10c | **Iterative-reasoning (OODA) loop** — tighter version of our research-agent subgraph: generate → check coverage → retrieve more → regenerate, raise turn cap from 5 to 8–10 with sufficiency-driven retrieval expansion | +48pp over non-iterative in published results | ~1 week | Nguyen et al. 2024 (cited in [FinanceBench SOTA survey](https://www.emergentmind.com/topics/financebench)) |

**Decision rule between 7.10b vs 7.10c**: gate on the 7.10a full-eval result. If Multi-HyDE alone gets us past ~52%, prioritize 7.10b (metadata) — it's additive and lower-risk. If 7.10a underperforms (<50%), prioritize 7.10c (iterative reasoning) — it's higher-effort but addresses the multi-step-reasoning bottleneck that Multi-HyDE alone wouldn't fix.

### Why these three, in this order

- **Multi-HyDE first** because it bridges the vocabulary mismatch our queries currently have with chunks ("FY23 revenue" vs "fiscal year 2023 revenues"). Lowest-effort, highest published gain claim, lowest regression risk (additive — original retrieval still runs, HyDE adds candidates).
- **Metadata-augmentation second** if Multi-HyDE works because it stacks naturally: HyDE generates better queries → augmented chunks have better recall against those queries.
- **Iterative reasoning third** if pass rate is still below target after the first two, because it's the biggest single lever but also the riskiest (latency × N iterations, potential for diverging into noise).

### What we are NOT doing

- **PageIndex / vectorless tree-of-contents indexing** — months of architectural rewrite, undocumented production cost, unproven generalization. Wrong tool for this project.
- **More seed / cache / proxy tweaks** — practical ceiling on stochastic-variance reduction was reached at Sprint 8e Fix A given the OpenAI API constraints. Further tweaks burn time without moving quality.
- **More verification evals of the same config** — empirical noise floor is now characterized at ~15% per-question. Re-running the same setup adds no signal.
- **GraphRAG (FinGEAR-style)** — 2–3 week investment with high variance; deferred until after 7.10c if pass rate is still gap-to-SOTA.

### Cost / time budget

Estimated total: **~$30 LLM + ~6 days engineering + ~5 hours of eval wall time** across 7.10a–c if all three ship. Decision gates between each prevent over-investment.

---

## Known limitations / what I'd build next

A senior reviewer should read this section *before* the achievements section. I'm not pretending these aren't real.

1. **Never deployed to production.** The full stack runs locally via `docker compose up -d`. No public URL. No real user traffic. CI workflows exist (`.github/workflows/`) but haven't been used for deployment.
2. **47.3% is below SOTA.** FinGEAR (EMNLP 2025) achieved ~55% with GraphRAG. Patronus's original FinanceBench paper baselines were 38–43%. We sit credibly in the published-baseline range but well below state-of-the-art.
3. **Frontend (Sprint 9) is partial.** Sprint 9.1 vertical slice (login + streaming chat) is built and the BFF wiring works, but the smoke test caught an environment-variable issue (`LITELLM_URL` pointing to a docker-internal hostname while running uvicorn on the host) that's still pending fix. Sidebar history, HITL UI, admin panel, citation PDF viewer are not yet built.
4. **Feature-flagged-off experiments left in source.** `ENABLE_GRADER_EMPTY_CONTEXT_FALLBACK`, `ENABLE_LTR_GATE`, `ENABLE_CALCULATOR_TOOL` all `=False`. The code is preserved as research record but adds surface area to the repo. A cleaner version would delete or move to a `experiments/` branch.
5. **Multi-judge eval all uses gpt-4o-mini.** RAGAS + DeepEval + correctness all judged by the same model family. A cleaner eval would diversify judges to control for judge-family bias (the [`scripts/dual_judge_check.py`](../scripts/dual_judge_check.py) script exists but wasn't used as the canonical gate).
6. **GraphRAG never tried.** Would likely be the biggest single quality lever remaining (FinGEAR shows the gap). Estimated 2–3 weeks of work, deferred until after the Sprint 7.10 levers above.
7. **No production-deployment ops.** No load testing, no horizontal scaling validation, no incident response runbooks. The Langfuse + LiteLLM stack would work in production but hasn't been stress-tested.

If I had another two weeks, the committed priority order (see "Roadmap — Sprint 7.10" section above) is: **(1) Multi-HyDE, (2) metadata-augmented chunks OR iterative-reasoning loop (gated on 7.10a result), (3) ship to free-tier infra (Render + Vercel + Neon) once eval target is hit so the public URL has something defensible to point at**. Sprint 9.2 frontend work (sidebar / HITL UI / admin panel) runs in parallel in a separate chat session and doesn't block the eval-quality push.

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

Total LLM spend across the four eval-quality sprints: **~$80**. Per-eval cost at campaign close: **$5.28** (down from $9.70 pre-tiering — a 46% reduction that compounds on every future eval).
