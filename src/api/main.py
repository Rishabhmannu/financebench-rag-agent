import logging
import os
import subprocess
from contextlib import asynccontextmanager
from functools import lru_cache

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import AsyncConnectionPool

from src.api.routes import admin, approvals, auth, chat, documents, health, hitl, ingest, threads
from src.config.settings import settings

API_VERSION = "1"

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL), format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Set env-specific LangSmith project name before any LLM calls
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project_name

    # Sprint 7.19 logging Tier 1: structured event log + boot banner.
    # Attaches a FileHandler so logger.info from all 17 graph nodes ends up in
    # logs/run_<ts>.log, and prints "what's actually loaded" to stderr.
    from src.services.event_log import attach_file_handler, log_runtime_components
    attach_file_handler()
    log_runtime_components()

    # Startup validation
    if not settings.OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY is not set! Embeddings and generation will fail.")
    if not settings.GROQ_API_KEY:
        logger.warning("GROQ_API_KEY is not set. Router/grader will fall back to OpenAI.")
    if not settings.LANGCHAIN_API_KEY:
        logger.warning("LANGCHAIN_API_KEY is not set. LangSmith tracing will be disabled.")

    # Sprint 9.0 — run alembic migrations before anything that reads the DB.
    # The roles table (and future schema) must exist before roles_service
    # tries to hydrate its cache. Failures here are logged but non-fatal:
    # roles_service falls back to the static dict in rbac_config so the
    # app still boots if alembic can't reach Postgres.
    try:
        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations: up-to-date")
    except Exception as e:
        logger.warning("Alembic upgrade failed (continuing with static RBAC): %s", e)

    # Initialize PostgresSaver checkpointer for HITL persistence
    try:
        conninfo = settings.postgres_url
        logger.info(f"Connecting to PostgreSQL at {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}")
        pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=5,
            open=False,
        )
        await pool.open()
        # Verify connection works
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        logger.info("PostgreSQL connection verified")

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # Run setup with autocommit connection (CREATE INDEX CONCURRENTLY requires it)
        async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as setup_conn:
            setup_checkpointer = AsyncPostgresSaver(setup_conn)
            await setup_checkpointer.setup()
        logger.info("Checkpointer tables created")

        checkpointer = AsyncPostgresSaver(pool)
        app.state.checkpointer = checkpointer
        app.state.pool = pool
        logger.info("PostgresSaver checkpointer initialized for HITL persistence")
    except Exception as e:
        logger.error(f"PostgresSaver init failed (HITL will be disabled): {e}", exc_info=True)
        app.state.checkpointer = None
        app.state.pool = None

    logger.info(f"RAG Agent API starting (environment={settings.ENVIRONMENT})")
    yield

    # Cleanup
    if app.state.pool:
        await app.state.pool.close()
    logger.info("RAG Agent API shutting down")


app = FastAPI(
    title="FinanceBench RAG Agent API",
    description="Enterprise Financial Document Q&A with RBAC, Guardrails, and Multi-Agent Pipeline",
    version="0.3.5",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@lru_cache(maxsize=1)
def _git_sha() -> str:
    # 0.1.5: prefer the GIT_SHA env var (injected at docker build time via
    # ARG → ENV in the Dockerfile, value passed by the wizard's
    # --build-arg GIT_SHA=$(git rev-parse HEAD)). Container has no .git/ so
    # subprocess fallback always returned "unknown" — the banner reported
    # "sha unknown" on every running container before this.
    env_sha = os.environ.get("GIT_SHA", "").strip()
    if env_sha and env_sha != "unknown":
        return env_sha[:12]
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        return out[:12]
    except Exception:
        return "unknown"


@app.get("/version")
def version() -> dict:
    return {"api_version": API_VERSION, "semver": app.version, "git_sha": _git_sha()}


@app.get("/")
def root() -> dict:
    """Friendly entry point. The boot banner in the CLI displays the backend
    URL with rich's [underline] markup; iTerm2/Terminal auto-link it as
    clickable, and a user landing here without this route hits FastAPI's
    default 404 (`{"detail":"Not Found"}`). 0.1.1 surfaces the actual
    entry points instead so the URL is useful."""
    return {
        "name": "FinanceBench RAG Agent API",
        "api_version": API_VERSION,
        "semver": app.version,
        "try": {
            "health": f"/v{API_VERSION}/health",
            "version": "/version",
            "openapi_docs": "/docs",
            "openapi_redoc": "/redoc",
        },
        "cli": "pip install financebench-rag-agent",
        "repo": "https://github.com/Rishabhmannu/financebench-rag-agent",
    }


_routers = [health, auth, chat, ingest, hitl, admin, threads, documents, approvals]

# Canonical: /v1-prefixed routes. CLI declares it speaks v1.
for r in _routers:
    app.include_router(r.router, prefix=f"/v{API_VERSION}")

# Deprecated unprefixed aliases — kept for one minor version so the Sprint 9
# web frontend (which calls /auth/login etc.) keeps working during transition.
for r in _routers:
    app.include_router(r.router)
