"""Unit tests for the cost tracker."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services import cost_tracker
from src.services.cost_tracker import (
    MODEL_PRICING,
    CostCallbackHandler,
    CostTracker,
    _ActiveContext,
    compute_cost,
)


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Each test gets its own log file + run dir, and resets the active context."""
    log_path = tmp_path / "cost_log.jsonl"
    run_dir = tmp_path / "by_run"
    monkeypatch.setattr(cost_tracker, "DEFAULT_LOG_FILE", log_path)
    monkeypatch.setattr(cost_tracker, "DEFAULT_RUN_DIR", run_dir)
    _ActiveContext.set(None, log_path)
    yield log_path
    _ActiveContext.set(None, log_path)


def test_compute_cost_claude_sonnet_no_cache():
    # 1M input + 1M output @ list price
    cost = compute_cost("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(3.00 + 15.00)


def test_compute_cost_claude_with_cache_split():
    # Anthropic prompt-caching split: total input 1M, of which 800k is cache_read
    # Expected: 200k @ $3 + 800k @ $0.30 + 100k @ $15
    cost = compute_cost(
        "claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=800_000,
    )
    expected = (200_000 * 3.0 + 800_000 * 0.3 + 100_000 * 15.0) / 1_000_000
    assert cost == pytest.approx(round(expected, 6))


def test_compute_cost_unknown_model_returns_zero():
    assert compute_cost("totally-made-up-model", input_tokens=10_000, output_tokens=5_000) == 0.0


def test_compute_cost_groq_free_tier():
    # Groq tracks tokens at 0 cost
    assert compute_cost("llama-3.3-70b-versatile", input_tokens=50_000, output_tokens=20_000) == 0.0


def test_pricing_table_has_required_models():
    # Guards against accidental deletion of models the project actively uses.
    for required in [
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "gpt-4o-mini",
        "text-embedding-3-small",
        "text-embedding-3-large",
        "llama-3.3-70b-versatile",
    ]:
        assert required in MODEL_PRICING, f"missing pricing for {required}"


def test_callback_records_to_jsonl(_isolated_log: Path):
    """Simulate an LLM call by driving the callback handler directly."""
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    handler = CostCallbackHandler()
    run_uuid = uuid4()

    handler.on_chat_model_start(
        serialized={"kwargs": {"model": "claude-sonnet-4-6"}},
        prompts=["hi"],
        run_id=run_uuid,
        metadata={"node": "generator"},
    )

    msg = AIMessage(
        content="hello world",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "input_token_details": {"cache_read": 800, "cache_creation": 0},
        },
    )
    response = LLMResult(
        generations=[[ChatGeneration(message=msg)]],
        llm_output={"model_name": "claude-sonnet-4-6"},
    )

    with CostTracker.run("test_run"):
        handler.on_llm_end(response, run_id=run_uuid)

    assert _isolated_log.exists()
    lines = _isolated_log.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["model"] == "claude-sonnet-4-6"
    assert rec["run_id"] == "test_run"
    assert rec["input_tokens"] == 1000
    assert rec["output_tokens"] == 200
    assert rec["cache_read_tokens"] == 800
    assert rec["node"] == "generator"
    # 200 plain input @ $3 + 800 cache_read @ $0.30 + 200 output @ $15, all per-million
    expected = (200 * 3.0 + 800 * 0.3 + 200 * 15.0) / 1_000_000
    assert rec["cost_usd"] == pytest.approx(round(expected, 6))


def test_summarize_aggregates_across_records(_isolated_log: Path):
    """Two records, same run, same model → summed."""
    records = [
        {"run_id": "r1", "model": "gpt-4o-mini", "cost_usd": 0.001, "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "cache_write_tokens": 0},
        {"run_id": "r1", "model": "gpt-4o-mini", "cost_usd": 0.002, "input_tokens": 200, "output_tokens": 75, "cache_read_tokens": 0, "cache_write_tokens": 0},
        {"run_id": "r2", "model": "claude-sonnet-4-6", "cost_usd": 0.05, "input_tokens": 5000, "output_tokens": 1000, "cache_read_tokens": 0, "cache_write_tokens": 0},
    ]
    with _isolated_log.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    summary = CostTracker.summarize(log_path=_isolated_log)
    assert summary["totals"]["calls"] == 3
    assert summary["totals"]["cost_usd"] == pytest.approx(0.053)
    assert summary["runs"]["r1"]["cost_usd"] == pytest.approx(0.003)
    assert summary["runs"]["r1"]["models"]["gpt-4o-mini"]["calls"] == 2
    assert summary["runs"]["r2"]["cost_usd"] == pytest.approx(0.05)


def test_run_context_writes_summary(_isolated_log: Path):
    """Entering CostTracker.run + emitting one record + exiting → per-run summary file."""
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult
    from uuid import uuid4

    handler = CostCallbackHandler()
    run_uuid = uuid4()

    with CostTracker.run("smoke_run", log_path=_isolated_log):
        handler.on_chat_model_start(
            serialized={"kwargs": {"model": "gpt-4o-mini"}},
            prompts=[""],
            run_id=run_uuid,
        )
        msg = AIMessage(
            content="x",
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        )
        handler.on_llm_end(
            LLMResult(
                generations=[[ChatGeneration(message=msg)]],
                llm_output={"model_name": "gpt-4o-mini"},
            ),
            run_id=run_uuid,
        )

    summary_path = cost_tracker.DEFAULT_RUN_DIR / "smoke_run.json"
    assert summary_path.exists()
    payload = json.loads(summary_path.read_text())
    assert payload["run_id"] == "smoke_run"
    assert payload["calls"] == 1
    assert payload["models"]["gpt-4o-mini"]["calls"] == 1
