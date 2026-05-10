"""DB-backed role / permissions lookup with in-memory fallback.

Sprint 9.0 promotes RBAC role definitions from a hardcoded dict in
``src/config/rbac_config.py`` to a Postgres-backed ``roles`` table so the
admin panel can edit them live without a redeploy.

Why a fallback? Two reasons:

  1. Bootstrap order — the very first ``alembic upgrade`` runs *after* the
     app process imports modules. Graph nodes (rbac_gate, retrieval) call
     ``get_permissions`` during construction, so until the DB is ready
     we'd otherwise crash. The static dict in ``rbac_config`` covers that
     window.

  2. Test isolation — unit tests don't want to spin up Postgres just to
     read RBAC config. The in-memory path keeps the test suite hermetic.

The lookup is cached in-process for 30s so we don't hammer Postgres on
every retrieval call. Cache invalidation: the ``/admin/roles`` CRUD
endpoints call ``invalidate_cache()`` after writes.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import psycopg

from src.config import rbac_config
from src.config.settings import settings

logger = logging.getLogger(__name__)

# Process-local cache: {role_name -> permissions dict}, repopulated every
# CACHE_TTL_SECONDS. Cheap memo for the hot path; the admin CRUD flushes
# it explicitly so admin edits propagate instantly.
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOADED_AT: float = 0.0
CACHE_TTL_SECONDS = 30.0


def _sync_conninfo() -> str:
    """Sync (psycopg3) connection string — the rest of the app uses async,
    but ``get_permissions`` is called from sync code paths (graph nodes
    that don't await), so we use a one-shot sync connection here.
    """
    return (
        f"host={settings.POSTGRES_HOST} port={settings.POSTGRES_PORT} "
        f"dbname={settings.POSTGRES_DB} user={settings.POSTGRES_USER} "
        f"password={settings.POSTGRES_PASSWORD}"
    )


def _load_all_from_db() -> dict[str, dict[str, Any]]:
    """Fetch every role into a {name -> permissions} dict. Returns empty
    dict on any DB error so callers fall through to the static config.
    """
    sql = (
        "SELECT name, allowed_doc_types, allowed_confidentiality, "
        "max_results, requires_hitl_above, is_system "
        "FROM roles"
    )
    try:
        with psycopg.connect(_sync_conninfo(), connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
    except Exception as e:
        # Common during local bootstrap: DB not yet up, or migrations
        # haven't run. Fall back to the static config silently — full
        # error goes to debug log so it doesn't spam startup.
        logger.debug("roles_service DB read failed (falling back to static): %s", e)
        return {}

    return {
        name: {
            "allowed_doc_types": adt,
            "allowed_confidentiality": ac,
            "max_results": mr,
            "requires_hitl_above": rha,
            "is_system": is_system,
        }
        for (name, adt, ac, mr, rha, is_system) in rows
    }


def _refresh_if_stale() -> None:
    global _CACHE, _CACHE_LOADED_AT
    if time.monotonic() - _CACHE_LOADED_AT < CACHE_TTL_SECONDS and _CACHE:
        return
    fresh = _load_all_from_db()
    if fresh:
        _CACHE = fresh
        _CACHE_LOADED_AT = time.monotonic()


def invalidate_cache() -> None:
    """Force the next get_* call to re-read from Postgres. Called by the
    /admin/roles CRUD endpoints after every write.
    """
    global _CACHE, _CACHE_LOADED_AT
    _CACHE = {}
    _CACHE_LOADED_AT = 0.0


def get_permissions(role: str) -> dict[str, Any]:
    """Return the RBAC permissions dict for a role.

    Looks DB first (cached 30s) and falls back to ``rbac_config`` when:
      - the DB is unreachable, or
      - the role isn't found in the DB but exists in the static dict
        (e.g. during the bootstrap window before alembic runs).

    The shape matches ``rbac_config.get_permissions`` exactly so this is a
    drop-in. ``rbac_gate``/``retrieval`` call sites don't need changes.
    """
    _refresh_if_stale()
    db_row = _CACHE.get(role)
    if db_row is not None:
        # Strip the `is_system` field — graph code doesn't need it
        return {k: v for k, v in db_row.items() if k != "is_system"}
    # Fallback to the static dict directly (NOT rbac_config.get_permissions,
    # which now delegates back here — that would infinite-loop).
    return rbac_config.ROLE_PERMISSIONS.get(role, rbac_config.ROLE_PERMISSIONS["analyst"])


# ── CRUD helpers (used by `/admin/roles`) ────────────────────────────────

def list_roles() -> list[dict[str, Any]]:
    """Return every role record. Falls back to the static dict when DB
    is empty / unreachable so the admin UI never sees a blank state.
    """
    _refresh_if_stale()
    if _CACHE:
        return [{"name": name, **perms} for name, perms in _CACHE.items()]
    # Fallback: synthesize from the static config; mark all as system
    return [
        {"name": name, "is_system": True, **perms}
        for name, perms in rbac_config.ROLE_PERMISSIONS.items()
    ]


def get_role(name: str) -> dict[str, Any] | None:
    _refresh_if_stale()
    row = _CACHE.get(name)
    if row is None:
        return None
    return {"name": name, **row}


def create_role(role: dict[str, Any]) -> dict[str, Any]:
    sql = (
        "INSERT INTO roles (name, allowed_doc_types, allowed_confidentiality, "
        "max_results, requires_hitl_above, is_system) "
        "VALUES (%s, %s::jsonb, %s::jsonb, %s, %s, %s) "
        "RETURNING name, allowed_doc_types, allowed_confidentiality, "
        "max_results, requires_hitl_above, is_system"
    )
    import json
    with psycopg.connect(_sync_conninfo(), connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    role["name"],
                    json.dumps(role["allowed_doc_types"]),
                    json.dumps(role["allowed_confidentiality"]),
                    role.get("max_results", 10),
                    role.get("requires_hitl_above"),
                    bool(role.get("is_system", False)),
                ),
            )
            row = cur.fetchone()
        conn.commit()
    invalidate_cache()
    return _row_to_dict(row)


def update_role(name: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """Apply a partial patch — only fields present in `patch` are updated."""
    fields = {}
    for k in ("allowed_doc_types", "allowed_confidentiality", "max_results", "requires_hitl_above"):
        if k in patch:
            fields[k] = patch[k]
    if not fields:
        return get_role(name)

    import json
    sets = []
    params: list[Any] = []
    for k, v in fields.items():
        if k in ("allowed_doc_types", "allowed_confidentiality"):
            sets.append(f"{k} = %s::jsonb")
            params.append(json.dumps(v))
        else:
            sets.append(f"{k} = %s")
            params.append(v)
    sets.append("updated_at = NOW()")
    params.append(name)

    sql = (
        f"UPDATE roles SET {', '.join(sets)} WHERE name = %s "
        "RETURNING name, allowed_doc_types, allowed_confidentiality, "
        "max_results, requires_hitl_above, is_system"
    )
    with psycopg.connect(_sync_conninfo(), connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
    invalidate_cache()
    return _row_to_dict(row) if row else None


def delete_role(name: str) -> str:
    """Returns one of: "deleted", "not_found", "blocked_system"."""
    with psycopg.connect(_sync_conninfo(), connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT is_system FROM roles WHERE name = %s", (name,))
            row = cur.fetchone()
            if row is None:
                return "not_found"
            if row[0]:  # is_system
                return "blocked_system"
            cur.execute("DELETE FROM roles WHERE name = %s", (name,))
        conn.commit()
    invalidate_cache()
    return "deleted"


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "name": row[0],
        "allowed_doc_types": row[1],
        "allowed_confidentiality": row[2],
        "max_results": row[3],
        "requires_hitl_above": row[4],
        "is_system": row[5],
    }
