# Enterprise RAG Agent

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![LangGraph 0.6](https://img.shields.io/badge/LangGraph-0.6-green.svg)](https://github.com/langchain-ai/langgraph)
[![Tests](https://img.shields.io/badge/tests-152%20passing-brightgreen.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Multi-agent LangGraph system for RBAC-secured financial document Q&A with guardrails, self-correction, hallucination checking, and production monitoring.

## Features

- **Multi-Agent Pipeline** — 14-node LangGraph StateGraph with 5 conditional edge routing functions
- **Role-Based Access Control** — JWT auth with 5 roles, enforced at the vector DB level via Qdrant metadata filtering
- **3-Layer Guardrails** — Regex heuristics → LLM Guard prompt injection scanner → LLM-based classifier (cost-optimized cascade)
- **PII Detection** — Microsoft Presidio redacts sensitive data before any LLM sees the query
- **Self-Correction** — Automatic query rewriting when retrieved chunks are irrelevant (max 2 retries)
- **Hallucination Checking** — LLM-based grounding verification with retry loop (max 2 retries)
- **Human-in-the-Loop** — LangGraph `interrupt()` pauses the pipeline for high-value financial answers, persisted via PostgresSaver
- **SSE Streaming** — Real-time node progress and token streaming via FastAPI Server-Sent Events
- **RAGAS Evaluation** — 61 Q&A test pairs with CI/CD gate on faithfulness, answer relevancy, and context precision
- **LangSmith Tracing** — Full observability of every LLM call, retrieval, and routing decision

## Architecture

```
START → rbac_gate → guardrails → router ─→ retrieval → grader → generator → hallucination_checker
                        |            |          ↑          |                         |
                   [blocked]    [clarification]  |    [query_rewriter]          [hitl_gate]
                       ↓        [out_of_scope]   |     (retry max 2)               |
                      END            ↓           |                          [response_formatter]
                                    END     retry loop                             |
                                                                                  END
```

**LLM Strategy**: Llama 3.3 70B on Groq (free) for routing, grading, and classification. GPT-4o-mini for generation and hallucination checking. Automatic fallback between providers via `LLMFactory`.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Orchestration | LangGraph 0.6 (StateGraph, conditional edges, interrupt) |
| Backend | FastAPI + SSE streaming |
| Frontend | Gradio ChatInterface |
| Vector DB | Qdrant (metadata filtering for RBAC) |
| Document Processing | Docling + HybridChunker (512 token chunks) |
| LLMs | Llama 3.3 70B (Groq) + GPT-4o-mini (OpenAI) |
| Embeddings | OpenAI text-embedding-3-small (1536 dims) |
| PII Detection | Microsoft Presidio Analyzer + Anonymizer |
| Prompt Injection | LLM Guard + LLM classifier + regex heuristics |
| Auth | PyJWT with role-based access |
| Checkpointing | PostgreSQL via AsyncPostgresSaver |
| Evaluation | RAGAS (faithfulness, answer relevancy, context precision) |
| Tracing | LangSmith |

## Evaluation Results

Evaluated on 61 Q&A pairs against real SEC 10-K filings for AAPL / MSFT / TSLA fiscal year 2023 (249 chunks in Qdrant). Evaluator model: `gpt-4o-mini`.

### Sprint-by-sprint progress

| Metric | Baseline (Sprint 6) | After 7a.v2 (entity-aware retrieval) | After 7b (+ Claude Sonnet 4.6) | **After 7.5 (+ router fix, GPT-4o-mini)** | Final Target |
|--------|:---:|:---:|:---:|:---:|:---:|
| Faithfulness | 0.586 | 0.598 | 0.656 | **0.811** | 0.80 ✅ |
| Answer Relevancy | 0.645 | 0.662 | 0.707 | **0.834** | 0.75 ✅ |
| Context Precision | 0.568 | 0.586 | 0.627 | **0.747** | 0.70 ✅ |
| Context Recall | 0.555 | 0.607 | 0.634 | **0.738** | — |

**All original Sprint 7 aspirational targets cleared in Sprint 7.5.** The single highest-impact intervention was a ~1-hour router prompt rewrite driven by failure-case inspection ([docs/research/06-failure-analysis.md](docs/research/06-failure-analysis.md)) — the router was falsely classifying ~40% of worst-scoring queries as out-of-scope, preventing the pipeline from even attempting them. Fixing the router recovered those questions and moved all four metrics +13 to +21 points.

**GPT-4o-mini vs Claude Sonnet 4.6 parity finding**: Re-running the same pipeline with Claude as the generator (`after_sprint7_5_router_fix_claude.json`) scored within RAGAS measurement noise of the GPT-4o-mini run (faithfulness 0.780 vs 0.811, a ±0.03 delta). At n=61 questions the two configs are statistically indistinguishable. Good retrieval + a correct router mattered more than LLM-tier choice on this eval. Production retains Claude for real-user quality; evaluation runs on GPT-4o-mini for cost + reproducibility.

Raw scores: [`baseline_real_sec_fy2023.json`](tests/evaluation/eval_results/baseline_real_sec_fy2023.json), [`after_sprint7a_v2_entity_aware.json`](tests/evaluation/eval_results/after_sprint7a_v2_entity_aware.json), [`after_sprint7b_claude_sonnet.json`](tests/evaluation/eval_results/after_sprint7b_claude_sonnet.json), [`after_sprint7_5_router_fix.json`](tests/evaluation/eval_results/after_sprint7_5_router_fix.json), [`after_sprint7_5_router_fix_claude.json`](tests/evaluation/eval_results/after_sprint7_5_router_fix_claude.json).

### Co-primary benchmark governance

- SEC 61-Q evaluation remains the primary regression gate for graph + prompt changes.
- FinanceBench (150 Q across 32 companies) is tracked as a co-primary external benchmark for generalization.
- Evaluation outputs include diagnostics slices (`refusal_rate`, question-type slices `lookup`/`multi_hop`/`calc`, and contamination buckets) in addition to aggregate metrics.
- Baseline artifacts are checksum-frozen in [`baseline_manifest.json`](tests/evaluation/eval_results/baseline_manifest.json).
- Pipeline caches embed full reproducibility metadata (git SHA, settings snapshot, Qdrant collection state, judge model) so two runs can be **proven** to share identical config post-hoc.

### FinanceBench external benchmark — pypdf vs docling A/B (Sprint 7.5 Step 4)

Final clean run, both tracks under identical code (git `144ac41f` + reliability patches), identical settings, `FORCE_OPENAI_ONLY=true` (GPT-4o-mini for generator + judge), `RERANKER_DEVICE=cpu`, LLM Guard runtime disabled. Patronus skipped (free-tier credits exhausted); DeepEval used as second-judge framework.

| Metric | pypdf RAGAS | docling RAGAS | pypdf DeepEval | docling DeepEval |
|---|:---:|:---:|:---:|:---:|
| Faithfulness | **0.532** | 0.417 | **0.854** | 0.842 |
| Answer Relevancy | **0.384** | 0.301 | **0.735** | 0.714 |
| Context Precision | 0.529 | 0.521 | **0.591** | 0.552 |
| Context Recall | **0.248** | 0.242 | 0.488 | **0.492** |
| Refusal rate | **22.0%** (33/150) | 29.3% (44/150) | — | — |
| Empty-context rate | **26.0%** (39/150) | 31.3% (47/150) | — | — |
| Pipeline runtime | **43 min** | 92 min | — | — |

**Decision: pypdf is the canonical FinanceBench parser.** Wins on every aggregate metric, has a 7.3 pp lower refusal rate, 5.3 pp lower empty-context rate, and runs 2.1× faster.

**Nuance worth knowing**: when both systems do produce an answer, docling is essentially equivalent on per-attempt quality (within 0.04 on every DeepEval dimension) and slightly better on contextual recall (+5.5 pp on the answered subset). Docling's table-aware chunks really do contain more per chunk — but its 1500-char chunk size gives the retriever fewer "shots on goal" than pypdf's 800-char chunks, and that recall difference dominates the aggregate result.

**Estimated pass rate** (1 − refusal_rate × answered-recall): pypdf ≈ 51%, docling ≈ 50%. Both land comfortably in the published RAG-baseline range (38–55% per FinanceBench paper [Islam et al, Patronus 2023] and FinGEAR [EMNLP 2025]), above the GPT-4-Turbo baseline RAG (38–43%), below FinGEAR graph-augmented SOTA (~55%).

Raw scores: [`financebench_pypdf_clean.json`](tests/evaluation/eval_results/financebench_pypdf_clean.json), [`financebench_docling_clean.json`](tests/evaluation/eval_results/financebench_docling_clean.json), DeepEval per-sample at [`financebench_pypdf_clean.deepeval.json`](tests/evaluation/eval_results/financebench_pypdf_clean.deepeval.json) and [`financebench_docling_clean.deepeval.json`](tests/evaluation/eval_results/financebench_docling_clean.deepeval.json).

Reproduce a clean run end-to-end:

```bash
# pypdf track (~75 min pipeline + 4 min RAGAS + 12 min DeepEval)
python tests/evaluation/run_financebench.py \
  --output tests/evaluation/eval_results/financebench_pypdf_clean.json \
  --collection financebench_corpus_pypdf_clean \
  --ragas-judge-model gpt-4o-mini \
  --deepeval-concurrency 6 \
  --flush-every 5

# docling track (same command; swap output + collection name)
python tests/evaluation/run_financebench.py \
  --output tests/evaluation/eval_results/financebench_docling_clean.json \
  --collection financebench_corpus_docling_clean \
  --ragas-judge-model gpt-4o-mini \
  --deepeval-concurrency 6 \
  --flush-every 5

# If interrupted, append --resume-pipeline to the same command — partial cache
# is flushed every 5 questions and resumes at the last checkpoint.
```

Optional: re-enable Patronus as a third judge (requires funded `PATRONUS_API_KEY`):

```bash
python tests/evaluation/run_financebench.py ... --enable-patronus
```

### Dual-judge reliability check

Run a sampled re-score with two different judge families and generate an agreement report:

```bash
python scripts/dual_judge_check.py \
  --pipeline-cache tests/evaluation/eval_results/financebench_pypdf_clean.pipeline.json \
  --dataset financebench \
  --primary-judge openai:gpt-4o-mini \
  --secondary-judge anthropic:claude-sonnet-4-5 \
  --sample-size 30 \
  --output tests/evaluation/eval_results/financebench_dual_judge_report.json
```

The report includes per-metric mean deltas, mean absolute differences, agreement rates under a configurable threshold, and per-sample score diffs for manual audit.

An earlier baseline on synthetic PDFs ([`baseline_pre_optimization.json`](tests/evaluation/eval_results/baseline_pre_optimization.json)) is preserved for historical comparison — it was replaced because the synthetic corpus was too small (36 chunks) to exercise the retrieval stack realistically.

## Quick Start

### Prerequisites

- Python 3.12+
- Docker and Docker Compose
- OpenAI API key
- Groq API key (optional — falls back to OpenAI)

### Local Development

```bash
# Clone and install
git clone https://github.com/your-username/rag-agent.git
cd rag-agent
pip install -e ".[dev]"

# Configure environment
cp .env.example .env
# Edit .env with your OPENAI_API_KEY, GROQ_API_KEY, JWT_SECRET, etc.

# Start infrastructure (Qdrant + PostgreSQL)
make docker-up

# Generate and seed sample financial documents
python scripts/download_sample_data.py
python scripts/seed_qdrant.py --sample

# Start the API server (with hot reload)
make run

# In another terminal, start the Gradio frontend
make frontend
```

Open http://localhost:7860 and login with a test account.

### Run Everything with Docker

```bash
docker compose up --build
```

Starts all 4 services: API (`:8000`), Frontend (`:7860`), Qdrant (`:6333`), PostgreSQL (`:5432`).

### Test Accounts

| Username | Password | Role | Access |
|----------|----------|------|--------|
| analyst | analyst123 | analyst | Public 10-K filings only |
| finance | finance123 | finance | 10-K, invoices, expense policies (internal) |
| hr | hr123 | hr | Expense policies only (internal) |
| clevel | clevel123 | c_level | All doc types incl. board reports (confidential) |
| admin | admin123 | admin | Full access to everything |

## Development

```bash
make test-unit         # Run 152 unit tests
make test-integration  # Run integration tests (requires Qdrant + Postgres)
make eval              # Run RAGAS evaluation suite
make lint              # Ruff lint check
make format            # Auto-format with Ruff
make check             # Lint + unit tests combined
make jwt               # Generate a test JWT token
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/auth/login` | Login with username/password, returns JWT |
| `POST` | `/chat` | Send a query (non-streaming) |
| `POST` | `/chat/stream` | Send a query with SSE streaming |
| `POST` | `/hitl/approve` | Approve a HITL-paused response |
| `POST` | `/hitl/reject` | Reject a HITL-paused response |
| `POST` | `/ingest` | Ingest documents into Qdrant |
| `GET`  | `/health` | Health check |

## Project Structure

```
src/
├── api/              # FastAPI application and route handlers
│   └── routes/       # auth, chat, health, hitl, ingest
├── config/           # Settings, RBAC config, prompt templates
├── frontend/         # Gradio ChatInterface with streaming + HITL UI
├── graph/
│   ├── nodes/        # 14 LangGraph nodes (one file each)
│   ├── edges.py      # 5 conditional edge routing functions
│   └── builder.py    # StateGraph construction and compilation
├── ingestion/        # Document pipeline (Docling → chunk → embed → Qdrant)
├── models/           # Pydantic models (RAGState, schemas, auth)
└── services/         # LLM factory, auth, embeddings, guardrails, vector store

tests/
├── unit/             # 152 unit tests (mocked LLMs)
├── integration/      # Integration tests
└── evaluation/       # RAGAS evaluation suite (61 Q&A pairs)

scripts/              # Data download, Qdrant seeding, JWT generation
data/sample/          # 8 curated sample financial PDFs
```

## Environment Variables

See [`.env.example`](.env.example) for the full list. Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | Yes | OpenAI API key for generation + embeddings |
| `GROQ_API_KEY` | No | Groq API key for free routing/grading (falls back to OpenAI) |
| `JWT_SECRET` | Yes (prod) | Must be changed from default in production |
| `POSTGRES_PASSWORD` | Yes (prod) | Must be changed from default in production |
| `ENVIRONMENT` | No | `dev` (default), `staging`, or `production` |
| `CORS_ORIGINS` | No | Allowed origins (default `["*"]`; must be restricted in production) |
| `LANGCHAIN_API_KEY` | No | LangSmith tracing (optional) |

## Deployment

### Docker Compose (local/staging)

```bash
make docker-prod    # Build and start all services in detached mode
make docker-logs    # Tail logs
make docker-ps      # Check service health
make docker-restart # Restart API + frontend
```

### AWS ECS Fargate (production)

The project includes a GitHub Actions workflow ([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)) that:

1. Runs unit tests
2. Runs RAGAS evaluation (gates deployment on quality thresholds)
3. Builds and pushes Docker image to ECR
4. Deploys to ECS Fargate

Required GitHub Secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY`.

## Documentation

- [docs/architecture.md](docs/architecture.md) — Graph topology, node responsibilities, state management, LLM strategy
- [docs/rbac-matrix.md](docs/rbac-matrix.md) — Role permissions, confidentiality levels, HITL thresholds
- [docs/api-reference.md](docs/api-reference.md) — All endpoints with request/response examples
- [docs/research/](docs/research/) — Feasibility research: eval frameworks, LLM providers, production roadmap, UI framework comparison

## Roadmap

- [PROJECT_MASTER_DOCUMENT.md](PROJECT_MASTER_DOCUMENT.md) — Original design spec (Sprints 1–5)
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — Forward-looking plan (Sprints 6–10: quality wins, production plumbing, Next.js UI, portfolio content)

## License

MIT
