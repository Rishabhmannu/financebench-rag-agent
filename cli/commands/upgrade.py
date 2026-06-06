"""financebench upgrade — pull repo updates + rebuild api + restart stack.

Section 18.3.3 of DEPLOYMENT_PLAN.md. Cookbook entry for the common
"the maintainer pushed a new prompt / reranker / bug fix; how do I get
it?" workflow.

Preserves volumes (so chat history + ingested corpora + cost logs all
survive). Refuses if the cloned repo has uncommitted local changes —
don't want to silently overwrite user edits.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import typer

from cli.commands.setup import DEFAULT_CLONE_PATH
from cli.render import console, render_error, render_info, render_success


_NOTICES_FILE = Path.home() / ".financebench" / "upgrade_notices_seen.json"


# 0.3.2: one-time upgrade notices keyed by a stable slug. Printed once per
# CLI install, persisted to ~/.financebench/upgrade_notices_seen.json. Add
# new entries when shipping a behavior change that doesn't break anything
# in the default flow but matters for a subset of users (e.g., PII-sensitive
# or extra-dependent workloads). Keep the body short — five lines max — and
# always include the release notes URL so the user can read the full story.
_NOTICES: list[tuple[str, str]] = [
    (
        "0.3.1-spacy-docling",
        (
            "[bold]0.3.1 changed two backend defaults[/bold] (image pull is ~530 MB smaller):\n"
            "  • spaCy [bold]en_core_web_lg → en_core_web_md[/bold]. PERSON PII recall is\n"
            "    identical on full names; drops ~20pp on single-name references like\n"
            "    \"Buffett\" or \"Dimon\". For compliance/HR/legal workloads, set\n"
            "    [cyan]USE_LARGE_SPACY_MODEL=1[/cyan] in .env and run:\n"
            "      [dim]docker exec <api-container> python -m spacy download en_core_web_lg[/dim]\n"
            "  • [bold]docling[/bold] moved to optional [cyan]\\[docling][/cyan] extra. Default ingestion uses\n"
            "    pypdf (canonical anyway). Install with: [dim]pip install \".\\[docling]\"[/dim]\n"
            "  Release notes: https://github.com/Rishabhmannu/financebench-rag-agent/releases/tag/v0.3.1"
        ),
    ),
]


def _show_pending_notices() -> None:
    """Print any one-time notices the user hasn't acknowledged yet, then
    record them as seen so next upgrade doesn't repeat the same message."""
    seen: set[str] = set()
    if _NOTICES_FILE.exists():
        try:
            seen = set(json.loads(_NOTICES_FILE.read_text()).get("seen", []))
        except (OSError, ValueError):
            pass

    for slug, body in _NOTICES:
        if slug in seen:
            continue
        console.print()
        console.print(f"[yellow]ℹ  one-time notice (you'll see this once):[/yellow]")
        console.print(body)
        console.print()
        seen.add(slug)

    try:
        _NOTICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        _NOTICES_FILE.write_text(json.dumps({"seen": sorted(seen)}, indent=2) + "\n")
    except OSError:
        # Non-fatal — worst case the notice fires again on next upgrade.
        pass


def upgrade(
    full: bool = typer.Option(False, "--full", help="Target docker-compose.yml (11 services) instead of compose.minimal.yml."),
    repo_dir: str = typer.Option(None, "--repo-dir"),
    force: bool = typer.Option(False, "--force", help="Discard local changes in the cloned repo (hard-reset to the latest) before upgrading."),
    build: bool = typer.Option(
        False,
        "--build",
        help="Build the api image from source instead of pulling the pre-built GHCR image. Also triggered by BUILD_FROM_SOURCE=1 env var.",
    ),
) -> None:
    """Pull latest repo + refresh api image + restart stack.

    Default: docker compose pull (uses the pre-built GHCR image). Use --build
    or BUILD_FROM_SOURCE=1 to build from source (slower, for dev / pre-release
    testing).
    """
    path = _resolve_repo(repo_dir)
    compose_file = "docker-compose.yml" if full else "compose.minimal.yml"
    if not (path / compose_file).exists():
        render_error(f"{compose_file} not found at {path}")
        raise typer.Exit(1)

    # 0.3.2: print any one-time upgrade notices the user hasn't acknowledged
    # yet. Done before the pull so they can read it while git + docker work.
    _show_pending_notices()

    _check_git_clean(path, allow_dirty=force)
    _git_pull(path, force=force)
    _compose_pull(path, compose_file)
    # 0.2.0: build is opt-in. Default flow uses the pre-built GHCR image
    # pulled in the step above.  --build  or  BUILD_FROM_SOURCE=1  forces
    # local build from source (slower, ~10 min on M1 with cold cache).
    if build or os.environ.get("BUILD_FROM_SOURCE") == "1":
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


def _git_pull(path: Path, force: bool = False) -> None:
    if not (path / ".git").exists():
        return
    if force:
        # --force means "I don't care about local edits in this managed clone."
        # A plain ff-only pull aborts when tracked files differ (e.g. demo gifs
        # regenerated in place during local testing), so fetch and hard-reset to
        # the upstream branch — that's what makes --force actually able to
        # upgrade a dirty clone instead of hitting the same wall.
        render_info(f"git fetch + hard reset to upstream in {path} (--force) ...")
        f = subprocess.run(["git", "fetch", "--tags", "origin"], cwd=path, capture_output=True, text=True, check=False)
        if f.returncode != 0:
            render_error(f"git fetch failed:\n{f.stdout}\n{f.stderr}")
            raise typer.Exit(1)
        r = subprocess.run(["git", "reset", "--hard", "@{u}"], cwd=path, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            render_error(f"git reset --hard failed:\n{r.stdout}\n{r.stderr}")
            raise typer.Exit(1)
        console.print(f"[dim]{r.stdout.strip()}[/dim]")
        return
    render_info(f"git pull in {path} ...")
    r = subprocess.run(["git", "pull", "--ff-only"], cwd=path, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        render_error(
            f"git pull failed:\n{r.stdout}\n{r.stderr}\n"
            f"If this clone has local changes you don't need, re-run with --force."
        )
        raise typer.Exit(1)
    console.print(f"[dim]{r.stdout.strip()}[/dim]")


def _compose_pull(path: Path, compose_file: str) -> None:
    """Pull updates for all images including the api (from GHCR).

    0.2.0: the api service now has an `image:` directive pointing at GHCR;
    pull fetches it for the matching FB_IMAGE_TAG. Pre-0.2.0 the api was
    built locally and this step skipped it.
    """
    # Same FB_IMAGE_TAG threading as setup.py — the compose file's
    # ${FB_IMAGE_TAG:-...} substitution needs this in the env so the right
    # version of the api image gets pulled.
    from cli import __version__ as cli_version  # noqa: PLC0415

    env = os.environ.copy()
    env["FB_IMAGE_TAG"] = cli_version

    render_info(
        f"docker compose pull (qdrant, postgres, redis-stack, api:{cli_version} from GHCR) ..."
    )
    rc = subprocess.run(
        ["docker", "compose", "-f", compose_file, "pull"],
        cwd=path,
        env=env,
        check=False,
    ).returncode
    if rc != 0:
        render_info(
            "compose pull returned non-zero. If this was a GHCR auth/network "
            "error, set BUILD_FROM_SOURCE=1 to fall back to a local source build."
        )


def _compose_build_api(path: Path, compose_file: str) -> None:
    """Build the api image from source.

    0.1.5 added GIT_SHA threading to cli/commands/setup.py:_bring_up_stack
    but this second call site was missed for three releases — banner reported
    `sha unknown` on every container started via `financebench upgrade`. Fix
    is the same pattern: capture host git sha, export to env, docker compose
    substitutes into build.args.GIT_SHA, Dockerfile ARG → ENV carries it.
    Same "fixed-one-call-site-missed-the-other" bug class as 0.1.3 guardrails
    and 0.1.6 event_log; documented as recurring meta-lesson in
    docs/engineering-log.md.
    """
    env = os.environ.copy()
    import shutil  # noqa: PLC0415

    if shutil.which("git"):
        try:
            env["GIT_SHA"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:  # noqa: BLE001
            pass

    render_info("docker compose build api (incorporates the pulled source) ...")
    rc = subprocess.run(
        ["docker", "compose", "-f", compose_file, "build", "api"],
        cwd=path,
        env=env,
        check=False,
    ).returncode
    if rc != 0:
        render_error(f"docker compose build api failed (exit {rc}).")
        raise typer.Exit(1)


def _compose_up(path: Path, compose_file: str) -> None:
    # 0.2.0: same FB_IMAGE_TAG threading as _compose_pull — `up -d
    # --force-recreate` re-resolves the image: directive against the env,
    # so we need the matching version set here too.
    from cli import __version__ as cli_version  # noqa: PLC0415

    env = os.environ.copy()
    env["FB_IMAGE_TAG"] = cli_version

    render_info("docker compose up -d (recreates containers; data volumes preserved) ...")
    rc = subprocess.run(
        ["docker", "compose", "-f", compose_file, "up", "-d", "--force-recreate", "api"],
        cwd=path,
        env=env,
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
