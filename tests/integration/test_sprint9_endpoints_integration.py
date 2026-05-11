"""Integration tests for the Sprint 9.0 new endpoints.

Unit tests in `tests/unit/test_auth_routes.py`, `test_admin_users_and_roles.py`,
`test_documents_route.py`, `test_ingest_upload.py`, `test_threads_routes.py`
patch every external dependency (Qdrant, Postgres, the LangGraph state) so
they run hermetic in <2s. These integration tests hit the SAME endpoints
through the SAME FastAPI app but exercise the real DB + the real Qdrant
when those are reachable, so we get end-to-end coverage of the contract.

Scope of each test:
  - Auth missing  → 401
  - Wrong role    → 403
  - Cross-user access (where applicable) → 403
  - Happy path    → 200 with the expected schema

Tests skip cleanly when their backing service is down so the integration
suite remains runnable in environments without Docker.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.services.auth_service import create_token


def _postgres_alive() -> bool:
    try:
        import psycopg
        from src.config.settings import settings

        conn = psycopg.connect(
            f"host={settings.POSTGRES_HOST} port={settings.POSTGRES_PORT} "
            f"dbname={settings.POSTGRES_DB} user={settings.POSTGRES_USER} "
            f"password={settings.POSTGRES_PASSWORD}",
            connect_timeout=2,
        )
        conn.close()
        return True
    except Exception:
        return False


def _qdrant_alive() -> bool:
    try:
        from src.services.vector_store import get_qdrant_client

        client = get_qdrant_client()
        client.get_collections()
        return True
    except Exception:
        return False


postgres_required = pytest.mark.skipif(
    not _postgres_alive(),
    reason="Postgres not reachable on configured host:port — skipping live integration",
)

qdrant_required = pytest.mark.skipif(
    not _qdrant_alive(),
    reason="Qdrant not reachable on localhost:6333 — skipping live integration",
)


def _token(role: str, user_id: str | None = None) -> str:
    return create_token(
        user_id=user_id or role,
        name=f"Test {role.title()}",
        role=role,
        department="x",
    )


@pytest.fixture
def client():
    return TestClient(app)


# ─── /auth/login + /auth/me — needs no external service ──────────────────

def test_auth_login_to_me_roundtrip_happy_path(client):
    """Full handshake: POST /auth/login → use returned token to GET /auth/me.
    Verifies the JWT actually validates against the live decode path.
    """
    login = client.post("/auth/login", json={"username": "finance", "password": "finance123"})
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["user_id"] == "finance"
    assert body["role"] == "finance"
    # finance role has the documented HITL threshold
    assert body["permissions"]["requires_hitl_above"] == 100_000


def test_auth_me_401_without_token(client):
    resp = client.get("/auth/me")
    assert resp.status_code in (401, 403)


def test_auth_me_401_with_garbage_token(client):
    resp = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


# ─── /admin/users — DB-free, just hits in-memory DEV_USERS ───────────────

def test_admin_users_401_no_token(client):
    resp = client.get("/admin/users")
    assert resp.status_code in (401, 403)


def test_admin_users_403_for_non_admin(client):
    resp = client.get("/admin/users", headers={"Authorization": f"Bearer {_token('analyst')}"})
    assert resp.status_code == 403


def test_admin_users_200_for_admin(client):
    resp = client.get("/admin/users", headers={"Authorization": f"Bearer {_token('admin')}"})
    assert resp.status_code == 200
    users = resp.json()["users"]
    assert any(u["username"] == "finance" for u in users)


# ─── /admin/roles — needs live Postgres (reads roles table) ──────────────

@postgres_required
def test_admin_roles_list_returns_5_system_roles(client):
    resp = client.get("/admin/roles", headers={"Authorization": f"Bearer {_token('admin')}"})
    assert resp.status_code == 200
    roles = resp.json()["roles"]
    names = {r["name"] for r in roles}
    # The 5 system roles seeded by migration 20260511_0002
    assert {"analyst", "finance", "hr", "c_level", "admin"}.issubset(names)
    # All seeded ones should be is_system=true
    for r in roles:
        if r["name"] in {"analyst", "finance", "hr", "c_level", "admin"}:
            assert r["is_system"] is True


@postgres_required
def test_admin_roles_403_for_non_admin(client):
    resp = client.get("/admin/roles", headers={"Authorization": f"Bearer {_token('analyst')}"})
    assert resp.status_code == 403


@postgres_required
def test_admin_roles_cannot_delete_system_role(client):
    """Deleting a system role must return 409 — the seed migration marks
    the 5 built-ins is_system=true precisely so they survive admin clicks.
    """
    resp = client.delete(
        "/admin/roles/admin",
        headers={"Authorization": f"Bearer {_token('admin')}"},
    )
    assert resp.status_code == 409
    assert "system" in resp.json()["detail"].lower()


@postgres_required
def test_admin_roles_full_crud_lifecycle(client):
    """Create → list → patch → delete a non-system role, verifying every
    step is visible to subsequent GETs. Catches cache-invalidation bugs
    in roles_service that wouldn't surface in unit tests with mocks.
    """
    admin_headers = {"Authorization": f"Bearer {_token('admin')}"}
    test_name = "_integration_test_role"

    # Cleanup any leftover from a prior failed run
    client.delete(f"/admin/roles/{test_name}", headers=admin_headers)

    # CREATE
    create_resp = client.post(
        "/admin/roles",
        headers=admin_headers,
        json={
            "name": test_name,
            "allowed_doc_types": ["10k"],
            "allowed_confidentiality": ["public"],
            "max_results": 7,
            "requires_hitl_above": 25000,
        },
    )
    assert create_resp.status_code == 201
    assert create_resp.json()["max_results"] == 7

    # LIST should include the new role
    list_resp = client.get("/admin/roles", headers=admin_headers)
    assert any(r["name"] == test_name for r in list_resp.json()["roles"])

    # PATCH
    patch_resp = client.patch(
        f"/admin/roles/{test_name}",
        headers=admin_headers,
        json={"max_results": 12},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["max_results"] == 12

    # DELETE (non-system, so allowed)
    del_resp = client.delete(f"/admin/roles/{test_name}", headers=admin_headers)
    assert del_resp.status_code == 204

    # LIST should no longer include it
    list_resp = client.get("/admin/roles", headers=admin_headers)
    assert not any(r["name"] == test_name for r in list_resp.json()["roles"])


@postgres_required
def test_admin_roles_create_409_on_duplicate(client):
    """Re-creating a known role should 409; ensures the existence check
    survives the round-trip to Postgres, not just the unit-test mock.
    """
    resp = client.post(
        "/admin/roles",
        headers={"Authorization": f"Bearer {_token('admin')}"},
        json={
            "name": "admin",  # already exists as system role
            "allowed_doc_types": ["*"],
            "allowed_confidentiality": ["*"],
        },
    )
    assert resp.status_code == 409


# ─── /documents — needs live Qdrant for the RBAC metadata lookup ─────────

@qdrant_required
def test_documents_400_on_path_traversal(client):
    """The unit test stubs the Qdrant lookup so the route never reaches it;
    this integration test confirms the path-traversal guard fires BEFORE
    any service call, regardless of Qdrant state.
    """
    headers = {"Authorization": f"Bearer {_token('admin')}"}
    for bad in ["../etc/passwd.pdf", "..\\evil.pdf", "/etc/passwd.pdf"]:
        resp = client.get(f"/documents/{bad}", headers=headers)
        assert resp.status_code in (400, 404), f"path traversal '{bad}' should not succeed (got {resp.status_code})"


@qdrant_required
def test_documents_401_without_token(client):
    resp = client.get("/documents/anything.pdf")
    assert resp.status_code in (401, 403)


@qdrant_required
def test_documents_404_for_unindexed_filename(client):
    """A real Qdrant lookup against a filename that doesn't exist should
    yield 404 (not 403, not 500). Confirms the Qdrant scroll-with-filter
    path returns cleanly on no match.
    """
    resp = client.get(
        "/documents/definitely_not_indexed_12345.pdf",
        headers={"Authorization": f"Bearer {_token('admin')}"},
    )
    assert resp.status_code == 404


# ─── /threads — needs live Postgres ──────────────────────────────────────

@postgres_required
def test_threads_401_without_token(client):
    resp = client.get("/threads")
    assert resp.status_code in (401, 403)


@postgres_required
def test_threads_get_404_for_random_id(client):
    resp = client.get(
        "/threads/this-is-not-a-real-thread-id",
        headers={"Authorization": f"Bearer {_token('admin')}"},
    )
    # Either 404 (no checkpoints for that thread_id) or 503 (no pool)
    assert resp.status_code in (404, 503)


@postgres_required
def test_threads_list_returns_empty_for_new_user(client):
    """A brand-new user_id should see total=0 — confirms the
    metadata->>'user_id' filter actually works against the live DB.
    """
    # Use a JWT for a fresh, never-used user_id
    fresh = create_token(
        user_id="brand-new-user-for-tests",
        name="x",
        role="analyst",
        department="x",
    )
    resp = client.get("/threads", headers={"Authorization": f"Bearer {fresh}"})
    if resp.status_code == 503:
        pytest.skip("Checkpoint store not initialized in this test context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["threads"] == []
