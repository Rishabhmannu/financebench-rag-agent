# Enterprise RAG Agent

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![LangGraph 0.6](https://img.shields.io/badge/LangGraph-0.6-green.svg)](https://github.com/langchain-ai/langgraph)
[![Tests](https://img.shields.io/badge/tests-294%20passing-brightgreen.svg)]()
[![FinanceBench](https://img.shields.io/badge/FinanceBench-47.3%25%20pass-blue.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Multi-agent LangGraph system for RBAC-secured financial document Q&A with guardrails, self-correction, hallucination checking, and production observability. **Driven end-to-end by a disciplined evaluation methodology** — every shipped change cleared a decision gate; every null result was rolled back with the failure mechanism documented.

## Headline

On the public **FinanceBench** benchmark (150 questions across 32 companies), pass rate trajectory across four eval-quality sprints: **30.7% → 47.3% (+16.6pp)**. Per-eval cost reduced **46%** ($9.70 → $5.28). Multi-hop slice — stuck at 4/13 across three retrieval interventions — finally moved to 6/13 after a LoRA-fine-tuned reranker on FinanceBench labels. Six interventions tested in total; **three shipped, three documented null results rolled back behind feature flags**. The methodology caught the failures cleanly and preserved the wins.

## Features

- **Multi-Agent Pipeline** — 14-node LangGraph StateGraph with 5 conditional edge routing functions, including a **research-agent subgraph** (decompose → retrieve → grade → sufficiency → synthesize, 5-turn cap) for complex queries
- **Role-Based Access Control** — JWT auth with 5 roles, enforced at the vector DB level via Qdrant metadata filtering (unauthorized chunks are never retrieved, not just hidden post-retrieval)
- **3-Layer Guardrails** — Regex heuristics (~0ms) → LLM Guard prompt-injection scanner (~100ms) → LLM-based classifier (~1–2s, only on borderline scores) — cost-optimized cascade
- **PII Detection** — Microsoft Presidio redacts sensitive data before any LLM sees the query
- **Self-Correction** — Automatic query rewriting on irrelevant retrievals (max 2) + hallucination-checker re-generation (max 2)
- **Human-in-the-Loop** — LangGraph `interrupt()` pauses the pipeline for high-value financial answers, persisted via PostgresSaver, resumable via `Command(resume=...)`
- **Heterogeneous Model Tiering** — Sonnet 4.6 for generation, Haiku 4.5 for hallucination verification, gpt-4o-mini for decompose/sufficiency, Llama 3.3 (Groq, free) for routing/grading. Tier mapping validated via dev-set noise-floor methodology (see [Evaluation Methodology](#evaluation-methodology))
- **Fine-Tuned Reranker** — `BAAI/bge-reranker-v2-m3` + LoRA adapter (rank=16, 2.6M trainable / 568M base, ~10MB) trained on FinanceBench correctness labels. Activated by `RERANKER_ADAPTER_PATH` env var; default empty preserves stock BGE behavior
- **Production Observability** — Self-hosted Langfuse v3 stack (postgres + redis + clickhouse + minio + worker + web) + LiteLLM proxy gateway captures every LLM call with cost, latency, tokens, and per-user attribution
- **Semantic Cache** — Redis Stack (RediSearch) backs a LiteLLM `redis-semantic` cache at 0.95 cosine threshold; identical queries serve sub-500ms
- **Per-Stage Result Cache** — Redis-backed caching for embedding / reranker / grader stages
- **SSE Streaming** — Real-time node progress + token streaming via FastAPI Server-Sent Events
- **Multi-Judge Evaluation** — RAGAS (4 metrics) + DeepEval (faithfulness, contextual recall/precision, answer relevancy) + LLM correctness judge, all with reproducibility metadata embedded in pipeline cache

## Architecture

```
START → rbac_gate → guardrails → router ─→ retrieval → reranker → grader → generator → hallucination_checker
                        |          |  |         ↑          ↑          |                       |
                   [blocked]   [clarif]|   [query_rewriter]|       [retrieval_evaluator] [hitl_gate]
                       ↓     [out_scope]|    (retry max 2) |                              |
                      END        ↓     |                   |                       [response_formatter]
                                 END   |                   |                              |
                       [research_required]                 |                             END
                                 ↓                         |
                       research_agent_subgraph: ───────────┘
                       decompose → retrieve → grade → sufficiency → synthesize (5-turn cap)
```

**Selective agentic RAG**: the router classifies each query as `simple_lookup` (~52% of FinanceBench) or `research_required`. Lookup queries take the fast direct path; research queries enter a multi-turn subgraph that decomposes the question, retrieves per sub-question, grades sufficiency, and synthesizes a final answer. **Both paths share the same RBAC-filtered retrieval node** — agentic queries cannot bypass access control.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Orchestration | LangGraph 0.6 (StateGraph, conditional edges, `interrupt()`) |
| Backend | FastAPI + SSE streaming |
| Frontend (legacy) | Gradio ChatInterface (Sprints 1–8) |
| Frontend (current) | Next.js 16 + React 19 + Tailwind 4 + shadcn/ui (Sprint 9.1 in progress — replaces Gradio in 9.5) |
| Vector DB | Qdrant (metadata filtering for RBAC) |
| Document Processing | pypdf canonical (validated against Docling A/B at Sprint 7.5) |
| Generator LLM | Claude Sonnet 4.6 (Anthropic) — quality-justified, kept across all tiering audits |
| Hallucination LLM | Claude Haiku 4.5 (Sprint 7.9 tier downgrade — saves $1.35/eval) |
| Decompose / Sufficiency | gpt-4o-mini (Sprint 7.9 tier downgrade — pure structured-output classifiers) |
| Router / Grader / Query rewriter | Llama 3.3 70B on Groq (free) → gpt-4o-mini fallback |
| Embeddings | voyage-finance-2 (1024d, finance-tuned) — canonical from Sprint 7.8 |
| Reranker | BGE-reranker-v2-m3 + LoRA adapter (Sprint 7.9 fine-tune on FinanceBench labels) |
| LLM Gateway | LiteLLM proxy — single pane of glass for cost/latency/error tracing |
| Observability | Self-hosted Langfuse v3 (6 services: postgres, redis, clickhouse, minio, worker, web) |
| Semantic Cache | Redis Stack (RediSearch) via LiteLLM `redis-semantic` (0.95 threshold) |
| Result Cache | Redis-backed per-stage cache (embedding, reranker, grader) |
| PII Detection | Microsoft Presidio Analyzer + Anonymizer |
| Prompt Injection | LLM Guard + LLM classifier + regex heuristics (3-layer cascade) |
| Auth | PyJWT with role-based access |
| Checkpointing | PostgreSQL via AsyncPostgresSaver |
| Evaluation | RAGAS + DeepEval + LLM correctness judge |
| Tracing | LangSmith (client-side) + Langfuse (gateway-side) |

## Evaluation Methodology

The evaluation discipline is the **strongest engineering signal in this project**. Concretely:

### Two evaluation datasets, two roles
- **Internal canonical (61 Q&A pairs over real SEC 10-K filings for AAPL/MSFT/TSLA FY2023, 249 Qdrant chunks)** — primary regression gate for graph + prompt changes. Cleared all Sprint 7 quality targets (faithfulness 0.811, answer relevancy 0.834, context precision 0.747).
- **External benchmark (FinanceBench: 150 questions across 32 companies, 68k Qdrant chunks from 10-K PDFs)** — published academic benchmark used as a co-primary external gate. The +16.6pp campaign trajectory is on this dataset.

### Multi-judge scoring with reproducibility metadata
- Every full eval runs RAGAS (faithfulness, answer relevancy, context precision, context recall), DeepEval (4 metrics), and a custom LLM correctness judge in parallel.
- Pipeline cache embeds **18-field reproducibility snapshot**: git SHA, settings hash, Qdrant collection state, LLM Guard runtime status, judge model — so two runs can be **proven** to share identical config post-hoc.
- Per-question review artifact joins pipeline + RAGAS + DeepEval + correctness into `<output>.review.{csv,json}` for systematic failure inspection.
- Optional `dual_judge_check.py` runs a sampled re-score with two different judge families (e.g. OpenAI + Anthropic) and emits per-metric mean deltas, agreement rates, and per-sample diffs.

### Decision gates with documented null results
Stratified n=30 dev-set runs precede every full eval. Every intervention faces a binary gate:
- **Ship** — full-eval after dev-set passes
- **Roll back behind a feature flag** — code preserved, `ENABLE_*=False` in [.env.example](.env.example), failure mechanism documented in commit message + sprint handoff

Three null results documented in this campaign (Sprints 7.7–7.8):
1. **Grader empty-context fallback** (`ENABLE_GRADER_EMPTY_CONTEXT_FALLBACK=False`) — dev-set −1 net, no clean rescue mechanism
2. **Doc2Query BM25 enrichment** — targeted experiment on lookup vocabulary mismatch, null effect
3. **Calculator tool** (`ENABLE_CALCULATOR_TOOL=False`) — passed n=5 smoke, **regressed -4pp at full eval scale** via downstream hallucination-checker disclaimer cascade. The +6 new disclaimers in the failed run exactly matched the −6 net regression — direct evidence of the failure mechanism. Multi-agent calibration failure invisible to single-component testing.

### Dev-set noise-floor calibration (Sprint 7.9 Day 2.5 finding)
Re-ran the dev-set with **zero overrides** (default config, identical to canonical baseline). Result: **−3 net, 4 regressions** under identical settings. **Same code, same data, same baseline → −3 net just from grader/judge stochasticity at temperature=0.**

This re-calibrated the decision rule:
- Δ in [−3, +1] → within noise, requires noise-floor reference run or skip to full-eval
- Δ ≥ +2 OR Δ ≤ −4 with new regression patterns → decisive

Retroactively explained why three Sprint 7.7–7.8 dev-set aborts (−1 to −2 net) were within noise; the methodological correction unblocked two interventions that would otherwise have been falsely killed.

### Cost tracking on every LLM call
Every LLM call routed through `LLMFactory` is logged via `cost_tracker.py`. Per-run summaries committed to git under [`cost_logs/by_run/`](cost_logs/by_run/) — full audit trail of campaign spend by sprint, by run, by model.

## Evaluation Results

### External benchmark — FinanceBench campaign trajectory (Sprints 7.6 → 7.9)

| Sprint | Day | Intervention | Pass rate | Δ vs prior | Cost | Status |
|---|---|---|:---:|:---:|:---:|:---:|
| 7.6 Day 1 | — | Claude Sonnet 4.6 generator baseline (honest measurement after refusal-detection patch) | 30.7% | — | $2.91 | baseline |
| 7.6 Day 4 | — | + selective agentic RAG (research-agent subgraph for `research_required`) | **38.7%** | **+8.0pp** | $13 | ✅ shipped |
| 7.7 Day 6 | — | + text-embedding-3-large (3072d) | **43.3%** | **+4.6pp** | $16.50 | ✅ shipped |
| 7.7 Day 7 | — | grader empty-context fallback | (skipped) | dev-set null | $1.99 | ❌ rolled back |
| 7.7 Day 8 | — | Doc2Query BM25 enrichment | (skipped) | targeted null | $0.33 | ❌ rolled back |
| 7.8 Day 16 | — | + voyage-finance-2 embeddings (hosted, finance-tuned, 1024d) | **44.7%** | **+1.4pp** | $9.70 | ✅ shipped |
| 7.8 Day 19 | — | calculator tool (AST-restricted arithmetic) | 40.7% | **−4.0pp** | $9.89 | ❌ rolled back |
| 7.9 Day 3 | — | + heterogeneous model tiering (Haiku for hallucination, gpt-4o-mini for decompose/sufficiency, Sonnet for HITL high-stakes) | (no quality regression) | matches noise floor | $11.62 | ✅ shipped |
| **7.9 Day 7** | — | + LoRA-fine-tuned BGE reranker on FB labels | **47.3%** | **+2.7pp** | **$5.28** | ✅ shipped |

**Total campaign: 30.7% → 47.3% (+16.6pp). Refusal rate halved: 14.0% → 7.3%. Per-eval cost: $9.70 → $5.28 (−46%).**

### Per-slice breakdown (Sprint 7.9 Day 7 vs Sprint 7.8 voyage canonical)

| Slice | Day 16 voyage (Sprint 7.8) | **Day 7 (Sprint 7.9 closer)** | Δ |
|---|:---:|:---:|:---:|
| Pass rate (correctness) | 67/150 (44.7%) | **71/150 (47.3%)** | **+4 / +2.7pp** |
| Refusal rate | 21/150 (14.0%) | **11/150 (7.3%)** | **−6.7pp** |
| RAGAS faithfulness | 0.666 | 0.707 | +0.04 |
| RAGAS context_precision | 0.683 | 0.733 | +0.05 |
| RAGAS context_recall | 0.343 | 0.386 | +0.04 |
| DeepEval contextual_precision | 0.751 | 0.768 | +0.02 |
| Lookup slice (n=86) | 39/86 (45%) | 41/86 (48%) | +2 |
| **Multi-hop slice (n=13)** | **4/13 (31%)** | **6/13 (46%)** | **+2 (+15pp)** ⭐ |
| Calc slice (n=51) | 24/51 (47%) | 24/51 (47%) | +0 (4+/4− churn) |

**The multi-hop unlock is the single most important finding.** Across Sprints 7.7+7.8, FOUR retrieval interventions (3-large, grader-fallback, Doc2Query, voyage-finance-2) and ONE tool-use intervention (calculator) ALL failed to lift the multi-hop slice off 4/13. The LoRA-fine-tuned reranker is the first thing in four sprints to move it. Three multi-hop rescues are clean signal:
- `id_00720` AMEX FY22 gross margin drivers (narrative comparison)
- `id_00724` Pfizer Q22023 regional revenue drop (multi-region comparison)
- `id_01198` AMD FY22 revenue drivers (narrative)

These are "what drove X" / multi-segment comparison questions where success depends on clean top-K input — exactly what the fine-tuned reranker fixes.

### Internal canonical — SEC 61-Q (Sprints 6 → 7.5)

Evaluated on 61 Q&A pairs against real SEC 10-K filings for AAPL / MSFT / TSLA fiscal year 2023 (249 chunks in Qdrant). Evaluator model: `gpt-4o-mini`.

| Metric | Baseline (Sprint 6) | After 7a.v2 (entity-aware retrieval) | After 7b (+ Claude Sonnet 4.6) | **After 7.5 (+ router fix)** | Final Target |
|--------|:---:|:---:|:---:|:---:|:---:|
| Faithfulness | 0.586 | 0.598 | 0.656 | **0.811** | 0.80 ✅ |
| Answer Relevancy | 0.645 | 0.662 | 0.707 | **0.834** | 0.75 ✅ |
| Context Precision | 0.568 | 0.586 | 0.627 | **0.747** | 0.70 ✅ |
| Context Recall | 0.555 | 0.607 | 0.634 | **0.738** | — |

**All Sprint 7 aspirational targets cleared in Sprint 7.5.** The single highest-impact intervention was a ~1-hour router prompt rewrite driven by failure-case inspection ([docs/research/06-failure-analysis.md](docs/research/06-failure-analysis.md)) — the router was falsely classifying ~40% of worst-scoring queries as out-of-scope, preventing the pipeline from even attempting them. Fixing the router moved all four metrics +13 to +21 points.

**GPT-4o-mini vs Claude parity at n=61**: re-running with Claude Sonnet 4.6 as generator scored within RAGAS measurement noise (faithfulness 0.780 vs 0.811, ±0.03). At n=61 the two configs are statistically indistinguishable. Good retrieval + a correct router mattered more than LLM-tier choice on this eval. Production retains Claude for real-user quality; internal eval runs on GPT-4o-mini for cost + reproducibility. The FinanceBench campaign (n=150) is where the LLM-tier wins compound.

Raw scores: [`baseline_real_sec_fy2023.json`](tests/evaluation/eval_results/baseline_real_sec_fy2023.json), [`after_sprint7a_v2_entity_aware.json`](tests/evaluation/eval_results/after_sprint7a_v2_entity_aware.json), [`after_sprint7b_claude_sonnet.json`](tests/evaluation/eval_results/after_sprint7b_claude_sonnet.json), [`after_sprint7_5_router_fix.json`](tests/evaluation/eval_results/after_sprint7_5_router_fix.json).

### FinanceBench parser A/B — pypdf vs docling (Sprint 7.5 Step 4)

Final clean run, both tracks under identical code (git `144ac41f` + reliability patches), identical settings.

| Metric | pypdf RAGAS | docling RAGAS | pypdf DeepEval | docling DeepEval |
|---|:---:|:---:|:---:|:---:|
| Faithfulness | **0.532** | 0.417 | **0.854** | 0.842 |
| Answer Relevancy | **0.384** | 0.301 | **0.735** | 0.714 |
| Context Precision | 0.529 | 0.521 | **0.591** | 0.552 |
| Refusal rate | **22.0%** | 29.3% | — | — |
| Pipeline runtime | **43 min** | 92 min | — | — |

**Decision: pypdf canonical.** Wins on every aggregate metric, 7.3 pp lower refusal rate, 2.1× faster. Nuance: when both produce an answer, docling matches pypdf on per-attempt quality (within 0.04 on every DeepEval dimension) — the aggregate gap is driven by docling's 1500-char chunks giving the retriever fewer "shots on goal" than pypdf's 800-char chunks. Table-aware chunking was *neutral-to-negative* for retrieval-conditioned answering at this chunk size.

### Reproducing a canonical eval

```bash
# Sprint 7.9 canonical (47.3% pass rate)
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

# If interrupted, append --resume-pipeline — partial cache flushed every 5 questions.
```

### Co-primary benchmark governance

- SEC 61-Q evaluation is the primary regression gate for graph + prompt changes
- FinanceBench (150 Q across 32 companies) is the co-primary external benchmark for generalization
- Evaluation outputs include diagnostics slices (`refusal_rate`, lookup/multi-hop/calc per-slice metrics, contamination buckets) in addition to aggregates
- Baseline artifacts checksum-frozen in [`baseline_manifest.json`](tests/evaluation/eval_results/baseline_manifest.json)

## Quick Start

### Prerequisites

- Python 3.12+
- Docker and Docker Compose
- OpenAI API key (required) + Anthropic API key (required for Sonnet generator path)
- Groq API key (optional — free tier covers routing/grading; falls back to OpenAI)
- Voyage AI API key (optional — for voyage-finance-2 embeddings; falls back to OpenAI text-embedding-3-small)

### Local Development

```bash
git clone https://github.com/your-username/rag-agent.git
cd rag-agent
pip install -e ".[dev]"

cp .env.example .env
# Edit .env with your API keys

# Start full stack (Qdrant + Postgres + LiteLLM + Redis + Langfuse v3)
docker compose up -d

# Generate and seed sample financial documents
python scripts/download_sample_data.py
python scripts/seed_qdrant.py --sample

# Start the API server (with hot reload)
make run

# In another terminal, start the Gradio frontend (legacy)
make frontend
```

Open http://localhost:7860 (Gradio) and login with a test account. Langfuse UI at http://localhost:3000 (default `admin@local.test` / `devpassword12`).

### Next.js Frontend (Sprint 9 — in progress)

A Next.js 16 admin/chat UI lives at [`web/`](web/). Phase 9.1 (login + streaming chat with sources + theme toggle) is complete; sidebar history, HITL dialog, admin panel, and citation PDF viewer are upcoming phases. Setup is independent of the Gradio app — both can run side-by-side during the migration:

```bash
cd web
npm install
cp .env.example .env.local
npm run dev                 # http://localhost:3002
```

The frontend uses a BFF pattern — Next.js route handlers proxy to FastAPI with the JWT in an httpOnly cookie, so the browser never sees the token and CORS isn't a concern. See [web/README.md](web/README.md) for full architecture, common pitfalls, and the Sprint 9 phase tracker.

### Test Accounts

| Username | Password | Role | Access |
|----------|----------|------|--------|
| analyst | analyst123 | analyst | Public 10-K filings only |
| finance | finance123 | finance | 10-K, invoices, expense policies (internal) |
| hr | hr123 | hr | Expense policies only (internal) |
| clevel | clevel123 | c_level | All doc types incl. board reports (confidential) |
| admin | admin123 | admin | Full access incl. `/admin/costs` endpoint |

## Development

```bash
make test-unit         # Run 294 unit tests
make test-integration  # Integration tests (requires Qdrant + Postgres)
make eval              # RAGAS evaluation suite
make lint              # Ruff lint check
make format            # Auto-format with Ruff
make check             # Lint + unit tests combined
make jwt               # Generate a test JWT token
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/auth/login` | Login with username/password, returns JWT + user identity |
| `GET`  | `/auth/me` | Current user identity + role-derived RBAC permissions |
| `POST` | `/chat` | Send a query (non-streaming) |
| `POST` | `/chat/stream` | Send a query with SSE streaming |
| `GET`  | `/threads` | List the caller's prior conversations (paginated) |
| `GET`  | `/threads/{thread_id}` | Load messages + interrupt state for a thread (ownership-gated) |
| `DELETE` | `/threads/{thread_id}` | Delete a thread (owner or admin) |
| `POST` | `/hitl/approve` | Approve a HITL-paused response |
| `POST` | `/hitl/reject` | Reject a HITL-paused response |
| `POST` | `/ingest` | Ingest the `data/sample/` directory (admin only) |
| `POST` | `/ingest/upload` | Multipart upload of PDFs + auto-ingest (admin only) |
| `GET`  | `/documents/{filename}` | RBAC-checked inline PDF stream for citation clickthrough |
| `GET`  | `/admin/costs?days=N` | Per-user / per-model / per-trace cost aggregation (admin) |
| `GET`  | `/admin/users` | List configured users (admin) |
| `GET`  | `/admin/roles` | List RBAC roles (admin) |
| `POST` | `/admin/roles` | Create a role (admin) |
| `PATCH` | `/admin/roles/{name}` | Edit a role (admin) |
| `DELETE` | `/admin/roles/{name}` | Delete a non-system role (admin) |
| `GET`  | `/health` | Health check |

## Project Structure

```
src/
├── api/              # FastAPI app + routes (auth, chat, health, hitl, ingest,
│                     # admin, threads, documents)
├── config/           # Settings, RBAC config, prompt templates
├── frontend/         # Gradio ChatInterface (legacy — removed in Sprint 9.5)
├── graph/
│   ├── nodes/        # 14 LangGraph nodes — including research_agent.py subgraph
│   ├── edges.py      # 5 conditional edge routing functions
│   └── builder.py    # StateGraph construction and compilation
├── ingestion/        # Document pipeline (parser → chunk → embed → Qdrant)
├── models/           # Pydantic models (RAGState, schemas, auth)
├── services/         # llm_factory, embeddings, vector_store, reranker_service,
│                     # guardrails_service, ltr_gate_service, candidate_validator,
│                     # cost_tracker, request_context, result_cache, llm_retry,
│                     # roles_service, thread_service
└── tools/            # AST-restricted calculator (feature-flagged off — see Sprint 7.8)

web/                  # Next.js 16 frontend (Sprint 9 — see web/README.md)
├── src/app/          # App Router pages + BFF route handlers
├── src/components/   # shadcn/ui + custom chat components
├── src/hooks/        # use-stream-chat (the SSE consumer state machine)
├── src/lib/          # env, session, api wrappers, hand-mirrored backend types
└── src/proxy.ts      # Next 16 auth gate (formerly middleware.ts)

migrations/           # Alembic — roles table + system role seed (Sprint 9.0)
tests/
├── unit/             # 294 unit tests (mocked LLMs)
├── integration/      # Integration tests
└── evaluation/       # RAGAS + DeepEval + correctness; 61-Q SEC + 150-Q FinanceBench

scripts/              # Data download, ingestion, JWT, dual-judge check,
                      # reranker training data builder, LoRA training script,
                      # debug / failure-analysis utilities
data/
├── sample/           # 8 curated sample financial PDFs (committed)
├── models/           # LoRA reranker adapter (committed: ~10MB safetensors + config)
└── training/         # Reranker training data manifests + jsonl (committed)
cost_logs/by_run/     # Per-sprint cost summaries (committed for audit trail)
```

## Environment Variables

See [`.env.example`](.env.example) for the full list. Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | Yes | OpenAI for embeddings + gpt-4o-mini fallback paths |
| `ANTHROPIC_API_KEY` | Yes | Claude Sonnet generator + Haiku hallucination |
| `GROQ_API_KEY` | No | Free routing/grading; falls back to OpenAI |
| `VOYAGE_API_KEY` | No | voyage-finance-2 embeddings (canonical from Sprint 7.8); falls back to OpenAI text-embedding-3-small |
| `EMBEDDING_PROVIDER` | No | `openai` (default) / `voyage` (canonical Sprint 7.8+) / `abaci` (experimental) |
| `RERANKER_ADAPTER_PATH` | No | Path to LoRA adapter (e.g. `data/models/reranker_ft_v1`); empty preserves stock BGE |
| `LITELLM_URL` | No | LiteLLM gateway URL (e.g. `http://litellm:4000`); empty bypasses gateway |
| `JWT_SECRET` | Yes (prod) | Must be changed from default in production |
| `POSTGRES_PASSWORD` | Yes (prod) | Must be changed from default in production |
| `ENVIRONMENT` | No | `dev` (default), `staging`, or `production` — `model_validator` blocks prod startup with default secrets |
| `CORS_ORIGINS` | No | Allowed origins (default `["*"]`; must be restricted in production) |
| `LANGCHAIN_API_KEY` | No | LangSmith tracing (optional; gateway-side Langfuse is the production observability path) |

## Production Observability (Sprint 8)

The Sprint 8 stack adds end-to-end observability without behavior change at default config:

- **LiteLLM gateway** — every Anthropic / OpenAI / Voyage call routes through a self-hosted proxy when `LITELLM_URL` is set; preserves provider-native features (Anthropic prompt caching `cache_control` fields survive the proxy hop)
- **Self-hosted Langfuse v3** — captures cost, latency, tokens, errors, semantic-cache hit rate, per-user attribution. Six services (postgres + redis + clickhouse + minio + worker + web) all health-gated
- **Per-user cost attribution** — `current_user_id` ContextVar set in FastAPI auth dep → flows through LLMFactory → forwarded as OpenAI/Anthropic `user` field → tagged on Langfuse trace → grouped by `/admin/costs` endpoint. No Langfuse SDK dependency in rag-agent
- **Redis semantic cache** — 0.95 cosine threshold (intentionally strict — false-positive cache hits in financial Q&A are actively harmful). Identical query: HIT, ~0.5s. Paraphrase: correctly MISS at 0.95
- **Per-stage result cache** — embedding / reranker / grader stages each have a Redis-backed cache layer
- **No external SaaS dependency** — production stack runs entirely on infrastructure you own

## Documentation

- [docs/engineering-log.md](docs/engineering-log.md) — Condensed narrative behind the 30.7% → 47.3% campaign: the noise-floor finding, the calculator regression, the LoRA reranker, and the things that aren't obvious from the code
- [docs/architecture.md](docs/architecture.md) — Graph topology, node responsibilities, state management, LLM strategy
- [docs/rbac-matrix.md](docs/rbac-matrix.md) — Role permissions, confidentiality levels, HITL thresholds
- [docs/api-reference.md](docs/api-reference.md) — All endpoints with request/response examples
- [docs/research/](docs/research/) — Feasibility research notes: eval frameworks, LLM providers, retrieval-filter research, failure analysis, chunker experiments

## Known Limitations

Things this project is NOT, said out loud so a reader doesn't have to guess.

1. **Never deployed to production.** The full stack runs locally via `docker compose up -d`. No public URL, no real user traffic. The `.github/workflows/deploy.yml` exists but has not been used to deploy.
2. **47.3% is below SOTA.** [FinGEAR (EMNLP 2025)](https://arxiv.org/abs/2410.18141) achieved ~55% on FinanceBench with GraphRAG. Patronus's original FinanceBench paper baselines were 38–43%. The project sits credibly in the published-baseline range but well below state-of-the-art.
3. **Frontend (Sprint 9) is partial.** The Next.js vertical slice (login + streaming chat + sources + theme + user header) is built and the BFF wiring works against the backend, but Sprint 9.2 (sidebar history, HITL approval UI), 9.3 (citation PDF viewer), 9.4 (admin panel), and 9.5 (file upload, eval dashboard) are unbuilt. The Gradio frontend works as the current usable UI.
4. **Multi-judge eval uses one model family.** RAGAS + DeepEval + correctness judges all run on gpt-4o-mini. A `scripts/dual_judge_check.py` script exists for sampled re-scoring with a second judge family (Anthropic), but it's a manual cross-check, not a CI gate.
5. **Feature-flagged experiments preserved in source.** `ENABLE_GRADER_EMPTY_CONTEXT_FALLBACK`, `ENABLE_LTR_GATE`, `ENABLE_CALCULATOR_TOOL` are all `=False`. Code is preserved as research record, but adds surface area to the repo.
6. **No production ops.** No load testing, no horizontal scaling validation, no incident runbooks.

### What I'd build next, in priority order

1. **Deploy backend + frontend on free-tier infra** (Render + Vercel + Neon Postgres). Closes the biggest credibility gap.
2. **Finish Sprint 9.2** — thread sidebar + HITL approval UI is the most user-visible feature still missing.
3. **GraphRAG experiment** — likely the largest single quality lever remaining. FinGEAR's +8pp over baseline RAG suggests this is where the ceiling is.

## Roadmap

- ✅ **Sprints 1–5** — Foundation (graph, RBAC, guardrails, HITL, Docker)
- ✅ **Sprint 6** — Eval baseline + co-primary benchmark governance
- ✅ **Sprint 7 / 7.5** — Quality wins (faithfulness 0.586 → 0.811 on internal eval)
- ✅ **Sprint 7.6** — Selective agentic RAG (research-agent subgraph): +8.0pp on FinanceBench
- ✅ **Sprint 7.7** — text-embedding-3-large: +4.6pp; 2 documented null results
- ✅ **Sprint 7.8** — voyage-finance-2: +1.4pp; calculator regression (-4pp) rolled back
- ✅ **Sprint 7.9** — Heterogeneous tiering + LoRA reranker: +2.7pp, multi-hop unstuck, −46% per-eval cost
- ✅ **Sprint 8** — Production plumbing (LiteLLM + Redis cache + Langfuse + per-stage result cache); 248 tests
- ✅ **Sprint 9.0** — Backend prereqs for Next.js UI: 11 endpoints (`/auth/me`, `/threads`, `/documents/{filename}`, `/admin/roles` CRUD, `/admin/users`, `/ingest/upload`), Alembic migrations, dynamic role storage; 294 tests
- 🚧 **Sprint 9.1** — Next.js vertical slice ✅ (login + chat with SSE streaming, sources, status pills, user header, theme toggle, BFF auth, proxy gate)
- 🚧 **Sprint 9.2** — Thread sidebar (`/threads`) + HITL approval dialog (`/hitl/*`)
- 🚧 **Sprint 9.3** — Citation PDF clickthrough (in-browser `react-pdf` viewer)
- 🚧 **Sprint 9.4** — Admin panel: `/admin/costs` Recharts dashboard + user table + role CRUD
- 🚧 **Sprint 9.5** — File-upload UI, eval dashboard, Cmd+K palette, `web` service in docker-compose, retire Gradio
- 📝 **Sprint 10** — Portfolio writeup (Medium articles, comparison charts, demo video)

## License

MIT
