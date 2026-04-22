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

| Metric | Baseline (Sprint 6) | After 7a.v2 (entity-aware retrieval) | **After 7b (+ Claude Sonnet 4.6)** | CI Gate | Final Target |
|--------|:---:|:---:|:---:|:---:|:---:|
| Faithfulness | 0.586 | 0.598 | **0.656** | >= 0.62 | 0.80 |
| Answer Relevancy | 0.645 | 0.662 | **0.707** | >= 0.68 | 0.75 |
| Context Precision | 0.568 | 0.586 | **0.627** | >= 0.60 | 0.70 |
| Context Recall | 0.555 | 0.607 | **0.634** | — | — |

Current pipeline clears all CI thresholds. Remaining gap to the aspirational targets comes from source data (MD&A + Financial Statements only — expanding to include Risk Factors + Notes would raise the faithfulness ceiling) and retrieval (smaller chunks or a managed reranker). LLM-shaped gains were captured in Sprint 7b.

Raw scores: [`baseline_real_sec_fy2023.json`](tests/evaluation/eval_results/baseline_real_sec_fy2023.json), [`after_sprint7a_v2_entity_aware.json`](tests/evaluation/eval_results/after_sprint7a_v2_entity_aware.json), [`after_sprint7b_claude_sonnet.json`](tests/evaluation/eval_results/after_sprint7b_claude_sonnet.json).

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
