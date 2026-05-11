"""Unit tests for /threads endpoints.

The PostgresSaver-backed thread_service is stubbed, as is the LangGraph
state read. We only verify auth, ownership, and the response shape.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.services.auth_service import create_token


def _token(role: str = "finance", user_id: str = "finance") -> str:
    return create_token(user_id=user_id, name=f"Test {role}", role=role, department="x")


@pytest.fixture
def client():
    # Inject a fake pool object so the 503 short-circuit doesn't fire,
    # then restore whatever was there before. Without the restore, this
    # fixture leaks state to downstream tests (notably the integration
    # tests that need a real psycopg pool on app.state.pool).
    prior_pool = getattr(app.state, "pool", None)
    prior_graph = getattr(app.state, "graph", None)
    app.state.pool = object()
    yield TestClient(app)
    if prior_pool is None:
        if hasattr(app.state, "pool"):
            delattr(app.state, "pool")
    else:
        app.state.pool = prior_pool
    if prior_graph is None:
        if hasattr(app.state, "graph"):
            delattr(app.state, "graph")
    else:
        app.state.graph = prior_graph


@patch("src.api.routes.threads.list_threads_for_user", new_callable=AsyncMock)
def test_list_threads_filters_by_user(mock_list, client):
    mock_list.return_value = (
        [
            {"thread_id": "t1", "created_at_estimate": "x", "updated_at_estimate": "y", "checkpoint_count": 4},
            {"thread_id": "t2", "created_at_estimate": "x2", "updated_at_estimate": "y2", "checkpoint_count": 2},
        ],
        2,
    )

    # Inject a fake graph with aget_state stubbed
    fake_graph = MagicMock()
    fake_state = MagicMock()
    fake_state.values = {"original_query": "What was Apple's FY2023 revenue?"}
    fake_state.tasks = []
    fake_graph.aget_state = AsyncMock(return_value=fake_state)
    app.state.graph = fake_graph

    resp = client.get("/threads", headers={"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["threads"]) == 2
    assert body["threads"][0]["title"].startswith("What was Apple")
    # list_threads_for_user called with the JWT user_id
    args = mock_list.call_args
    assert args[0][1] == "finance"


def test_list_threads_requires_auth(client):
    resp = client.get("/threads")
    assert resp.status_code in (401, 403)


@patch("src.api.routes.threads.get_thread_owner", new_callable=AsyncMock)
def test_get_thread_blocks_cross_user(mock_owner, client):
    mock_owner.return_value = "different-user"  # someone else's thread
    resp = client.get("/threads/abc", headers={"Authorization": f"Bearer {_token('finance')}"})
    assert resp.status_code == 403


@patch("src.api.routes.threads.get_thread_owner", new_callable=AsyncMock)
def test_get_thread_404_when_not_found(mock_owner, client):
    mock_owner.return_value = None
    resp = client.get("/threads/abc", headers={"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 404


@patch("src.api.routes.threads.get_thread_owner", new_callable=AsyncMock)
def test_get_thread_returns_messages(mock_owner, client):
    mock_owner.return_value = "finance"

    fake_graph = MagicMock()
    fake_state = MagicMock()
    fake_state.values = {
        "original_query": "Apple FY23 revenue?",
        "final_response": "Apple's FY23 revenue was $383.3B.",
        "response_metadata": {"sources": [{"file": "10k.pdf", "page": 12}], "confidence": 0.92},
    }
    fake_state.tasks = []
    fake_graph.aget_state = AsyncMock(return_value=fake_state)
    app.state.graph = fake_graph

    resp = client.get("/threads/abc", headers={"Authorization": f"Bearer {_token('finance', 'finance')}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["thread_id"] == "abc"
    assert body["is_interrupted"] is False
    msgs = body["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["sources"][0]["file"] == "10k.pdf"


@patch("src.api.routes.threads.delete_thread", new_callable=AsyncMock)
@patch("src.api.routes.threads.get_thread_owner", new_callable=AsyncMock)
def test_delete_thread_owner_can(mock_owner, mock_delete, client):
    mock_owner.return_value = "finance"
    mock_delete.return_value = 7
    resp = client.delete("/threads/abc", headers={"Authorization": f"Bearer {_token('finance', 'finance')}"})
    assert resp.status_code == 204
    mock_delete.assert_awaited_once()


@patch("src.api.routes.threads.get_thread_owner", new_callable=AsyncMock)
def test_delete_thread_403_for_other_user(mock_owner, client):
    mock_owner.return_value = "someone-else"
    resp = client.delete("/threads/abc", headers={"Authorization": f"Bearer {_token('finance', 'finance')}"})
    assert resp.status_code == 403


@patch("src.api.routes.threads.delete_thread", new_callable=AsyncMock)
@patch("src.api.routes.threads.get_thread_owner", new_callable=AsyncMock)
def test_delete_thread_admin_can_delete_others(mock_owner, mock_delete, client):
    mock_owner.return_value = "someone-else"
    mock_delete.return_value = 7
    resp = client.delete(
        "/threads/abc",
        headers={"Authorization": f"Bearer {_token('admin', 'admin')}"},
    )
    assert resp.status_code == 204
