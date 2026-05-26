"""financebench down — stop the docker compose stack.

Locates the same repo dir as `setup` (current dir / ~/.financebench/repo)
and runs `docker compose -f <compose> down`. Preserves volumes by default;
pass --volumes to nuke them (destroys all conversation history, ingested
corpora, cost logs).
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import typer

from cli.commands.setup import DEFAULT_CLONE_PATH
from cli.render import render_error, render_info, render_success


def down(
    full: bool = typer.Option(False, "--full", help="Target docker-compose.yml (11 services) instead of compose.minimal.yml."),
    repo_dir: str = typer.Option(None, "--repo-dir"),
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Also remove named volumes (destroys all data — chat history, ingested corpora, cost logs)."),
) -> None:
    """Stop the docker compose stack started by `financebench setup`."""
    path = _resolve_repo(repo_dir)
    compose_file = "docker-compose.yml" if full else "compose.minimal.yml"
    if not (path / compose_file).exists():
        render_error(f"{compose_file} not found at {path}")
        raise typer.Exit(1)

    cmd = ["docker", "compose", "-f", compose_file, "down"]
    if volumes:
        cmd.append("--volumes")
        render_info("--volumes set: WILL DELETE postgres + qdrant + redis + hf_cache data.")
    render_info(f"Running: {' '.join(shlex.quote(c) for c in cmd)}")
    rc = subprocess.run(cmd, cwd=path, check=False).returncode
    if rc != 0:
        render_error(f"docker compose down failed (exit {rc}).")
        raise typer.Exit(1)
    render_success("Stack stopped." + (" Volumes removed." if volumes else " Volumes preserved."))


def _resolve_repo(repo_dir: str | None) -> Path:
    if repo_dir:
        return Path(repo_dir).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() and (cwd / "compose.minimal.yml").exists():
        return cwd
    return DEFAULT_CLONE_PATH
