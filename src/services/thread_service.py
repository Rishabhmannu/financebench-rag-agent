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
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def ts_from_checkpoint_id(cid: str | None) -> str | None:
    """Decode a langgraph-checkpoint UUIDv6 into an ISO-8601 UTC timestamp.

    langgraph-checkpoint-postgres uses UUIDv6 for checkpoint_id. The first
    60 bits encode the creation time as 100ns intervals since the Gregorian
    epoch (1582-10-15). Returns None if cid is None, malformed, or not v6.
    Cheap (pure arithmetic, no I/O) so safe to call per row.

    Bug A + Track 2 (audit): exposed so list/show endpoints can return a
    human-readable last_activity_at without a separate timestamp column.
    """
    if not cid:
        return None
    try:
        h = cid.replace("-", "")
        if len(h) != 32:
            return None
        time_high = int(h[0:8], 16)
        time_mid = int(h[8:12], 16)
        vh = int(h[12:16], 16)
        if ((vh >> 12) & 0xF) != 6:
            return None
        time_low = vh & 0xFFF
        ts_100ns_since_1582 = (time_high << 28) | (time_mid << 12) | time_low
        unix_100ns = ts_100ns_since_1582 - 122192928000000000  # 1582-10-15 → 1970-01-01 in 100ns units
        return datetime.fromtimestamp(unix_100ns / 10_000_000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


_LIST_SQL = """
SELECT
    c.thread_id,
    MIN(c.checkpoint_id) AS first_checkpoint_id,
    MAX(c.checkpoint_id) AS last_checkpoint_id,
    COUNT(*) AS checkpoint_count,
    -- Owner metadata is identical for every checkpoint of a given thread
    -- (we write it at chat-route time and never mutate). MIN picks one.
    MIN(c.metadata->>'user_id') AS user_id,
    MIN(c.metadata->>'name') AS name,
    MIN(c.metadata->>'role') AS role,
    MIN(c.metadata->>'department') AS department
FROM checkpoints c
WHERE c.metadata->>'user_id' = %s
GROUP BY c.thread_id
ORDER BY MAX(c.checkpoint_id) DESC
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

    Track 2 (audit): each row now includes owner identity (user_id, name,
    role, department — all derived from checkpoint metadata at no extra cost)
    plus decoded created_at / last_activity_at timestamps. The CLI consumes
    these to surface "owner | role | dept | last activity" columns instead
    of the previous flat "thread_id | title | checkpoints" view.
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
            "first_checkpoint_id": r[1],
            "last_checkpoint_id": r[2],
            "checkpoint_count": r[3],
            "user_id": r[4],
            "name": r[5],
            "role": r[6],
            "department": r[7],
            "created_at": ts_from_checkpoint_id(r[1]),
            "last_activity_at": ts_from_checkpoint_id(r[2]),
        }
        for r in rows
    ], int(total)


_LIST_ALL_PAGED_SQL = """
SELECT
    c.thread_id,
    MIN(c.checkpoint_id) AS first_checkpoint_id,
    MAX(c.checkpoint_id) AS last_checkpoint_id,
    COUNT(*) AS checkpoint_count,
    MIN(c.metadata->>'user_id') AS user_id,
    MIN(c.metadata->>'name') AS name,
    MIN(c.metadata->>'role') AS role,
    MIN(c.metadata->>'department') AS department
FROM checkpoints c
GROUP BY c.thread_id
ORDER BY MAX(c.checkpoint_id) DESC
LIMIT %s OFFSET %s
"""

_COUNT_ALL_SQL = "SELECT COUNT(DISTINCT thread_id) FROM checkpoints"


async def list_all_threads_paged(
    pool, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """Admin-only: paginated list of EVERY thread (cross-user), enriched with
    owner metadata + decoded timestamps. Same row shape as
    `list_threads_for_user` so the route handler can treat both uniformly.

    Bug A (audit): the existing `list_all_threads` was unpaginated and only
    used by the approvals inbox. Admin's `GET /threads` needs the same
    enrichment + pagination as user-scoped listing.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_LIST_ALL_PAGED_SQL, (limit, offset))
            rows = await cur.fetchall()
            await cur.execute(_COUNT_ALL_SQL)
            (total,) = await cur.fetchone()

    return [
        {
            "thread_id": r[0],
            "first_checkpoint_id": r[1],
            "last_checkpoint_id": r[2],
            "checkpoint_count": r[3],
            "user_id": r[4],
            "name": r[5],
            "role": r[6],
            "department": r[7],
            "created_at": ts_from_checkpoint_id(r[1]),
            "last_activity_at": ts_from_checkpoint_id(r[2]),
        }
        for r in rows
    ], int(total)


_HITL_DECISION_COUNT_SQL = """
SELECT COUNT(*) FROM checkpoints
WHERE thread_id = %s
  AND (metadata->>'source' = 'update'
       OR metadata::text LIKE '%%human_decision%%')
"""


async def count_hitl_decisions_on_thread(pool, thread_id: str) -> int:
    """Best-effort count of HITL approve/reject events on this thread.

    Track 2 (audit): the approvals inbox detail panel uses this so the
    approver can see whether they're looking at a fresh request vs. one
    that's been bounced before."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_HITL_DECISION_COUNT_SQL, (thread_id,))
            (count,) = await cur.fetchone()
    return int(count or 0)


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


_LIST_ALL_SQL = """
SELECT
    c.thread_id,
    MIN(c.metadata->>'user_id') AS user_id,
    MIN(c.metadata->>'name') AS name,
    MIN(c.metadata->>'department') AS department,
    MIN(c.metadata->>'role') AS role,
    MAX(c.checkpoint_id) AS latest_checkpoint_id
FROM checkpoints c
GROUP BY c.thread_id
ORDER BY MAX(c.checkpoint_id) DESC
LIMIT %s
"""

_OWNERSHIP_ROLE_SQL = """
SELECT metadata->>'user_id', metadata->>'role', metadata->>'name', metadata->>'department'
FROM checkpoints
WHERE thread_id = %s
LIMIT 1
"""


async def list_all_threads(pool, limit: int = 200) -> list[dict[str, Any]]:
    """List all threads (any user) with their {thread_id, user_id, name,
    department, role} metadata. Used by the approvals inbox to find pending
    HITL interrupts submitted by other users."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_LIST_ALL_SQL, (limit,))
            rows = await cur.fetchall()
    return [
        {
            "thread_id": r[0],
            "user_id": r[1],
            "name": r[2],
            "department": r[3],
            "role": r[4],
            "latest_checkpoint_id": r[5],
        }
        for r in rows
    ]


async def get_thread_owner_role(pool, thread_id: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (user_id, role, name, department) for the thread's creator.
    All None if not found. Older callers using a 2-tuple unpacking should
    expand to 4-tuple after this commit."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_OWNERSHIP_ROLE_SQL, (thread_id,))
            row = await cur.fetchone()
    if row is None:
        return None, None, None, None
    return row[0], row[1], row[2], row[3]


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
