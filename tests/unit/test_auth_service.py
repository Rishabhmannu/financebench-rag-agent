"""Unit tests for the auth service (create_token / decode_token)."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from src.config.settings import settings
from src.services.auth_service import create_token, decode_token


# ── create_token returns a non-empty string ───────────────────────────────

def test_create_token_returns_nonempty_string():
    token = create_token(user_id="u1", name="Alice", role="finance", department="treasury")

    assert isinstance(token, str)
    assert len(token) > 0


# ── decode_token returns User with correct fields ─────────────────────────

def test_decode_token_returns_user_with_correct_fields():
    token = create_token(user_id="u42", name="Bob Smith", role="analyst", department="research")
    user = decode_token(token)

    assert user.user_id == "u42"
    assert user.name == "Bob Smith"
    assert user.role == "analyst"
    assert user.department == "research"


# ── Roundtrip preserves user_id, name, role ───────────────────────────────

def test_roundtrip_preserves_identity():
    cases = [
        {"user_id": "admin-1", "name": "Admin User", "role": "admin", "department": "IT"},
        {"user_id": "hr-99", "name": "HR Person", "role": "hr", "department": "people"},
        {"user_id": "c-lvl", "name": "CEO", "role": "c_level", "department": ""},
    ]

    for case in cases:
        token = create_token(**case)
        user = decode_token(token)

        assert user.user_id == case["user_id"]
        assert user.name == case["name"]
        assert user.role == case["role"]
        assert user.department == case["department"]


# ── Expired token raises HTTPException ────────────────────────────────────

def test_expired_token_raises_http_exception():
    # Manually craft a token that expired one hour ago
    payload = {
        "sub": "expired-user",
        "name": "Old Token",
        "role": "analyst",
        "department": "",
        "iat": datetime.now(timezone.utc) - timedelta(hours=25),
        "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        "iss": "rag-agent-auth",
    }
    expired_token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

    with pytest.raises(HTTPException) as exc_info:
        decode_token(expired_token)

    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower()


# ── Invalid token string raises HTTPException ─────────────────────────────

def test_invalid_token_raises_http_exception():
    with pytest.raises(HTTPException) as exc_info:
        decode_token("not-a-jwt-at-all")

    assert exc_info.value.status_code == 401
    assert "invalid" in exc_info.value.detail.lower()


# ── Token signed with wrong secret raises HTTPException ───────────────────

def test_wrong_secret_raises_http_exception():
    payload = {
        "sub": "user-wrong-key",
        "name": "Wrong Key",
        "role": "finance",
        "department": "",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "iss": "rag-agent-auth",
    }
    bad_token = jwt.encode(payload, "completely-wrong-secret", algorithm="HS256")

    with pytest.raises(HTTPException) as exc_info:
        decode_token(bad_token)

    assert exc_info.value.status_code == 401
    assert "invalid" in exc_info.value.detail.lower()
