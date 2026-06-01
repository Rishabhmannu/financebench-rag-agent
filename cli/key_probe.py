"""API-key Layer 2 validation — live probe against the provider's API.

Layer 1 (cli/commands/setup.py:_API_KEY_PROMPTS) checks the prefix format
(e.g. OPENAI starts with `sk-` or `sk-proj-`). That catches typos and
provider-mismatched pastes but misses:
  - Revoked keys
  - Keys from the wrong account / project
  - Expired keys
  - Format-correct but never-issued strings

Layer 2 issues one tiny request per provider. OpenAI/Anthropic/Groq use a
free `GET /v1/models` endpoint; Voyage uses a 1-token embedding (~$0.00002).
All probes are network-failure-tolerant: a transport error returns
NETWORK_ERROR which callers should treat as "skip, save as-is, warn user".

Probes are called from:
  - `financebench setup` (per-key, after Layer 1 accepts the value)
  - `financebench doctor` (full mode, one check per configured key)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import httpx

_PROBE_TIMEOUT_S = 10.0


class ProbeStatus(Enum):
    OK = "ok"
    BAD_KEY = "bad_key"
    NETWORK_ERROR = "network_error"


@dataclass
class ProbeResult:
    status: ProbeStatus
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ProbeStatus.OK


def _probe(
    method: str,
    url: str,
    headers: dict,
    json_body: dict | None,
    provider: str,
    signup_url: str,
) -> ProbeResult:
    try:
        if method == "GET":
            r = httpx.get(url, headers=headers, timeout=_PROBE_TIMEOUT_S)
        else:
            r = httpx.post(url, headers=headers, json=json_body, timeout=_PROBE_TIMEOUT_S)
    except httpx.RequestError as e:
        return ProbeResult(ProbeStatus.NETWORK_ERROR, f"couldn't reach {provider}: {e}")

    if r.status_code == 200:
        return ProbeResult(ProbeStatus.OK, f"{provider} accepted the key")
    if r.status_code in (401, 403):
        return ProbeResult(
            ProbeStatus.BAD_KEY,
            f"{provider} rejected the key ({r.status_code}). Check it at {signup_url}.",
        )
    # Non-auth error (rate limit, server-side issue). Treat as network-ish: don't
    # block setup, but report it. Format-correct keys that get HTTP 429 here are
    # almost certainly valid.
    return ProbeResult(
        ProbeStatus.NETWORK_ERROR,
        f"{provider} returned HTTP {r.status_code}: {r.text[:120]}",
    )


def probe_openai(key: str) -> ProbeResult:
    return _probe(
        method="GET",
        url="https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        json_body=None,
        provider="OpenAI",
        signup_url="https://platform.openai.com/api-keys",
    )


def probe_anthropic(key: str) -> ProbeResult:
    return _probe(
        method="GET",
        url="https://api.anthropic.com/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json_body=None,
        provider="Anthropic",
        signup_url="https://console.anthropic.com/settings/keys",
    )


def probe_voyage(key: str) -> ProbeResult:
    # 1-token embedding against voyage-finance-2 (~$0.00002 per call).
    # No free models-list endpoint, so this is the cheapest probe available.
    return _probe(
        method="POST",
        url="https://api.voyageai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json_body={"input": ["probe"], "model": "voyage-finance-2"},
        provider="Voyage",
        signup_url="https://dash.voyageai.com/api-keys",
    )


def probe_groq(key: str) -> ProbeResult:
    return _probe(
        method="GET",
        url="https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        json_body=None,
        provider="Groq",
        signup_url="https://console.groq.com/keys",
    )


PROBES = {
    "OPENAI_API_KEY": probe_openai,
    "ANTHROPIC_API_KEY": probe_anthropic,
    "VOYAGE_API_KEY": probe_voyage,
    "GROQ_API_KEY": probe_groq,
}
