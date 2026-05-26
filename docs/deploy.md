# Self-Host Deployment

This document is for users who want to run the backend stack themselves on a laptop, VM, or single-tenant server. For the CLI-driven happy path (`financebench setup` does this for you), see [docs/cli.md](cli.md).

## Two stack profiles

| Profile | Services | When to use | Memory |
|---|---|---|---|
| **Minimal** (`compose.minimal.yml`) | 4: `api`, `qdrant`, `postgres`, `redis` | Day-to-day use, demos, single-tenant install. Cost tracking still works via on-disk `cost_logs/cost_log.jsonl`. | ~2.2 GB at peak |
| **Full** (`docker-compose.yml`) | 11: minimal + `litellm` + Langfuse stack (6 services) + legacy Gradio frontend | When you want the Langfuse trace UI at `:3000`, centralized LLM cost dashboards, semantic cache, model gateway | ~5 GB at peak |

The minimal stack drops LiteLLM by setting `LITELLM_URL=""`, which makes `src/services/llm_factory.py` fall back to direct provider SDKs (Anthropic, OpenAI, Voyage, Groq). Verified in code at [src/services/llm_factory.py:89-97](../src/services/llm_factory.py#L89). Per-call cost is still tracked by the LangChain callback handler ([src/services/cost_tracker.py:249](../src/services/cost_tracker.py#L249)) and written to `cost_logs/cost_log.jsonl` regardless of which stack you run.

## Prerequisites

- Docker Desktop ≥ 4.30 (or Docker Engine + Compose v2)
- ~8 GB free RAM for the minimal stack, ~12 GB for the full stack
- ~10 GB free disk for images + volumes (more if you ingest the full FinanceBench corpus)
- API keys: at minimum `OPENAI_API_KEY` (embeddings + fallback) and `ANTHROPIC_API_KEY` (Sonnet generator). Optional: `VOYAGE_API_KEY` (canonical embedding), `GROQ_API_KEY` (free fast-path).
- Python 3.12+ if you want to run the CLI from source

## Environment variables

The full surface is in [`.env.example`](../.env.example). The critical ones:

| Variable | Required | What it controls |
|---|---|---|
| `OPENAI_API_KEY` | yes | Embeddings (default), gpt-4o-mini routing/grading |
| `ANTHROPIC_API_KEY` | yes | Sonnet 4.6 generator + hallucination check |
| `VOYAGE_API_KEY` | no | voyage-finance-2 embeddings (canonical for FinanceBench eval) |
| `GROQ_API_KEY` | no | Free routing/grading; falls back to OpenAI |
| `EMBEDDING_PROVIDER` | no | `voyage` (canonical), `openai`. Default: `openai` |
| `RERANKER_ADAPTER_PATH` | no | Path to a LoRA adapter for BGE reranker. Empty = stock BGE-reranker-v2-m3. |
| `LITELLM_URL` | no | URL of the LiteLLM gateway. Empty = direct-provider mode (minimal stack). |
| `JWT_SECRET` | yes (prod) | Signing key for auth tokens. Default is `dev-secret-change-in-production` — startup validator blocks prod boot with that value. |
| `POSTGRES_PASSWORD` | yes (prod) | Same: default is `devpassword`; production validator rejects. |
| `ENVIRONMENT` | no | `dev` (default), `staging`, `production`. Triggers the startup validator. |
| `CORS_ORIGINS` | no | JSON array of allowed origins for the web frontend |
| `RESULT_CACHE_REDIS_HOST` / `_PORT` | no | Sub-component result cache (default: localhost:6380 host, redis:6379 in-container) |
| `QDRANT_COLLECTION` | no | Default `financial_docs`. Override to `cli_test_corpus` for the CLI demo path or `financebench_corpus_pypdf_voyage_finance2` for eval runs. |

## Bringing up the minimal stack manually

(`financebench setup` does this automatically, but here's the raw equivalent.)

```bash
git clone https://github.com/Rishabhmannu/financebench-rag-agent.git
cd financebench-rag-agent
cp .env.example .env                    # then edit to add your keys
docker compose -f compose.minimal.yml up -d
# wait for /v1/health -> 200 (3-6 min on cold cache; BGE downloads ~570MB)
docker compose -f compose.minimal.yml exec api python scripts/seed_qdrant.py --sample
```

Then verify:

```bash
curl http://localhost:8000/v1/health        # → {"status": "ok"}
curl http://localhost:8000/version          # → {"api_version": "1", ...}
curl http://localhost:8000/v1/warm          # → loaded models
```

## Bringing up the full stack

Same shape, different compose file. Includes the 6-service Langfuse stack with auto-bootstrap (first boot creates an org, project, and dev user; subsequent boots skip the init).

```bash
docker compose up -d
# Langfuse UI: http://localhost:3000  (dev@local.test / devpassword12)
```

All Langfuse defaults are sourced via `${VAR:-default}` in `docker-compose.yml` so a clean `docker compose up` works without manual config — but for ANY shared deployment you must override every `LANGFUSE_*` variable in `.env` (see `.env.example`'s LANGFUSE section). The defaults only protect your local-machine instance; rotating them is your responsibility before any non-local use.

## Production hardening checklist

If you're putting this anywhere shared (a VPS, a corporate network), don't skip these:

- [ ] **Set `ENVIRONMENT=production`** so the startup model_validator blocks boot with default secrets.
- [ ] **Rotate `JWT_SECRET`** to a high-entropy random value (e.g., `openssl rand -hex 32`).
- [ ] **Rotate `POSTGRES_PASSWORD`** away from `devpassword`.
- [ ] **Rotate `LANGFUSE_*` defaults** (PG password, Redis password, MinIO password, NEXTAUTH_SECRET, encryption key, init user password) if running the full stack.
- [ ] **Replace `DEV_USERS` in [src/api/routes/auth.py](../src/api/routes/auth.py)** with a real user store. The hardcoded analyst/finance/hr/clevel/admin accounts exist for local dev only.
- [ ] **Front the API with HTTPS** (Caddy / Traefik / nginx) — the FastAPI server runs HTTP only.
- [ ] **Configure `CORS_ORIGINS`** to your actual frontend domain, not `["*"]`.
- [ ] **Set `RERANKER_ADAPTER_PATH`** only if you have a fine-tuned adapter; otherwise leave unset and the stock BGE-reranker-v2-m3 loads.
- [ ] **Plan a backup strategy** for the `pg_data`, `qdrant_data`, and `cost_logs` directories — these contain all conversation history, vector data, and audit trail.

## Backup

The persistent state lives in named docker volumes + one host directory:

| Volume / dir | What's in it | When to back up |
|---|---|---|
| `pg_data` | Postgres: thread checkpoints (LangGraph), roles, alembic state | Daily; this is your conversation memory |
| `qdrant_data` | Vector embeddings + payloads for all ingested documents | After every corpus ingest; rare otherwise |
| `redis_data` | Sub-component caches (reranker scores, embedding cache). Lossy — safe to wipe. | Optional |
| `hf_cache` | BGE reranker weights (~570MB). Reproducible from HuggingFace. | Optional |
| `./cost_logs/` (host) | Append-only LLM cost log | Daily for compliance |
| `./logs/` (host) | Structured event logs (Sprint 7.19) | Weekly rotation OK |

Simplest backup: `docker compose down` (clean shutdown), `docker run --rm -v campusx-langgraph-course_pg_data:/data -v $(pwd)/backups:/backup alpine tar czf /backup/pg_data-$(date +%F).tar.gz /data`, then `docker compose up -d`. Repeat for `qdrant_data`.

## Verifying state

```bash
# What's actually loaded?
curl http://localhost:8000/v1/warm
# {"status": "warm", "loaded": {"reranker": "CrossEncoder", "sparse_embedder": "SparseTextEmbedding"}}

# Live thread + checkpoint count
docker exec campusx-langgraph-course-postgres-1 psql -U rag_user -d rag_agent \
  -c "SELECT COUNT(DISTINCT thread_id) FROM checkpoints;"

# Recent LLM cost
python -m src.services.cost_tracker --tail 20

# Boot banner (Sprint 7.19 audit trail) — shows what's actually loaded
docker compose logs api 2>&1 | grep -A 15 "Pipeline boot"
```

## Common gotchas

1. **Port 6380 collision on macOS** — `redis_data` exposes its server on host port 6380 (not 6379) because Homebrew's launchd Redis often already binds 6379. If you also run Brew Redis, no conflict.
2. **Postgres port 5432** — collides if you have another Postgres on your laptop. Stop the other one or edit `compose.minimal.yml` ports.
3. **First boot is slow** — BGE reranker (~570MB) downloads from HuggingFace on cold start. ~3-6 min. `hf_cache` volume makes subsequent boots ~30s.
4. **`uvicorn --reload` and code edits** — `make run` runs the api on host with `--reload`. When developing, your file edits hit the next request immediately. Docker-running api requires a `financebench upgrade` (or manual `docker compose build api`) to pick up backend changes.
5. **macOS Docker Desktop hangs after sleep** — known issue. If you see `docker ps` hanging, restart Docker Desktop from the tray icon. Volumes are preserved.

## Upgrading

See [docs/upgrade.md](upgrade.md) for the full playbook. Short version:

```bash
financebench upgrade     # git pull + compose build + restart + health probe
```
