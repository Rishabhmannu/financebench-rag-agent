# Setup & Development

This document covers everything needed to run the project locally — backend, frontend, sample data, and the API surface.

## Prerequisites

- Python 3.12+
- Node 20+ (for the Next.js frontend)
- Docker and Docker Compose
- API keys:
  - **OpenAI** (required) — embeddings + gpt-4o-mini fallback paths
  - **Anthropic** (required) — Claude Sonnet 4.6 generator + Haiku 4.5 hallucination check
  - **Groq** (optional, free) — routing / grading / query rewriter (falls back to OpenAI)
  - **Voyage AI** (optional, free 50M token tier) — finance-tuned embeddings (Sprint 7.8+ canonical)

## Local development — backend

```bash
git clone https://github.com/Rishabhmannu/financebench-rag-agent.git
cd financebench-rag-agent
pip install -e ".[dev]"

cp .env.example .env
# Edit .env with your API keys + JWT_SECRET

# Bring up the substrate (Qdrant + Postgres + the Sprint 8 observability stack)
docker compose up -d

# Generate and seed sample financial documents
python scripts/download_sample_data.py
python scripts/seed_qdrant.py --sample

# Start the API server with hot reload
make run                    # http://localhost:8000

# (Legacy) Gradio frontend
make frontend               # http://localhost:7860
```

## Local development — Next.js frontend (Sprint 9)

```bash
cd web
npm install
cp .env.example .env.local
npm run dev                  # http://localhost:3002
```

See [`web/README.md`](../web/README.md) for the full BFF / architecture / pitfalls notes.

Why port 3002, not 3000: port 3000 is taken by the Sprint 8 Langfuse stack.

## Test accounts (development only)

| Username | Password | Role | Access |
|----------|----------|------|--------|
| analyst | analyst123 | analyst | Public 10-K filings only |
| finance | finance123 | finance | 10-K, invoices, expense policies (internal) |
| hr | hr123 | hr | Expense policies only (internal) |
| clevel | clevel123 | c_level | All doc types including board reports (confidential) |
| admin | admin123 | admin | Full access including `/admin/costs` endpoint |

These are seeded into `src/api/routes/auth.py` for local development. Replace with a real user store before any non-local deployment.

## Useful make targets

```bash
make test-unit         # 294 unit tests (mocked LLMs)
make test-integration  # Integration tests (requires Qdrant + Postgres up)
make eval              # RAGAS evaluation suite
make lint              # Ruff lint check
make format            # Auto-format with Ruff
make check             # Lint + unit tests combined
make jwt               # Generate a test JWT token
```

## API surface (high-level)

For full request/response schemas see [`api-reference.md`](api-reference.md).

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/login` | Login with username/password, returns JWT + user identity |
| GET | `/auth/me` | Current user identity + role-derived RBAC permissions |
| POST | `/chat` | Send a query (non-streaming) |
| POST | `/chat/stream` | Send a query with SSE streaming |
| GET | `/threads` | List the caller's prior conversations (paginated) |
| GET | `/threads/{thread_id}` | Load messages + interrupt state for a thread |
| DELETE | `/threads/{thread_id}` | Delete a thread (owner or admin) |
| POST | `/hitl/approve` | Approve a HITL-paused response |
| POST | `/hitl/reject` | Reject a HITL-paused response |
| POST | `/ingest` | Ingest the `data/sample/` directory (admin only) |
| POST | `/ingest/upload` | Multipart upload of PDFs and auto-ingest (admin only) |
| GET | `/documents/{filename}` | RBAC-checked inline PDF stream for citation clickthrough |
| GET | `/admin/costs?days=N` | Per-user / per-model / per-trace cost aggregation (admin) |
| GET | `/admin/users` | List configured users (admin) |
| GET | `/admin/roles` | List RBAC roles (admin) |
| POST | `/admin/roles` | Create a role (admin) |
| PATCH | `/admin/roles/{name}` | Edit a role (admin) |
| DELETE | `/admin/roles/{name}` | Delete a non-system role (admin) |
| GET | `/health` | Health check |

## Project layout

```
src/
  api/              FastAPI app + routes (auth, chat, threads, documents, hitl, ingest, admin, health)
  config/           Settings, RBAC config, prompt templates
  frontend/         Gradio ChatInterface (legacy — to be retired in Sprint 9.5)
  graph/
    nodes/          14 LangGraph nodes including research_agent.py subgraph
    edges.py        5 conditional edge routing functions
    builder.py      StateGraph construction and compilation
  ingestion/        Document pipeline (parser -> chunk -> embed -> Qdrant)
  models/           Pydantic models (RAGState, schemas, auth)
  services/         llm_factory, embeddings, vector_store, reranker_service,
                    guardrails_service, ltr_gate_service, candidate_validator,
                    cost_tracker, request_context, result_cache, llm_retry,
                    roles_service, thread_service
  tools/            AST-restricted calculator (feature-flagged off — see engineering log)

web/                Next.js 16 frontend (Sprint 9 — see web/README.md)
  src/app/          App Router pages + BFF route handlers
  src/components/   shadcn/ui + custom chat components
  src/hooks/        use-stream-chat (the SSE consumer state machine)
  src/lib/          env, session, api wrappers, hand-mirrored backend types
  src/proxy.ts      Next 16 auth gate (formerly middleware.ts)

migrations/         Alembic — roles table + system role seed (Sprint 9.0)
tests/
  unit/             294 unit tests (mocked LLMs)
  integration/      Integration tests
  evaluation/       RAGAS + DeepEval + correctness; 61-Q SEC + 150-Q FinanceBench

scripts/            Data download, ingestion, JWT generation, dual-judge check,
                    reranker training data builder, LoRA training script,
                    failure-analysis utilities
data/
  sample/           8 curated sample financial PDFs (committed)
  models/           LoRA reranker adapter (committed: ~10MB safetensors + config)
  training/         Reranker training data manifests + jsonl (committed)
```

## Key environment variables

See [`.env.example`](../.env.example) for the full list.

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | yes | OpenAI for embeddings + gpt-4o-mini fallback paths |
| `ANTHROPIC_API_KEY` | yes | Claude Sonnet generator + Haiku hallucination check |
| `GROQ_API_KEY` | no | Free routing/grading; falls back to OpenAI |
| `VOYAGE_API_KEY` | no | voyage-finance-2 embeddings (canonical from Sprint 7.8) |
| `EMBEDDING_PROVIDER` | no | `openai` (default), `voyage` (canonical Sprint 7.8+), `abaci` (experimental) |
| `RERANKER_ADAPTER_PATH` | no | LoRA adapter path (e.g. `data/models/reranker_ft_v1`); empty preserves stock BGE |
| `LITELLM_URL` | no | LiteLLM gateway URL; empty bypasses gateway (direct-provider mode) |
| `JWT_SECRET` | yes (prod) | Must be changed from default in production |
| `POSTGRES_PASSWORD` | yes (prod) | Must be changed from default in production |
| `ENVIRONMENT` | no | `dev` (default), `staging`, or `production` — startup model_validator blocks prod boot with default secrets |
| `CORS_ORIGINS` | no | JSON array of allowed origins |
