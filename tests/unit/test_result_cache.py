"""Unit tests for the per-stage Redis result cache (Sprint 8e).

Three concerns to verify:
  1. Cache hit/miss semantics: get_or_compute calls compute() exactly once
     on a true miss, and never on a hit. The wrapped service paths (embed,
     rerank, grader) must inherit that.
  2. Fail-open: any Redis error (down, timeout, malformed payload) must
     log a warning and fall through to the compute path. The cache MUST
     NOT be able to break the rag-agent.
  3. Key isolation: the three caches use distinct prefixes so a reranker
     score and a grader verdict for the same (query, chunk) tuple never
     collide.

These tests stub the redis client so they run with no Redis container.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.services import result_cache


# ── Fake in-memory redis client ───────────────────────────────────────────

class _FakeRedis:
    """Minimal stand-in for redis.Redis that supports get/set/ping in-memory."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.calls = {"get": 0, "set": 0, "ping": 0}

    def ping(self):
        self.calls["ping"] += 1
        return True

    def get(self, key):
        self.calls["get"] += 1
        return self.store.get(key)

    def set(self, key, value, ex=None):  # noqa: ARG002
        self.calls["set"] += 1
        self.store[key] = value
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch redis.Redis to return a fake. Resets the cache singleton."""
    fake = _FakeRedis()

    class _RedisModule:
        @staticmethod
        def Redis(**kwargs):  # noqa: ARG004
            return fake

    monkeypatch.setattr("src.services.result_cache.os.environ", {"RAG_RESULT_CACHE_ENABLED": "1"})
    monkeypatch.setitem(__import__("sys").modules, "redis", _RedisModule)
    result_cache.clear_for_tests()
    return fake


# ── Round-trip: compute called once on miss, zero on subsequent hits ──────

def test_get_or_compute_calls_once_on_miss_zero_on_hit(fake_redis):
    call_count = {"n": 0}

    def compute():
        call_count["n"] += 1
        return [0.1, 0.2, 0.3]

    v1 = result_cache.get_or_compute("query-emb", ("voyage", "voyage-finance-2", "query", "what is roe"), compute)
    v2 = result_cache.get_or_compute("query-emb", ("voyage", "voyage-finance-2", "query", "what is roe"), compute)
    v3 = result_cache.get_or_compute("query-emb", ("voyage", "voyage-finance-2", "query", "what is roe"), compute)

    assert v1 == v2 == v3 == [0.1, 0.2, 0.3]
    assert call_count["n"] == 1, "compute() must run exactly once across 3 lookups of same key"


def test_get_or_compute_recomputes_on_different_key(fake_redis):
    call_count = {"n": 0}

    def compute():
        call_count["n"] += 1
        return f"value-{call_count['n']}"

    a = result_cache.get_or_compute("query-emb", ("p", "m", "query", "alpha"), compute)
    b = result_cache.get_or_compute("query-emb", ("p", "m", "query", "beta"), compute)

    assert a == "value-1"
    assert b == "value-2"
    assert call_count["n"] == 2, "different keys → both compute() invocations should run"


# ── Key isolation: same (query, chunk) collides across cache types? ───────

def test_caches_with_distinct_names_do_not_collide(fake_redis):
    # Reranker score and grader verdict for the same (q, chunk) are
    # different shapes — must not share a key.
    rerank_v = result_cache.get_or_compute("reranker", ("model-x", "q", "chunk-text"), lambda: 0.85)
    grader_v = result_cache.get_or_compute("grader", ("model-x", "q", "chunk-text"), lambda: {"relevant": True, "reason": "matches"})

    assert rerank_v == 0.85
    assert grader_v == {"relevant": True, "reason": "matches"}
    # 2 distinct keys must be in the store
    assert len(fake_redis.store) == 2


def test_unknown_cache_name_raises():
    with pytest.raises(KeyError, match="Unknown cache_name"):
        result_cache.get_or_compute("not-a-cache", ("a",), lambda: 1)


# ── Fail-open paths ───────────────────────────────────────────────────────

def test_compute_runs_when_redis_connect_fails(monkeypatch):
    """If redis.Redis.ping() raises (e.g. container down), the cache must
    silently disable itself and every get_or_compute() must run compute().
    """
    class _BrokenRedisModule:
        @staticmethod
        def Redis(**kwargs):  # noqa: ARG004
            class _BrokenClient:
                def ping(self):
                    raise ConnectionError("redis container is down")
            return _BrokenClient()

    monkeypatch.setitem(__import__("sys").modules, "redis", _BrokenRedisModule)
    monkeypatch.setattr("src.services.result_cache.os.environ", {"RAG_RESULT_CACHE_ENABLED": "1"})
    result_cache.clear_for_tests()

    call_count = {"n": 0}

    def compute():
        call_count["n"] += 1
        return "computed"

    # 3 lookups should all run compute() because the cache is disabled
    for _ in range(3):
        result_cache.get_or_compute("query-emb", ("a", "b", "c", "d"), compute)

    assert call_count["n"] == 3, "compute() must run on every call when Redis is unreachable"


def test_compute_runs_when_redis_get_raises(fake_redis):
    """A flaky GET (timeout / connection drop mid-call) must fall through."""

    def boom(_key):
        raise TimeoutError("socket timeout")

    with patch.object(fake_redis, "get", side_effect=boom):
        call_count = {"n": 0}

        def compute():
            call_count["n"] += 1
            return "fresh"

        v = result_cache.get_or_compute("query-emb", ("p", "m", "query", "x"), compute)

        assert v == "fresh"
        assert call_count["n"] == 1


def test_malformed_cached_payload_falls_through(fake_redis):
    """If Redis returns invalid JSON (e.g. wrong type cached by another
    process), the cache layer must treat it as a miss, not crash.
    """
    # Manually poison the cache with a non-JSON byte string
    key = result_cache._KEY_PREFIX["query-emb"] + result_cache._hash_key("p", "m", "query", "x")
    fake_redis.store[key] = b"\xff\xfe\x00garbage"

    compute_calls = {"n": 0}

    def compute():
        compute_calls["n"] += 1
        return [1.0, 2.0]

    v = result_cache.get_or_compute("query-emb", ("p", "m", "query", "x"), compute)

    assert v == [1.0, 2.0]
    assert compute_calls["n"] == 1


def test_disabled_via_env_skips_redis_entirely(monkeypatch):
    """Setting RAG_RESULT_CACHE_ENABLED=0 must short-circuit before any
    Redis calls happen — important for environments where Redis exists
    but you want clean cold-cache benchmarks.
    """
    sentinel_module_calls = {"n": 0}

    class _SentinelRedisModule:
        @staticmethod
        def Redis(**kwargs):  # noqa: ARG004
            sentinel_module_calls["n"] += 1
            class _Stub:
                def ping(self): return True
                def get(self, _): sentinel_module_calls["n"] += 1; return None
                def set(self, *a, **kw): sentinel_module_calls["n"] += 1
            return _Stub()

    monkeypatch.setitem(__import__("sys").modules, "redis", _SentinelRedisModule)
    monkeypatch.setattr("src.services.result_cache.os.environ", {"RAG_RESULT_CACHE_ENABLED": "0"})
    result_cache.clear_for_tests()

    compute_calls = {"n": 0}

    def compute():
        compute_calls["n"] += 1
        return "x"

    for _ in range(5):
        result_cache.get_or_compute("query-emb", ("p", "m", "query", "v"), compute)

    assert compute_calls["n"] == 5
    assert sentinel_module_calls["n"] == 0, "redis client should never be touched when disabled"


# ── Hash stability — same input produces same key across runs ─────────────

def test_hash_key_is_stable():
    a = result_cache._hash_key("foo", "bar", "baz")
    b = result_cache._hash_key("foo", "bar", "baz")
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_hash_key_separator_prevents_collisions():
    # ("foo", "bar") vs ("foob", "ar") must hash differently because the
    # separator byte sequence prevents naive concatenation collisions.
    a = result_cache._hash_key("foo", "bar")
    b = result_cache._hash_key("foob", "ar")
    assert a != b
