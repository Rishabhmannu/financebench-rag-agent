"""Unit tests for /admin/audit + /admin/evaluations (Sprint 9.0.2).

Both endpoints are admin-only. /admin/audit hits Langfuse over HTTP and
is stubbed via httpx.MockTransport (same pattern as /admin/costs tests).
/admin/evaluations reads files from disk and is stubbed by overriding
the search root via a fixture.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import admin
from src.services.auth_service import create_token


def _admin_token() -> str:
    return create_token(user_id="admin", name="Test Admin", role="admin", department="IT")


def _analyst_token() -> str:
    return create_token(user_id="analyst", name="Test Analyst", role="analyst", department="x")


@pytest.fixture
def client():
    return TestClient(app)


# ─── /admin/audit ────────────────────────────────────────────────────────

def test_audit_requires_admin(client):
    assert client.get("/admin/audit").status_code in (401, 403)
    assert client.get(
        "/admin/audit", headers={"Authorization": f"Bearer {_analyst_token()}"}
    ).status_code == 403


def _stub_langfuse_traces(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/public/traces" in request.url.path:
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "not stubbed"})
    return httpx.MockTransport(handler)


def test_audit_returns_traces_sorted_newest_first(monkeypatch, client):
    payload = {
        "data": [
            {
                "id": "trace-1", "timestamp": "2026-05-11T10:00:00Z",
                "userId": "finance", "totalCost": 0.0015, "latency": 1234,
                "name": "litellm-acompletion",
                "input": {"messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "What was Apple FY23 revenue?"},
                ]},
            },
            {
                "id": "trace-2", "timestamp": "2026-05-11T11:30:00Z",
                "userId": "analyst", "totalCost": 0.0008, "latency": 980,
                "name": "litellm-acompletion",
                "input": {"messages": [{"role": "user", "content": "Microsoft margin?"}]},
            },
        ],
        "meta": {"totalItems": 2},
    }

    transport = _stub_langfuse_traces(payload)
    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(admin.httpx, "AsyncClient", fake_client)

    resp = client.get(
        "/admin/audit?hours=24&limit=10",
        headers={"Authorization": f"Bearer {_admin_token()}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["events"][0]["trace_id"] == "trace-2"  # newer first
    assert body["events"][0]["user_id"] == "analyst"
    assert body["events"][0]["query"].startswith("Microsoft margin")
    # Cost surfaces from totalCost on the trace
    assert body["events"][1]["cost_usd"] == pytest.approx(0.0015)


def test_audit_query_preview_handles_anthropic_content_blocks(monkeypatch, client):
    """Anthropic-style messages use list-of-blocks for content. The
    preview helper should extract the text part rather than stringify
    the whole structure."""
    payload = {
        "data": [{
            "id": "trace-3", "timestamp": "2026-05-11T12:00:00Z",
            "userId": "finance", "totalCost": 0.001, "latency": 800,
            "input": {"messages": [{
                "role": "user",
                "content": [{"type": "text", "text": "Pfizer Q4 2022 expenses?"}],
            }]},
        }]
    }
    transport = _stub_langfuse_traces(payload)
    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(admin.httpx, "AsyncClient", fake_client)
    resp = client.get(
        "/admin/audit", headers={"Authorization": f"Bearer {_admin_token()}"}
    )
    assert resp.status_code == 200
    assert resp.json()["events"][0]["query"].startswith("Pfizer Q4")


def test_audit_502_on_langfuse_failure(monkeypatch, client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(admin.httpx, "AsyncClient", fake_client)
    resp = client.get(
        "/admin/audit", headers={"Authorization": f"Bearer {_admin_token()}"}
    )
    assert resp.status_code == 502


def test_audit_validates_query_params(client):
    h = {"Authorization": f"Bearer {_admin_token()}"}
    assert client.get("/admin/audit?hours=0", headers=h).status_code == 422
    assert client.get("/admin/audit?hours=721", headers=h).status_code == 422
    assert client.get("/admin/audit?limit=0", headers=h).status_code == 422
    assert client.get("/admin/audit?limit=501", headers=h).status_code == 422


# ─── /admin/evaluations ──────────────────────────────────────────────────

def test_evaluations_requires_admin(client):
    assert client.get("/admin/evaluations").status_code in (401, 403)
    assert client.get(
        "/admin/evaluations", headers={"Authorization": f"Bearer {_analyst_token()}"}
    ).status_code == 403


def test_evaluations_normalizes_string_and_dict_metric_fields(tmp_path, monkeypatch, client):
    """Older eval snapshots embed metric dicts as JSON strings; newer ones
    use objects. Both must round-trip to the same response shape."""
    # Route reads tests/evaluation/eval_results relative to cwd
    root = tmp_path / "tests" / "evaluation" / "eval_results"
    root.mkdir(parents=True)

    # Old-style: metrics as JSON strings
    (root / "financebench_old_style.json").write_text(json.dumps({
        "num_samples": 150,
        "pipeline_time_seconds": 4320,
        "correctness": json.dumps({"pass_rate": 0.30, "n_pass": 45, "n_samples": 150}),
        "ragas": json.dumps({"faithfulness": 0.47, "context_precision": 0.55}),
        "deepeval": json.dumps({"faithfulness": 0.63}),
        "diagnostics": json.dumps({"refusal_rate": 0.12}),
    }))

    # New-style: metrics as dicts
    (root / "financebench_new_style.json").write_text(json.dumps({
        "num_samples": 150,
        "pipeline_time_seconds": 6500,
        "correctness": {"pass_rate": 0.467, "n_pass": 70, "n_samples": 150},
        "ragas": {"faithfulness": 0.71, "context_precision": 0.73},
        "deepeval": {"faithfulness": 0.83},
        "diagnostics": {"refusal_rate": 0.07},
    }))

    # Decoy files that must be filtered out
    (root / "financebench_old_style.pipeline.json").write_text("{}")
    (root / "financebench_old_style.ragas.json").write_text("{}")
    (root / "financebench_old_style.review.json").write_text("{}")
    (root / "_manifest.json").write_text("{}")

    # Patch the search root by reaching into the route helper
    monkeypatch.chdir(tmp_path)

    resp = client.get(
        "/admin/evaluations",
        headers={"Authorization": f"Bearer {_admin_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # 2 snapshots, not 6 — decoys filtered out
    assert body["total"] == 2
    snaps = {s["label"]: s for s in body["snapshots"]}
    assert "old_style" in snaps and "new_style" in snaps

    # Both styles normalize to the same dict shape
    old = snaps["old_style"]
    assert old["correctness"]["pass_rate"] == pytest.approx(0.30)
    assert old["ragas"]["faithfulness"] == pytest.approx(0.47)
    assert old["deepeval"]["faithfulness"] == pytest.approx(0.63)

    new = snaps["new_style"]
    assert new["correctness"]["pass_rate"] == pytest.approx(0.467)
    assert new["ragas"]["faithfulness"] == pytest.approx(0.71)


def test_evaluations_empty_when_no_dir(tmp_path, monkeypatch, client):
    monkeypatch.chdir(tmp_path)  # eval_results dir doesn't exist
    resp = client.get(
        "/admin/evaluations",
        headers={"Authorization": f"Bearer {_admin_token()}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"snapshots": [], "total": 0}


def test_evaluations_handles_malformed_snapshot(tmp_path, monkeypatch, client):
    """An unreadable snapshot file is skipped, not fatal."""
    root = tmp_path / "tests/evaluation/eval_results"
    root.mkdir(parents=True)
    (root / "financebench_corrupted.json").write_text("not-valid-json{")
    (root / "financebench_good.json").write_text(json.dumps({
        "num_samples": 150,
        "correctness": {"pass_rate": 0.5, "n_pass": 75, "n_samples": 150},
    }))

    monkeypatch.chdir(tmp_path)
    resp = client.get(
        "/admin/evaluations",
        headers={"Authorization": f"Bearer {_admin_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Only the good one made it through
    assert body["total"] == 1
    assert body["snapshots"][0]["label"] == "good"
