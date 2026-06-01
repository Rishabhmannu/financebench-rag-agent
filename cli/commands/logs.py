"""financebench logs — view api container logs.

In 0.2.2 the api's /app/logs and /app/cost_logs moved from host bind mounts
to named volumes (api_logs, api_cost_logs) so the container's appuser owns
them regardless of host UID. Trade-off: host-side `tail logs/run_*.jsonl`
no longer works. This command is the replacement.

`financebench logs` tails docker compose's stdout/stderr capture (uvicorn
output + request log lines). For the structured event-log JSONL files
written by src/services/event_log.py, use `financebench logs --event-log
[--follow]` which docker-execs into the container and tails the latest
run_<timestamp>.jsonl.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import typer

from cli.commands.setup import DEFAULT_CLONE_PATH
from cli.render import console, render_error, render_info


def logs(
    follow: bool = typer.Option(
        False,
        "--follow",
        "-f",
        help="Stream new lines as they're written (tail -f).",
    ),
    tail: int = typer.Option(
        200,
        "--tail",
        "-n",
        help="Show the last N lines before optional follow.",
    ),
    event_log: bool = typer.Option(
        False,
        "--event-log",
        help=(
            "Show the structured JSONL event log instead of the docker compose "
            "stdout. Sources from /app/logs/run_<latest>.jsonl inside the container."
        ),
    ),
    repo_dir: str = typer.Option(
        None,
        "--repo-dir",
        help="Path to your financebench repo checkout. Defaults to ~/.financebench/repo or cwd.",
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Use docker-compose.yml (11 services) instead of compose.minimal.yml.",
    ),
) -> None:
    """View backend logs (uvicorn output or the JSONL event log)."""
    if not shutil.which("docker"):
        render_error("Docker is not installed or not on PATH. Run `financebench doctor`.")
        raise typer.Exit(1)

    repo = _resolve_repo(repo_dir)
    compose_file = "docker-compose.yml" if full else "compose.minimal.yml"
    if not (repo / compose_file).exists():
        render_error(f"{compose_file} not found at {repo}")
        raise typer.Exit(1)

    if event_log:
        # ls -t /app/logs/run_*.jsonl | head -1, then tail it.
        shell_cmd = (
            f"latest=$(ls -t /app/logs/run_*.jsonl 2>/dev/null | head -n1); "
            f"if [ -z \"$latest\" ]; then echo 'no event log files yet — make a request first.' >&2; exit 1; fi; "
            f"echo \"--- $latest ---\" >&2; "
            f"tail -n {tail} {'-f ' if follow else ''}\"$latest\""
        )
        cmd = ["docker", "compose", "-f", compose_file, "exec", "-T", "api", "sh", "-c", shell_cmd]
    else:
        cmd = ["docker", "compose", "-f", compose_file, "logs", "api", "--tail", str(tail)]
        if follow:
            cmd.append("--follow")

    render_info(f"Running: {' '.join(shlex.quote(c) for c in cmd)}")
    rc = subprocess.run(cmd, cwd=repo, check=False).returncode
    if rc != 0 and not follow:
        # When --follow is used, Ctrl-C returns non-zero; that's expected.
        raise typer.Exit(rc)


def _resolve_repo(repo_dir: str | None) -> Path:
    if repo_dir:
        return Path(repo_dir).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() and (cwd / "compose.minimal.yml").exists():
        return cwd
    return DEFAULT_CLONE_PATH
