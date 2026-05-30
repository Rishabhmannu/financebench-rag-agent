"""financebench upgrade — pull repo updates + rebuild api + restart stack.

Section 18.3.3 of DEPLOYMENT_PLAN.md. Cookbook entry for the common
"the maintainer pushed a new prompt / reranker / bug fix; how do I get
it?" workflow.

Preserves volumes (so chat history + ingested corpora + cost logs all
survive). Refuses if the cloned repo has uncommitted local changes —
don't want to silently overwrite user edits.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

import typer

from cli.commands.setup import DEFAULT_CLONE_PATH
from cli.render import console, render_error, render_info, render_success


def upgrade(
    full: bool = typer.Option(False, "--full", help="Target docker-compose.yml (11 services) instead of compose.minimal.yml."),
    repo_dir: str = typer.Option(None, "--repo-dir"),
    force: bool = typer.Option(False, "--force", help="Allow upgrade even if the cloned repo has uncommitted changes (potentially destructive)."),
) -> None:
    """Pull latest repo + rebuild api image + restart stack."""
    path = _resolve_repo(repo_dir)
    compose_file = "docker-compose.yml" if full else "compose.minimal.yml"
    if not (path / compose_file).exists():
        render_error(f"{compose_file} not found at {path}")
        raise typer.Exit(1)

    _check_git_clean(path, allow_dirty=force)
    _git_pull(path)
    _compose_pull(path, compose_file)
    _compose_build_api(path, compose_file)
    _compose_up(path, compose_file)
    _wait_for_health()
    render_success("Upgrade complete. Volumes preserved — chat history + ingested corpora intact.")
    console.print("[dim]Next: financebench chat   (the new build is live; banner will show the updated sha)[/dim]")


def _resolve_repo(repo_dir: str | None) -> Path:
    if repo_dir:
        return Path(repo_dir).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() and (cwd / "compose.minimal.yml").exists():
        return cwd
    return DEFAULT_CLONE_PATH


def _check_git_clean(path: Path, allow_dirty: bool) -> None:
    if not (path / ".git").exists():
        render_info(f"{path} is not a git checkout; skipping git pull (image rebuild only).")
        return
    r = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        render_error("git status failed; refusing to continue.")
        raise typer.Exit(1)
    if r.stdout.strip() and not allow_dirty:
        render_error(
            f"Uncommitted changes in {path}:\n{r.stdout}\n"
            f"Stash, commit, or pass --force to upgrade anyway."
        )
        raise typer.Exit(1)


def _git_pull(path: Path) -> None:
    if not (path / ".git").exists():
        return
    render_info(f"git pull in {path} ...")
    r = subprocess.run(["git", "pull", "--ff-only"], cwd=path, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        render_error(f"git pull failed:\n{r.stdout}\n{r.stderr}")
        raise typer.Exit(1)
    console.print(f"[dim]{r.stdout.strip()}[/dim]")


def _compose_pull(path: Path, compose_file: str) -> None:
    """Pull updates for pinned images (qdrant, postgres, redis-stack).
    Doesn't pull the locally-built api image; that's handled by `build`."""
    render_info("docker compose pull (pinned images: qdrant, postgres, redis-stack, ...) ...")
    rc = subprocess.run(["docker", "compose", "-f", compose_file, "pull"], cwd=path, check=False).returncode
    if rc != 0:
        render_info("compose pull returned non-zero — usually fine (some services have no upstream image).")


def _compose_build_api(path: Path, compose_file: str) -> None:
    render_info("docker compose build api (incorporates the pulled source) ...")
    rc = subprocess.run(
        ["docker", "compose", "-f", compose_file, "build", "api"],
        cwd=path,
        check=False,
    ).returncode
    if rc != 0:
        render_error(f"docker compose build api failed (exit {rc}).")
        raise typer.Exit(1)


def _compose_up(path: Path, compose_file: str) -> None:
    render_info("docker compose up -d (recreates containers; data volumes preserved) ...")
    rc = subprocess.run(
        ["docker", "compose", "-f", compose_file, "up", "-d", "--force-recreate", "api"],
        cwd=path,
        check=False,
    ).returncode
    if rc != 0:
        render_error(f"docker compose up failed (exit {rc}).")
        raise typer.Exit(1)


def _wait_for_health(timeout_s: int = 360, interval_s: int = 5) -> None:
    import httpx
    started = time.monotonic()
    with console.status("Waiting for /v1/health after upgrade...", spinner="dots") as ui:
        while time.monotonic() - started < timeout_s:
            try:
                if httpx.get("http://localhost:8000/v1/health", timeout=5.0).status_code == 200:
                    ui.stop()
                    elapsed = int(time.monotonic() - started)
                    render_success(f"Healthy after {elapsed}s.")
                    return
            except Exception:
                pass
            time.sleep(interval_s)
    render_error(f"API not healthy within {timeout_s}s after upgrade. Check logs.")
    raise typer.Exit(1)
