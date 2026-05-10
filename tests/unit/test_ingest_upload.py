"""Unit tests for POST /ingest/upload (multipart PDF upload).

ingest_file is patched so we don't run the real Docling/embedding/Qdrant
path; we just verify the route saves files to DOCUMENTS_ROOT, calls
ingest_file with the right kwargs, and reports per-file results.
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
    return create_token(user_id="analyst", name="Test Analyst", role="analyst", department="x")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.settings.settings.DOCUMENTS_ROOT", str(tmp_path))
    return TestClient(app), tmp_path


def test_upload_requires_admin(client):
    c, _ = client
    resp = c.post("/ingest/upload", headers={"Authorization": f"Bearer {_analyst_token()}"}, files=[
        ("files", ("a.pdf", b"%PDF-1.4\nstub", "application/pdf")),
    ])
    assert resp.status_code == 403


@patch("src.api.routes.ingest.ingest_file")
def test_upload_calls_ingest_per_file(mock_ingest, client):
    mock_ingest.return_value = 17  # pretend each file produced 17 chunks
    c, tmp = client
    resp = c.post(
        "/ingest/upload",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        files=[
            ("files", ("a.pdf", b"%PDF-1.4 stub a", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-1.4 stub b", "application/pdf")),
        ],
        data={"doc_type": "10k", "confidentiality": "public"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["files_processed"] == 2
    assert body["chunks_ingested"] == 34
    assert sorted(f["filename"] for f in body["files"]) == ["a.pdf", "b.pdf"]
    # Both files persisted to DOCUMENTS_ROOT
    assert (tmp / "a.pdf").exists()
    assert (tmp / "b.pdf").exists()
    # ingest_file called with the metadata override set to confidentiality
    for call in mock_ingest.call_args_list:
        assert call.kwargs.get("doc_type") == "10k"
        assert call.kwargs.get("metadata_override") == {"confidentiality": "public"}


@patch("src.api.routes.ingest.ingest_file")
def test_upload_rejects_non_pdf(mock_ingest, client):
    c, _ = client
    resp = c.post(
        "/ingest/upload",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        files=[("files", ("malware.exe", b"MZ\x90", "application/octet-stream"))],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["files_processed"] == 0
    assert body["status"] == "failed"
    assert len(body["errors"]) == 1
    assert ".pdf" in body["errors"][0]["error"].lower()
    mock_ingest.assert_not_called()


@patch("src.api.routes.ingest.ingest_file")
def test_upload_partial_status_on_mixed_results(mock_ingest, client):
    # First call succeeds, second raises an ingestion error
    mock_ingest.side_effect = [42, RuntimeError("simulated ingest crash")]
    c, _ = client
    resp = c.post(
        "/ingest/upload",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        files=[
            ("files", ("ok.pdf", b"%PDF-1.4 ok", "application/pdf")),
            ("files", ("bad.pdf", b"%PDF-1.4 bad", "application/pdf")),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "partial"
    assert body["files_processed"] == 1
    assert body["chunks_ingested"] == 42
    assert len(body["errors"]) == 1
    assert body["errors"][0]["filename"] == "bad.pdf"
