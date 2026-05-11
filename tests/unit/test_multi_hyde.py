"""Unit tests for Multi-HyDE (Sprint 7.10a).

Two concerns:
  1. The service generates N hypotheticals from one LLM call, parses the
     blank-line-separated output, tolerates numbered prefixes, and degrades
     gracefully when the LLM fails or returns malformed output.
  2. The retrieval node, when ENABLE_MULTI_HYDE=True, runs (1 + N) hybrid
     searches and RRF-fuses the results, deduping by chunk_id. When the
     flag is off (or hypotheticals are empty), it must behave exactly like
     the pre-Sprint-7.10a single-query path.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.config.settings import settings
from src.graph.nodes import retrieval as retrieval_module
from src.services import multi_hyde


# ── Fake LLM ──────────────────────────────────────────────────────────────


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    def invoke(self, messages):  # noqa: ARG002
        self.calls += 1
        return SimpleNamespace(content=self._content)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """Bypass Redis result cache: every test calls the underlying LLM path."""
    def passthrough(_cache_name, _key_parts, compute, ttl_seconds=None):  # noqa: ARG001
        return compute()

    monkeypatch.setattr(multi_hyde, "get_or_compute", passthrough)


# ── Service: parsing + failure modes ──────────────────────────────────────


def test_generate_hypotheticals_returns_n_passages(monkeypatch):
    llm = _FakeLLM(
        "Apple Inc.'s total net sales for fiscal 2023 were $383.3 billion, a "
        "decrease of 3% from the prior year, reflecting lower sales of iPhone "
        "and Mac partially offset by Services growth.\n\n"
        "Net sales in the U.S. were $162.6 billion in fiscal 2023, compared "
        "to $169.7 billion in fiscal 2022, with the change driven primarily "
        "by foreign currency headwinds.\n\n"
        "Total revenue recognized in the year ended September 30, 2023 "
        "consisted of $298.1 billion in product sales and $85.2 billion in "
        "service revenue, totaling $383.3 billion."
    )
    monkeypatch.setattr(multi_hyde, "_llm_for_task", lambda *a, **kw: llm)
    out = multi_hyde.generate_hypotheticals(
        query="What was Apple's FY23 revenue?",
        target_company="apple",
        target_fiscal_year=2023,
        n=3,
    )
    assert len(out) == 3
    assert llm.calls == 1
    assert all(h.strip() for h in out)
    assert "Apple Inc.'s" in out[0]
    assert out[1].startswith("Net sales")
    assert "Total revenue" in out[2]


def test_generate_hypotheticals_strips_numbered_prefixes(monkeypatch):
    llm = _FakeLLM(
        "1. First hypothetical passage about revenue.\n\n"
        "2) Second hypothetical passage about margins.\n\n"
        "- Third hypothetical passage about segments."
    )
    monkeypatch.setattr(multi_hyde, "_llm_for_task", lambda *a, **kw: llm)
    out = multi_hyde.generate_hypotheticals("q", n=3)
    assert len(out) == 3
    assert out[0].startswith("First")
    assert out[1].startswith("Second")
    assert out[2].startswith("Third")


def test_generate_hypotheticals_returns_fewer_when_llm_underdelivers(monkeypatch):
    llm = _FakeLLM("Only one passage came back.\n\nAnd a second one.")
    monkeypatch.setattr(multi_hyde, "_llm_for_task", lambda *a, **kw: llm)
    out = multi_hyde.generate_hypotheticals("q", n=3)
    assert len(out) == 2  # parser returns what it got, doesn't pad


def test_generate_hypotheticals_returns_empty_on_llm_failure(monkeypatch):
    class _Boom:
        def invoke(self, _):
            raise RuntimeError("upstream rate-limited")

    monkeypatch.setattr(multi_hyde, "_llm_for_task", lambda *a, **kw: _Boom())
    out = multi_hyde.generate_hypotheticals("q", n=3)
    assert out == []  # caller falls back to single-query retrieval


def test_generate_hypotheticals_empty_query_short_circuits(monkeypatch):
    # If query is empty, we should never call the LLM
    called = {"n": 0}

    def fake_factory(*_a, **_kw):
        called["n"] += 1
        return _FakeLLM("")

    monkeypatch.setattr(multi_hyde, "_llm_for_task", fake_factory)
    assert multi_hyde.generate_hypotheticals("", n=3) == []
    assert called["n"] == 0


# ── Retrieval node integration ────────────────────────────────────────────


def _chunk(source_file: str, idx: int, score: float = 0.5) -> dict:
    return {
        "content": f"chunk-{source_file}-{idx}",
        "metadata": {"source_file": source_file, "chunk_index": idx, "page_number": idx},
        "score": score,
    }


@pytest.fixture
def fake_qdrant(monkeypatch):
    """Stub Qdrant client + RBAC permissions to keep retrieval pure."""
    client = MagicMock()
    monkeypatch.setattr(retrieval_module, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(retrieval_module, "build_retrieval_filter", lambda **_kw: None)
    monkeypatch.setattr(
        retrieval_module,
        "get_permissions",
        lambda _role: {"allowed_confidentiality": ["*"]},
    )
    monkeypatch.setattr(retrieval_module, "embed_text", lambda text: [0.0] * 8)
    return client


def test_retrieval_node_without_multi_hyde_runs_single_search(monkeypatch, fake_qdrant):
    monkeypatch.setattr(settings, "ENABLE_MULTI_HYDE", False)
    monkeypatch.setattr(settings, "RETRIEVAL_TOP_K", 5)

    search_calls = []

    def fake_hybrid_search(**kw):
        search_calls.append(kw["query_text"])
        return [_chunk("a.pdf", 1), _chunk("b.pdf", 2)]

    monkeypatch.setattr(retrieval_module, "hybrid_search", fake_hybrid_search)

    out = retrieval_module.retrieval_node({"sanitized_query": "What is ROE?"})
    assert len(search_calls) == 1
    assert search_calls[0] == "What is ROE?"
    assert len(out["retrieved_chunks"]) == 2


def test_retrieval_node_with_multi_hyde_runs_n_plus_one_searches(monkeypatch, fake_qdrant):
    monkeypatch.setattr(settings, "ENABLE_MULTI_HYDE", True)
    monkeypatch.setattr(settings, "MULTI_HYDE_N", 3)
    monkeypatch.setattr(settings, "RETRIEVAL_TOP_K", 50)

    monkeypatch.setattr(
        retrieval_module,
        "generate_hypotheticals",
        lambda **_kw: ["hypothetical 1", "hypothetical 2", "hypothetical 3"],
    )

    search_calls = []

    def fake_hybrid_search(**kw):
        search_calls.append(kw["query_text"])
        # Each path returns different chunks to verify fusion + dedup
        if kw["query_text"] == "What is ROE?":
            return [_chunk("a.pdf", 1, 0.9), _chunk("a.pdf", 2, 0.8)]
        if kw["query_text"] == "hypothetical 1":
            return [_chunk("b.pdf", 1, 0.7), _chunk("a.pdf", 1, 0.6)]  # overlap a.pdf-1
        if kw["query_text"] == "hypothetical 2":
            return [_chunk("c.pdf", 1, 0.65)]
        if kw["query_text"] == "hypothetical 3":
            return [_chunk("a.pdf", 1, 0.5)]  # third hit on a.pdf-1
        return []

    monkeypatch.setattr(retrieval_module, "hybrid_search", fake_hybrid_search)

    out = retrieval_module.retrieval_node({
        "sanitized_query": "What is ROE?",
        "target_company": "apple",
        "target_fiscal_year": 2023,
    })

    assert len(search_calls) == 4  # 1 original + 3 hypotheticals
    chunk_ids = [(c["metadata"]["source_file"], c["metadata"]["chunk_index"]) for c in out["retrieved_chunks"]]
    # Deduped: a.pdf-1 appears once even though it's in 3 paths
    assert chunk_ids.count(("a.pdf", 1)) == 1
    # a.pdf-1 ranks first because it has highest RRF score (appears in 3 paths)
    assert chunk_ids[0] == ("a.pdf", 1)


def test_retrieval_node_falls_back_when_multi_hyde_returns_empty(monkeypatch, fake_qdrant):
    monkeypatch.setattr(settings, "ENABLE_MULTI_HYDE", True)
    monkeypatch.setattr(settings, "RETRIEVAL_TOP_K", 5)
    monkeypatch.setattr(retrieval_module, "generate_hypotheticals", lambda **_kw: [])

    search_calls = []

    def fake_hybrid_search(**kw):
        search_calls.append(kw["query_text"])
        return [_chunk("a.pdf", 1)]

    monkeypatch.setattr(retrieval_module, "hybrid_search", fake_hybrid_search)

    out = retrieval_module.retrieval_node({"sanitized_query": "What is ROE?"})
    assert len(search_calls) == 1  # falls back to single-query path
    assert len(out["retrieved_chunks"]) == 1


def test_retrieval_node_recovers_when_one_hyde_path_search_throws(monkeypatch, fake_qdrant):
    """A search failure on one hypothetical path must not abort retrieval —
    the original query + surviving hypotheticals should still produce results."""
    monkeypatch.setattr(settings, "ENABLE_MULTI_HYDE", True)
    monkeypatch.setattr(settings, "MULTI_HYDE_N", 2)
    monkeypatch.setattr(settings, "RETRIEVAL_TOP_K", 5)
    monkeypatch.setattr(retrieval_module, "generate_hypotheticals", lambda **_kw: ["h1", "h2"])

    def fake_hybrid_search(**kw):
        if kw["query_text"] == "h1":
            raise RuntimeError("transient qdrant error")
        return [_chunk("a.pdf", 1)]

    monkeypatch.setattr(retrieval_module, "hybrid_search", fake_hybrid_search)

    out = retrieval_module.retrieval_node({"sanitized_query": "What is ROE?"})
    assert len(out["retrieved_chunks"]) == 1
