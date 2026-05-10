"""Unit tests for /admin/users + /admin/roles CRUD.

The roles CRUD path is stubbed via patches against roles_service so the
test runs hermetically — no Postgres dependency.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.services.auth_service import create_token


def _admin_token() -> str:
    return create_token(user_id="admin", name="Test Admin", role="admin", department="IT")


def _analyst_token() -> str:
    return create_token(user_id="analyst", name="Test Analyst", role="analyst", department="Research")


@pytest.fixture
def client():
    return TestClient(app)


# ── /admin/users ──────────────────────────────────────────────────────────

def test_users_list_admin_only(client):
    resp = client.get("/admin/users")
    assert resp.status_code in (401, 403)

    resp = client.get("/admin/users", headers={"Authorization": f"Bearer {_analyst_token()}"})
    assert resp.status_code == 403


def test_users_list_returns_dev_users(client):
    resp = client.get("/admin/users", headers={"Authorization": f"Bearer {_admin_token()}"})
    assert resp.status_code == 200
    users = resp.json()["users"]
    usernames = {u["username"] for u in users}
    assert {"analyst", "finance", "hr", "clevel", "admin"} == usernames
    for u in users:
        assert "name" in u and "role" in u and "department" in u


# ── /admin/roles GET ──────────────────────────────────────────────────────

@patch("src.services.roles_service.list_roles")
def test_roles_list_admin_only(mock_list, client):
    mock_list.return_value = [
        {"name": "analyst", "allowed_doc_types": ["10k"], "allowed_confidentiality": ["public"],
         "max_results": 5, "requires_hitl_above": None, "is_system": True},
    ]
    resp = client.get("/admin/roles", headers={"Authorization": f"Bearer {_analyst_token()}"})
    assert resp.status_code == 403

    resp = client.get("/admin/roles", headers={"Authorization": f"Bearer {_admin_token()}"})
    assert resp.status_code == 200
    assert resp.json()["roles"][0]["name"] == "analyst"


# ── /admin/roles POST ─────────────────────────────────────────────────────

@patch("src.services.roles_service.get_role")
@patch("src.services.roles_service.create_role")
def test_create_role_returns_201(mock_create, mock_get, client):
    mock_get.return_value = None
    mock_create.return_value = {
        "name": "auditor", "allowed_doc_types": ["10k", "invoice"],
        "allowed_confidentiality": ["public", "internal"], "max_results": 8,
        "requires_hitl_above": None, "is_system": False,
    }
    resp = client.post(
        "/admin/roles",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={
            "name": "auditor",
            "allowed_doc_types": ["10k", "invoice"],
            "allowed_confidentiality": ["public", "internal"],
            "max_results": 8,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "auditor"
    mock_create.assert_called_once()


@patch("src.services.roles_service.get_role")
def test_create_role_rejects_duplicate(mock_get, client):
    mock_get.return_value = {"name": "finance", "is_system": True}
    resp = client.post(
        "/admin/roles",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={
            "name": "finance",
            "allowed_doc_types": ["10k"],
            "allowed_confidentiality": ["public"],
        },
    )
    assert resp.status_code == 409


# ── /admin/roles PATCH ────────────────────────────────────────────────────

@patch("src.services.roles_service.get_role")
@patch("src.services.roles_service.update_role")
def test_update_role_partial_patch(mock_update, mock_get, client):
    mock_get.return_value = {"name": "finance", "is_system": True}
    mock_update.return_value = {
        "name": "finance", "allowed_doc_types": ["10k", "invoice"],
        "allowed_confidentiality": ["public", "internal"], "max_results": 20,
        "requires_hitl_above": 100_000, "is_system": True,
    }
    resp = client.patch(
        "/admin/roles/finance",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"max_results": 20},
    )
    assert resp.status_code == 200
    assert resp.json()["max_results"] == 20
    # Verify only the passed field was patched (others not in kwargs)
    call_kwargs = mock_update.call_args
    assert "max_results" in call_kwargs[0][1]
    assert "allowed_doc_types" not in call_kwargs[0][1]


@patch("src.services.roles_service.get_role")
def test_update_role_404_when_missing(mock_get, client):
    mock_get.return_value = None
    resp = client.patch(
        "/admin/roles/nope",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"max_results": 5},
    )
    assert resp.status_code == 404


# ── /admin/roles DELETE ───────────────────────────────────────────────────

@patch("src.services.roles_service.delete_role")
def test_delete_role_204_on_success(mock_delete, client):
    mock_delete.return_value = "deleted"
    resp = client.delete("/admin/roles/auditor", headers={"Authorization": f"Bearer {_admin_token()}"})
    assert resp.status_code == 204


@patch("src.services.roles_service.delete_role")
def test_delete_role_409_for_system(mock_delete, client):
    mock_delete.return_value = "blocked_system"
    resp = client.delete("/admin/roles/admin", headers={"Authorization": f"Bearer {_admin_token()}"})
    assert resp.status_code == 409
    assert "system" in resp.json()["detail"].lower()


@patch("src.services.roles_service.delete_role")
def test_delete_role_404_when_missing(mock_delete, client):
    mock_delete.return_value = "not_found"
    resp = client.delete("/admin/roles/ghost", headers={"Authorization": f"Bearer {_admin_token()}"})
    assert resp.status_code == 404


def test_delete_role_non_admin_forbidden(client):
    resp = client.delete("/admin/roles/analyst", headers={"Authorization": f"Bearer {_analyst_token()}"})
    assert resp.status_code == 403
