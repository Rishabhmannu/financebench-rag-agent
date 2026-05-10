"""Thread enumeration service over the LangGraph PostgresSaver checkpoint store.

LangGraph's AsyncPostgresSaver exposes per-thread read APIs (``aget_state``,
``aget_state_history``) but no public "list all threads matching X" query.
Sprint 9's sidebar history requires that listing, so we drop down to a raw
SQL query against the ``checkpoints`` table.

Our ``src/api/routes/chat.py`` populates the LangGraph metadata blob with
``{"user_id", "role", "thread_id", "hitl_enabled"}`` (see RunnableConfig
construction at chat.py:89). The Postgres-saver persists that blob into
``checkpoints.metadata`` (JSONB), so we can filter by
``metadata->>'user_id' = $user_id`` to enumerate a user's threads.

For thread title we pick the first user message captured in the earliest
checkpoint for the thread — the chat input.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


_LIST_SQL = """
SELECT
    c.thread_id,
    MIN(c.created_at_estimate) AS created_at,
    MAX(c.created_at_estimate) AS updated_at,
    COUNT(*) AS checkpoint_count
FROM (
    SELECT
        thread_id,
        -- `checkpoint_id` is sortable timestamp-like uuid; map to a synthetic
        -- timestamp ordering field. We don't have a real timestamp column so
        -- order by checkpoint_id as a proxy.
        checkpoint_id AS created_at_estimate,
        metadata
    FROM checkpoints
    WHERE metadata->>'user_id' = %s
) c
GROUP BY c.thread_id
ORDER BY MAX(c.created_at_estimate) DESC
LIMIT %s OFFSET %s
"""

_COUNT_SQL = """
SELECT COUNT(DISTINCT thread_id) FROM checkpoints WHERE metadata->>'user_id' = %s
"""

_OWNERSHIP_SQL = """
SELECT metadata->>'user_id' FROM checkpoints
WHERE thread_id = %s
LIMIT 1
"""

_DELETE_SQL = """
DELETE FROM checkpoints WHERE thread_id = %s;
DELETE FROM checkpoint_writes WHERE thread_id = %s;
DELETE FROM checkpoint_blobs WHERE thread_id = %s;
"""


async def list_threads_for_user(
    pool, user_id: str, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total_count) for a user's threads, newest first.

    `pool` is the `psycopg_pool.AsyncConnectionPool` stored on app.state by
    `src/api/main.py` lifespan setup.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_LIST_SQL, (user_id, limit, offset))
            rows = await cur.fetchall()
            await cur.execute(_COUNT_SQL, (user_id,))
            (total,) = await cur.fetchone()

    return [
        {
            "thread_id": r[0],
            # checkpoint_id sorts lexically by recency; expose it as the
            # client-side ordering key. Real timestamps live in the
            # checkpoint blob itself but parsing them is expensive.
            "created_at_estimate": r[1],
            "updated_at_estimate": r[2],
            "checkpoint_count": r[3],
        }
        for r in rows
    ], int(total)


async def get_thread_owner(pool, thread_id: str) -> str | None:
    """Return the user_id that created this thread, or None if not found.

    Used by route handlers to enforce ownership before returning thread
    contents — cross-user access returns 403, not 404, because the thread
    *exists* but is not the caller's.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_OWNERSHIP_SQL, (thread_id,))
            row = await cur.fetchone()
    if row is None:
        return None
    return row[0]


async def delete_thread(pool, thread_id: str) -> int:
    """Delete every checkpoint row associated with this thread.

    Returns the total number of rows removed (across all three checkpoint
    tables). Caller is responsible for the ownership check.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
            n1 = cur.rowcount
            await cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,))
            n2 = cur.rowcount
            await cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
            n3 = cur.rowcount
        await conn.commit()
    return int((n1 or 0) + (n2 or 0) + (n3 or 0))
