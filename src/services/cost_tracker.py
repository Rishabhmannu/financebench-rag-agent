"""Cost tracker for every LLM call routed through LLMFactory.

Records token usage and computed USD cost from LangChain's normalized
`AIMessage.usage_metadata` (cross-provider). Append-only JSONL log plus a
per-run summary JSON. Built to keep Sprint 7.6 / 7.7 within the $75 Anthropic
budget cap during long FinanceBench evaluations.

Programmatic usage:

    from src.services.cost_tracker import CostTracker
    with CostTracker.run("sprint_7_6_day1_claude_baseline"):
        ...  # any LLMFactory.get_*().invoke(...) inside is tagged

Env-driven usage (no code changes required):

    RAG_COST_RUN_ID=sprint_7_6_day1_claude_baseline python tests/evaluation/run_financebench.py ...

CLI:

    python -m src.services.cost_tracker                       # summary, all runs
    python -m src.services.cost_tracker --run-id <run>        # single run
    python -m src.services.cost_tracker --tail 20             # last N raw records
    python -m src.services.cost_tracker --budget 75           # exits 1 if total spend exceeds cap
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


# Pricing in USD per 1M tokens. Source: provider pricing pages, ~2026-04.
# Cache columns are Anthropic-specific (write = creation cost premium, read =
# discounted hit cost). OpenAI applies its cache discount on `input_token_details.cache_read`.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-4-7": {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
    # OpenAI chat
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075},
    "gpt-4o": {"input": 2.50, "output": 10.00, "cache_read": 1.25},
    # OpenAI embeddings (output cost = 0; recorded as input only)
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    # Groq free tier — tokens still recorded for telemetry
    "llama-3.3-70b-versatile": {"input": 0.0, "output": 0.0},
    "llama-3.1-70b-versatile": {"input": 0.0, "output": 0.0},
}

DEFAULT_LOG_DIR = Path("cost_logs")
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "cost_log.jsonl"
DEFAULT_RUN_DIR = DEFAULT_LOG_DIR / "by_run"


def normalize_model(model: str) -> str:
    """Map a provider-returned model id to a key in MODEL_PRICING.

    Providers append date stamps (`gpt-4o-mini-2024-07-18`,
    `claude-sonnet-4-6-20251022`) — strip them by longest-prefix match against
    the pricing table. Returns the model name unchanged if no prefix matches.
    """
    if model in MODEL_PRICING:
        return model
    candidates = [k for k in MODEL_PRICING if model.startswith(k)]
    if not candidates:
        return model
    return max(candidates, key=len)


def compute_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Compute USD cost for one LLM call.

    Anthropic semantics: cache_read tokens are billed at the discounted read rate;
    cache_write at the premium write rate; remainder of input_tokens at the
    standard input rate. OpenAI semantics: cached tokens are billed at the
    `cache_read` rate, the rest at the standard input rate.
    """
    pricing = MODEL_PRICING.get(normalize_model(model))
    if not pricing:
        return 0.0
    cache_read = max(cache_read_tokens, 0)
    cache_write = max(cache_write_tokens, 0)
    plain_input = max(input_tokens - cache_read - cache_write, 0)
    cost = (
        plain_input * pricing.get("input", 0.0)
        + cache_read * pricing.get("cache_read", pricing.get("input", 0.0))
        + cache_write * pricing.get("cache_write", pricing.get("input", 0.0))
        + output_tokens * pricing.get("output", 0.0)
    ) / 1_000_000
    return round(max(cost, 0.0), 6)


class _ActiveContext:
    """Module-level singleton tracking the active run_id across threads."""

    _lock = threading.Lock()
    _run_id: str | None = None
    _log_path: Path = DEFAULT_LOG_FILE

    @classmethod
    def set(cls, run_id: str | None, log_path: Path | None = None) -> None:
        with cls._lock:
            cls._run_id = run_id
            if log_path is not None:
                cls._log_path = log_path

    @classmethod
    def get(cls) -> tuple[str | None, Path]:
        with cls._lock:
            return cls._run_id, cls._log_path


class CostCallbackHandler(BaseCallbackHandler):
    """LangChain callback that captures token usage on every LLM call.

    Reads `usage_metadata` from the response (langchain-core 0.3+, normalized
    across providers). Falls back to provider-specific `llm_output` shapes.
    Failures are swallowed — telemetry must never break the pipeline.
    """

    raise_error = False

    def __init__(self) -> None:
        self._starts: dict[UUID, tuple[float, str | None, dict | None]] = {}
        self._lock = threading.Lock()

    def on_llm_start(
        self,
        serialized: dict,
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> None:
        invocation = kwargs.get("invocation_params") or {}
        serialized_kwargs = (serialized or {}).get("kwargs", {}) or {}
        model = (
            invocation.get("model")
            or invocation.get("model_name")
            or serialized_kwargs.get("model")
            or serialized_kwargs.get("model_name")
        )
        with self._lock:
            self._starts[run_id] = (time.monotonic(), model, metadata or {})

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        return self.on_llm_start(*args, **kwargs)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            start_time, start_model, metadata = self._starts.pop(
                run_id, (time.monotonic(), None, {})
            )
        duration = time.monotonic() - start_time

        try:
            usage = self._extract_usage(response)
            raw_model = self._extract_model(response) or start_model or "unknown"
            model = normalize_model(raw_model)
            cost = compute_cost(
                model=model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_read_tokens=usage["cache_read_tokens"],
                cache_write_tokens=usage["cache_write_tokens"],
            )

            active_run_id, log_path = _ActiveContext.get()
            record = {
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "run_id": active_run_id,
                "model": model,
                "model_raw": raw_model if raw_model != model else None,
                "duration_s": round(duration, 3),
                "cost_usd": cost,
                "node": (metadata or {}).get("node"),
                **usage,
            }
            self._append(log_path, record)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[cost_tracker] on_llm_end failed: {exc}\n")

    @staticmethod
    def _extract_usage(response: LLMResult) -> dict[str, int]:
        out = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}

        for gen_list in response.generations:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                usage = getattr(msg, "usage_metadata", None) if msg else None
                if not usage:
                    continue
                out["input_tokens"] += int(usage.get("input_tokens") or 0)
                out["output_tokens"] += int(usage.get("output_tokens") or 0)
                details = usage.get("input_token_details") or {}
                out["cache_read_tokens"] += int(details.get("cache_read") or 0)
                out["cache_write_tokens"] += int(details.get("cache_creation") or 0)

        if any(out.values()):
            return out

        token_usage = (response.llm_output or {}).get("token_usage", {}) or {}
        if token_usage:
            out["input_tokens"] = int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0)
            out["output_tokens"] = int(token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0)
            details = token_usage.get("prompt_tokens_details") or {}
            out["cache_read_tokens"] = int(details.get("cached_tokens") or 0)

        return out

    @staticmethod
    def _extract_model(response: LLMResult) -> str | None:
        llm_output = response.llm_output or {}
        return llm_output.get("model_name") or llm_output.get("model")

    @staticmethod
    def _append(path: Path, record: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[cost_tracker] log write failed: {exc}\n")


_GLOBAL_HANDLER = CostCallbackHandler()


def get_cost_handler() -> CostCallbackHandler:
    """Singleton callback handler. Attach to every LLM created by LLMFactory."""
    return _GLOBAL_HANDLER


class CostTracker:
    """Public API: name a run, write its summary, query aggregated spend."""

    @staticmethod
    @contextlib.contextmanager
    def run(run_id: str, log_path: Path | None = None):
        prev_run, prev_path = _ActiveContext.get()
        _ActiveContext.set(run_id, log_path)
        try:
            yield run_id
        finally:
            CostTracker.write_run_summary(run_id, log_path or prev_path)
            _ActiveContext.set(prev_run, prev_path)

    @staticmethod
    def start_run(run_id: str, log_path: Path | None = None) -> None:
        _ActiveContext.set(run_id, log_path)

    @staticmethod
    def end_run() -> Path | None:
        run_id, log_path = _ActiveContext.get()
        out = CostTracker.write_run_summary(run_id, log_path) if run_id else None
        _ActiveContext.set(None)
        return out

    @staticmethod
    def summarize(run_id: str | None = None, log_path: Path | None = None) -> dict:
        path = log_path or _ActiveContext.get()[1]
        if not path.exists():
            return {"runs": {}, "totals": {"cost_usd": 0.0, "calls": 0}}
        per_run: dict[str | None, dict[str, dict[str, float]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(float))
        )
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if run_id is not None and rec.get("run_id") != run_id:
                    continue
                rid = rec.get("run_id")
                model = rec.get("model", "unknown")
                bucket = per_run[rid][model]
                bucket["calls"] += 1
                bucket["cost_usd"] += float(rec.get("cost_usd") or 0.0)
                bucket["input_tokens"] += int(rec.get("input_tokens") or 0)
                bucket["output_tokens"] += int(rec.get("output_tokens") or 0)
                bucket["cache_read_tokens"] += int(rec.get("cache_read_tokens") or 0)
                bucket["cache_write_tokens"] += int(rec.get("cache_write_tokens") or 0)

        out: dict[str, Any] = {"runs": {}, "totals": {"cost_usd": 0.0, "calls": 0}}
        for rid, models in per_run.items():
            run_total = sum(m["cost_usd"] for m in models.values())
            run_calls = sum(m["calls"] for m in models.values())
            out["runs"][str(rid)] = {
                "models": {model: dict(stats) for model, stats in models.items()},
                "cost_usd": round(run_total, 4),
                "calls": int(run_calls),
            }
            out["totals"]["cost_usd"] += run_total
            out["totals"]["calls"] += int(run_calls)
        out["totals"]["cost_usd"] = round(out["totals"]["cost_usd"], 4)
        return out

    @staticmethod
    def write_run_summary(run_id: str, log_path: Path | None = None) -> Path | None:
        summary = CostTracker.summarize(run_id=run_id, log_path=log_path)
        run_data = summary["runs"].get(run_id)
        if not run_data:
            return None
        DEFAULT_RUN_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DEFAULT_RUN_DIR / f"{run_id}.json"
        payload = {
            "run_id": run_id,
            "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            **run_data,
        }
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return out_path


def _auto_init_from_env() -> None:
    run_id = os.environ.get("RAG_COST_RUN_ID")
    log_path = os.environ.get("RAG_COST_LOG_PATH")
    if run_id:
        _ActiveContext.set(run_id, Path(log_path) if log_path else None)


_auto_init_from_env()


def _format_summary(summary: dict) -> str:
    runs = summary["runs"]
    if not runs:
        return "No cost records found."
    lines: list[str] = []
    for rid, data in sorted(runs.items()):
        lines.append("")
        lines.append(f"=== {rid} ===")
        lines.append(f"  total: ${data['cost_usd']:.4f}  ({data['calls']} calls)")
        for model, stats in sorted(data["models"].items(), key=lambda kv: -kv[1]["cost_usd"]):
            lines.append(
                f"    {model:<35} ${stats['cost_usd']:>9.4f}  "
                f"calls={int(stats['calls']):<5} "
                f"in={int(stats['input_tokens']):>9,}  "
                f"out={int(stats['output_tokens']):>8,}  "
                f"cache_r={int(stats['cache_read_tokens']):>9,}"
            )
    lines.append("")
    lines.append(f"GRAND TOTAL: ${summary['totals']['cost_usd']:.4f}  ({summary['totals']['calls']} calls)")
    return "\n".join(lines)


def _cli() -> int:
    p = argparse.ArgumentParser(prog="cost_tracker", description="Aggregate LLM cost from cost_log.jsonl")
    p.add_argument("--run-id", help="Filter to a specific run_id")
    p.add_argument("--log-path", default=str(DEFAULT_LOG_FILE), help="Path to cost log")
    p.add_argument("--tail", type=int, help="Print the last N raw records")
    p.add_argument("--budget", type=float, help="Exit 1 if total spend exceeds this USD cap")
    args = p.parse_args()

    log_path = Path(args.log_path)

    if args.tail:
        if not log_path.exists():
            print("No log yet.")
            return 0
        with log_path.open(encoding="utf-8") as f:
            lines = f.readlines()[-args.tail:]
        for line in lines:
            print(line.rstrip())
        return 0

    summary = CostTracker.summarize(run_id=args.run_id, log_path=log_path)
    print(_format_summary(summary))

    if args.budget is not None and summary["totals"]["cost_usd"] > args.budget:
        print(f"\n!! BUDGET EXCEEDED: ${summary['totals']['cost_usd']:.4f} > ${args.budget:.2f} !!")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
