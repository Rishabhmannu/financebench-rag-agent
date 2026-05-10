"""Alembic environment.

We bypass alembic's default `sqlalchemy.url = ...` lookup and instead pull
the URL straight from `src/config/settings.py`. This keeps a single source
of truth for the Postgres credentials and lets the same alembic invocation
work locally, inside docker-compose, and against the production cluster.

Driver: alembic talks to Postgres through SQLAlchemy's `psycopg2`/`psycopg`
driver synchronously. The rag-agent uses `psycopg[binary]` (async) at
runtime; alembic's sync usage here is independent.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.config.settings import settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Force the URL from our settings module (psycopg2 sync driver for alembic;
# the app uses psycopg async at runtime). Same DB, just different driver.
config.set_main_option("sqlalchemy.url", f"postgresql+psycopg://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}")


def run_migrations_offline() -> None:
    """Generate SQL without connecting — useful for CI dry-runs."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live DB."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
