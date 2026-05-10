"""Unit tests for the auth routes — login + /auth/me.

Sprint 9.0 extended TokenResponse with user_id/name/department for the
frontend handoff, and added GET /auth/me. These tests pin the contract.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.services.auth_service import create_token


@pytest.fixture
def client():
    return TestClient(app)


# ── POST /auth/login ──────────────────────────────────────────────────────

def test_login_returns_full_user_tuple(client):
    """The frontend renders the header from this response — every field
    must be present and match the expected user."""
    resp = client.post("/auth/login", json={"username": "finance", "password": "finance123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "finance"
    assert body["name"] == "Test Finance"
    assert body["role"] == "finance"
    assert body["department"] == "FP&A"
    assert body["access_token"]
    assert body["token_type"] == "bearer"


def test_login_rejects_bad_password(client):
    resp = client.post("/auth/login", json={"username": "finance", "password": "wrong"})
    assert resp.status_code == 401


def test_login_rejects_unknown_user(client):
    resp = client.post("/auth/login", json={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


# ── GET /auth/me ──────────────────────────────────────────────────────────

def _token(role: str = "finance", user_id: str = "finance", department: str = "FP&A") -> str:
    return create_token(user_id=user_id, name=f"Test {role.title()}", role=role, department=department)


def test_me_returns_identity_and_permissions(client):
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "finance"
    assert body["role"] == "finance"
    perms = body["permissions"]
    assert "10k" in perms["allowed_doc_types"]
    assert perms["max_results"] >= 1
    # finance has a HITL threshold; analyst doesn't. Both are exposed.
    assert "requires_hitl_above" in perms


def test_me_requires_auth(client):
    resp = client.get("/auth/me")
    assert resp.status_code in (401, 403)


def test_me_rejects_invalid_token(client):
    resp = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
