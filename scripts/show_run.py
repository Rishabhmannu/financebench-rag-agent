"""Render a pipeline run's JSONL event log as a human-readable timeline.

Usage:
    # Show the boot banner + per-question events from the most recent run:
    python scripts/show_run.py latest

    # Specific run by file path or run_id:
    python scripts/show_run.py logs/run_20260515_123456.jsonl
    python scripts/show_run.py run_20260515_123456

    # Filter to a single question across all stages:
    python scripts/show_run.py latest --fb-id financebench_id_03029

    # Filter to a single stage across all questions:
    python scripts/show_run.py latest --stage grader

    # Both filters:
    python scripts/show_run.py latest --fb-id financebench_id_03029 --stage retrieval

    # Render only the boot banner (skip per-Q events):
    python scripts/show_run.py latest --banner-only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs"


def _resolve_path(spec: str) -> Path:
    """Accept (a) absolute/relative path, (b) bare run_id, or (c) the literal
    'latest' to mean the most recent .jsonl in LOGS_DIR."""
    if spec == "latest":
        candidates = sorted(LOGS_DIR.glob("run_*.jsonl"))
        if not candidates:
            print(f"No logs/*.jsonl found under {LOGS_DIR}", file=sys.stderr)
            sys.exit(2)
        return candidates[-1]
    p = Path(spec)
    if p.exists():
        return p
    # Try as run_id (no .jsonl suffix)
    candidate = LOGS_DIR / f"{spec}.jsonl"
    if candidate.exists():
        return candidate
    candidate = LOGS_DIR / spec
    if candidate.exists():
        return candidate
    print(f"Not found: {spec} (looked in {LOGS_DIR})", file=sys.stderr)
    sys.exit(2)


def _format_fields(rec: dict) -> str:
    """Pretty-print all non-meta fields for one event, abridged for terminal width."""
    skip = {"ts", "run_id", "stage", "fb_id"}
    fields = {k: v for k, v in rec.items() if k not in skip}
    if not fields:
        return ""
    return json.dumps(fields, default=str)


def _render_banner(rec: dict) -> None:
    """Render the runtime_components event as a multi-line banner."""
    sep = "=" * 78
    print(sep)
    print(f"RUNTIME BANNER  ({rec.get('ts')})")
    print(f"run_id = {rec.get('run_id')}")
    print(sep)
    for section in ("git", "external", "components_loaded", "settings", "env_relevant"):
        v = rec.get(section)
        if not v:
            continue
        print(f"\n[{section}]")
        if isinstance(v, dict):
            for k, vv in v.items():
                print(f"  {k}: {vv}")
        else:
            print(f"  {v}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", help="path | run_id | 'latest'")
    parser.add_argument("--fb-id", default=None, help="Filter to a single question's events")
    parser.add_argument("--stage", default=None, help="Filter to a single stage (e.g. 'grader')")
    parser.add_argument("--banner-only", action="store_true", help="Only print the boot banner")
    parser.add_argument("--no-banner", action="store_true", help="Skip the boot banner")
    args = parser.parse_args()

    path = _resolve_path(args.run)
    print(f"# log: {path}", file=sys.stderr)

    banner = None
    by_fb: dict[str, list[dict]] = {}
    pre_startup: list[dict] = []

    for line in path.open():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("stage") == "runtime_components":
            banner = rec
            continue
        if args.fb_id and rec.get("fb_id") != args.fb_id:
            continue
        if args.stage and rec.get("stage") != args.stage:
            continue
        fid = rec.get("fb_id")
        if fid is None:
            pre_startup.append(rec)
        else:
            by_fb.setdefault(fid, []).append(rec)

    if banner and not args.no_banner:
        _render_banner(banner)
        if args.banner_only:
            return

    if pre_startup and not args.fb_id:
        print(f"=== pre-startup ({len(pre_startup)} events) ===")
        for e in pre_startup:
            ts = e["ts"].split("T")[1][:12] if "T" in e["ts"] else e["ts"][:12]
            print(f"  [{ts}] {e['stage']:<32} {_format_fields(e)[:160]}")
        print()

    for fid, events in by_fb.items():
        print(f"=== {fid} ({len(events)} events) ===")
        for e in events:
            ts = e["ts"].split("T")[1][:12] if "T" in e["ts"] else e["ts"][:12]
            stage = e["stage"]
            fields = _format_fields(e)
            print(f"  [{ts}] {stage:<32} {fields[:200]}")
        print()


if __name__ == "__main__":
    main()
