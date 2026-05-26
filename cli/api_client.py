"""Thin httpx wrapper for the FinanceBench /v1 API."""

from __future__ import annotations

import json
from typing import Iterator

import httpx
from httpx_sse import connect_sse

from cli import credentials

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 180.0


class APIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


class APIClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        if base_url is None or token is None:
            creds = credentials.load()
            if creds is not None:
                base_url = base_url or creds.get("base_url", DEFAULT_BASE_URL)
                token = token or creds.get("token")
        self.base_url = base_url or DEFAULT_BASE_URL
        self.token = token
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(DEFAULT_TIMEOUT),
            headers={"Accept": "application/vnd.financebench.v1+json, application/json"},
        )

    def _headers(self, auth_required: bool) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if auth_required and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _raise_for(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except (ValueError, AttributeError):
                detail = r.text or f"<no body, status={r.status_code}>"
            raise APIError(r.status_code, detail if isinstance(detail, str) else str(detail))

    def post(self, path: str, json_body: dict, auth_required: bool = True) -> dict:
        r = self._client.post(path, json=json_body, headers=self._headers(auth_required))
        self._raise_for(r)
        return r.json()

    def get(self, path: str, auth_required: bool = True) -> dict:
        r = self._client.get(path, headers=self._headers(auth_required))
        self._raise_for(r)
        return r.json()

    def stream_chat(self, json_body: dict) -> Iterator[dict]:
        """Stream /v1/chat/stream as parsed-JSON SSE events.

        Yields dicts with a 'type' field — one of node_start, node_end, token,
        hitl_interrupt, final, error (see api/routes/chat.py for shapes).
        Unknown event types are yielded as-is; callers should ignore unknown
        types for forward compatibility (deployment plan Section 18.3.2).
        """
        with connect_sse(
            self._client,
            "POST",
            "/v1/chat/stream",
            json=json_body,
            headers=self._headers(auth_required=True),
        ) as event_source:
            for sse in event_source.iter_sse():
                if not sse.data:
                    continue
                try:
                    yield json.loads(sse.data)
                except json.JSONDecodeError:
                    continue

    def close(self) -> None:
        self._client.close()
