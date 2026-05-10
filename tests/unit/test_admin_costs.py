"""Unit tests for the /admin/costs endpoint (Sprint 8 8d).

The Langfuse HTTP calls are stubbed via httpx.MockTransport so the test
exercises only our aggregation logic, not the network. Per-user
attribution is wired through current_user_id (FastAPI dep sets it) and
read inside _attribution_kwargs in llm_factory; here we verify the
read-side aggregation paths.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import admin
from src.services.auth_service import create_token


def _admin_token() -> str:
    return create_token(user_id="admin-1", name="Admin User", role="admin", department="IT")


def _analyst_token() -> str:
    return create_token(user_id="analyst-1", name="An Analyst", role="analyst", department="Research")


@pytest.fixture
def client():
    return TestClient(app)


# ── auth gates ────────────────────────────────────────────────────────────

def test_admin_costs_requires_auth(client):
    resp = client.get("/admin/costs")
    assert resp.status_code in (401, 403)


def test_admin_costs_rejects_non_admin(client):
    resp = client.get(
        "/admin/costs",
        headers={"Authorization": f"Bearer {_analyst_token()}"},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


# ── aggregation: stub Langfuse, verify totals + buckets ───────────────────

def _stub_langfuse(responses: dict[str, dict]):
    """Build an httpx mock transport that returns canned JSON per URL path."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Match by path so the same handler covers /observations and /traces/<id>
        path = request.url.path
        for prefix, payload in responses.items():
            if path.startswith(prefix):
                if prefix == "/api/public/traces":
                    # Per-trace lookup — extract trace id from path tail
                    tid = path.rsplit("/", 1)[-1]
                    body = payload.get(tid, {"userId": None})
                    return httpx.Response(200, json=body)
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "not stubbed", "path": path})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_admin_costs_aggregates_by_user_model_and_trace(monkeypatch, client):
    observations_payload = {
        "data": [
            # Two calls from user-A
            {
                "id": "obs-1", "name": "litellm-acompletion", "model": "gpt-4o-mini",
                "promptTokens": 100, "completionTokens": 50, "calculatedTotalCost": 0.0010,
                "traceId": "trace-A1",
            },
            {
                "id": "obs-2", "name": "litellm-acompletion", "model": "gpt-4o-mini",
                "promptTokens": 200, "completionTokens": 80, "calculatedTotalCost": 0.0020,
                "traceId": "trace-A2",
            },
            # One call from user-B on a different model
            {
                "id": "obs-3", "name": "litellm-acompletion", "model": "claude-haiku-4-5",
                "promptTokens": 50, "completionTokens": 25, "calculatedTotalCost": 0.0005,
                "traceId": "trace-B1",
            },
            # An embedding (cache lookup) — no user (system call)
            {
                "id": "obs-4", "name": "litellm-aembedding", "model": "text-embedding-3-small",
                "promptTokens": 30, "completionTokens": 0, "calculatedTotalCost": 0.0001,
                "traceId": "trace-sys-1",
            },
        ],
        "meta": {"totalItems": 4},
    }
    traces_payload = {
        "trace-A1": {"userId": "user-a"},
        "trace-A2": {"userId": "user-a"},
        "trace-B1": {"userId": "user-b"},
        "trace-sys-1": {"userId": None},
    }

    transport = _stub_langfuse({
        "/api/public/observations": observations_payload,
        "/api/public/traces": traces_payload,
    })

    # Patch httpx.AsyncClient inside admin.py to use our transport
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(admin.httpx, "AsyncClient", fake_async_client)

    resp = client.get(
        "/admin/costs?days=7",
        headers={"Authorization": f"Bearer {_admin_token()}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Total: 4 calls, $0.0036 cost, 535 tokens
    assert body["total"]["calls"] == 4
    assert body["total"]["cost_usd"] == pytest.approx(0.0036, abs=1e-9)
    assert body["total"]["tokens"] == 535
    assert body["window_days"] == 7

    by_user = {row["key"]: row for row in body["by_user"]}
    assert by_user["user-a"]["calls"] == 2
    assert by_user["user-a"]["cost_usd"] == pytest.approx(0.0030, abs=1e-9)
    assert by_user["user-b"]["calls"] == 1
    assert by_user["user-b"]["cost_usd"] == pytest.approx(0.0005, abs=1e-9)
    # System / unattributed call shows up under None
    assert any(row["key"] is None for row in body["by_user"])

    by_model = {row["key"]: row for row in body["by_model"]}
    assert by_model["gpt-4o-mini"]["calls"] == 2
    assert by_model["claude-haiku-4-5"]["calls"] == 1
    assert by_model["text-embedding-3-small"]["calls"] == 1

    by_trace = {row["key"]: row for row in body["by_trace_name"]}
    # The route strips the random-suffix segment so calls collapse by op
    assert by_trace["litellm-acompletion"]["calls"] == 3
    assert by_trace["litellm-aembedding"]["calls"] == 1

    # Lists are sorted descending by cost so the most-expensive bucket is first
    assert body["by_user"][0]["cost_usd"] >= body["by_user"][-1]["cost_usd"]


# ── Langfuse 5xx surfaces as 502 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_costs_handles_langfuse_failure(monkeypatch, client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(admin.httpx, "AsyncClient", fake_async_client)

    resp = client.get(
        "/admin/costs?days=1",
        headers={"Authorization": f"Bearer {_admin_token()}"},
    )
    assert resp.status_code == 502
    assert "langfuse" in resp.json()["detail"].lower()


# ── Bounds check on the days query param ──────────────────────────────────

def test_admin_costs_validates_days_range(client):
    headers = {"Authorization": f"Bearer {_admin_token()}"}
    assert client.get("/admin/costs?days=0", headers=headers).status_code == 422
    assert client.get("/admin/costs?days=91", headers=headers).status_code == 422
