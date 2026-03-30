import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import AsyncConnectionPool

from src.api.routes import auth, chat, health, hitl, ingest
from src.config.settings import settings

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL), format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup validation
    if not settings.OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY is not set! Embeddings and generation will fail.")
    if not settings.GROQ_API_KEY:
        logger.warning("GROQ_API_KEY is not set. Router/grader will fall back to OpenAI.")
    if not settings.LANGCHAIN_API_KEY:
        logger.warning("LANGCHAIN_API_KEY is not set. LangSmith tracing will be disabled.")

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
    title="RAG Agent API",
    description="Enterprise Financial Document Q&A with RBAC, Guardrails, and Multi-Agent Pipeline",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(ingest.router)
app.include_router(hitl.router)
