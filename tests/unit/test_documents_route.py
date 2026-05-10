"""Unit tests for GET /documents/{filename}.

Path-traversal guard, RBAC, and 404 paths are stubbed via patches on
the Qdrant lookup helper so the test runs without a live Qdrant.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.services.auth_service import create_token


def _token(role: str) -> str:
    return create_token(user_id=role, name=f"Test {role}", role=role, department="x")


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Point DOCUMENTS_ROOT at an empty tmp dir so we control the FS surface
    monkeypatch.setattr("src.config.settings.settings.DOCUMENTS_ROOT", str(tmp_path))
    return TestClient(app), tmp_path


def test_path_traversal_blocked(client):
    c, _ = client
    for bad in ["../etc/passwd", "..\\evil.pdf", "/etc/passwd", ".secret.pdf"]:
        resp = c.get(f"/documents/{bad}", headers={"Authorization": f"Bearer {_token('admin')}"})
        # Either rejected as bad request before we hit RBAC, or 404
        assert resp.status_code in (400, 404), f"{bad} should not be 200"


def test_non_pdf_rejected(client):
    c, _ = client
    resp = c.get("/documents/file.txt", headers={"Authorization": f"Bearer {_token('admin')}"})
    assert resp.status_code == 400


def test_unauth_blocked(client):
    c, _ = client
    resp = c.get("/documents/file.pdf")
    assert resp.status_code in (401, 403)


@patch("src.api.routes.documents._lookup_document_meta")
def test_404_when_not_indexed(mock_lookup, client):
    c, _ = client
    mock_lookup.return_value = None
    resp = c.get("/documents/unknown.pdf", headers={"Authorization": f"Bearer {_token('admin')}"})
    assert resp.status_code == 404


@patch("src.api.routes.documents._lookup_document_meta")
def test_403_for_role_lacking_doc_type(mock_lookup, client):
    """Analyst sees only doc_type=10k/public. A confidential board report should 403."""
    c, _ = client
    mock_lookup.return_value = {"doc_type": "board_report", "confidentiality": "confidential"}
    resp = c.get(
        "/documents/board_minutes_2023.pdf",
        headers={"Authorization": f"Bearer {_token('analyst')}"},
    )
    assert resp.status_code == 403


@patch("src.api.routes.documents._lookup_document_meta")
def test_admin_wildcard_allows_any_doc(mock_lookup, client):
    """Admin has ["*"] for both axes — should pass RBAC; 404 because file isn't on disk."""
    c, tmp = client
    mock_lookup.return_value = {"doc_type": "board_report", "confidentiality": "confidential"}
    resp = c.get(
        "/documents/board_minutes_2023.pdf",
        headers={"Authorization": f"Bearer {_token('admin')}"},
    )
    # RBAC passed but file isn't on disk → 404 "Document file not on disk"
    assert resp.status_code == 404
    assert "disk" in resp.json()["detail"].lower()


@patch("src.api.routes.documents._lookup_document_meta")
def test_200_when_rbac_ok_and_file_present(mock_lookup, client):
    c, tmp = client
    mock_lookup.return_value = {"doc_type": "10k", "confidentiality": "public"}
    # Write a real PDF placeholder so FileResponse can stream it
    (tmp / "report.pdf").write_bytes(b"%PDF-1.4\nminimal stub\n%%EOF")
    resp = c.get(
        "/documents/report.pdf",
        headers={"Authorization": f"Bearer {_token('analyst')}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "inline" in resp.headers["content-disposition"]
