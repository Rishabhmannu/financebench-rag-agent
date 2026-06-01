"""financebench seed — ingest PDFs into a Qdrant collection.

Thin wrapper around `scripts/seed_qdrant.py` which already supports
--sample / --dir / --collection at the script level (shipped in 0.1.8).
This command exists so users don't have to type the docker compose exec
incantation. The actual ingestion (PDF parse → chunk → embed → upload)
runs inside the api container's pipeline.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import typer

from cli.commands.setup import DEFAULT_CLONE_PATH
from cli.render import console, render_error, render_info, render_success


def seed(
    sample: bool = typer.Option(
        False,
        "--sample",
        help="Seed the 8-PDF sample corpus shipped with the repo (data/sample/). Cheap demo.",
    ),
    dir: Path = typer.Option(
        None,
        "--dir",
        help=(
            "Path to your own PDF directory. Must be under <repo>/data/ (compose binds "
            "./data → /app/data so anything under it is reachable inside the container). "
            "Either --sample or --dir is required."
        ),
    ),
    collection: str = typer.Option(
        None,
        "--collection",
        help=(
            "Target Qdrant collection name. Defaults to QDRANT_COLLECTION in .env (typically "
            "'financial_docs'). Set this to keep custom corpora separate from the demo / eval collections."
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
    """Seed the active Qdrant collection with PDFs (sample or your own).

    Examples:
      financebench seed --sample
      financebench seed --dir data/raw/my-corpus
      financebench seed --dir ~/.financebench/repo/data/acme --collection acme_q3_2026

    Important: --dir must point inside <repo>/data/. The api container can only
    read what's bind-mounted from `./data` (per compose.minimal.yml). If your PDFs
    live elsewhere, move/copy them under data/ first.

    Caveat: the reranker + prompts are FinanceBench-tuned. Accuracy on unrelated
    finance docs may differ from the 72.67% headline. Use --collection to keep
    your corpus separate from the eval / demo collections.
    """
    if sample == bool(dir):
        # both true or both false → user gave neither or both
        render_error("Provide exactly one of --sample or --dir <path>.")
        raise typer.Exit(2)

    if not shutil.which("docker"):
        render_error("Docker is not installed or not on PATH. Run `financebench doctor`.")
        raise typer.Exit(1)

    repo = _resolve_repo(repo_dir)
    compose_file = "docker-compose.yml" if full else "compose.minimal.yml"
    if not (repo / compose_file).exists():
        render_error(f"{compose_file} not found at {repo}")
        raise typer.Exit(1)

    script_args: list[str] = []
    if sample:
        script_args = ["--sample"]
    else:
        # Translate host path → container path. Compose binds ./data → /app/data.
        host_path = dir.expanduser().resolve()
        repo_data = (repo / "data").resolve()
        try:
            relative = host_path.relative_to(repo_data)
        except ValueError:
            render_error(
                f"--dir must be inside {repo_data}/ (bind-mounted into the api "
                f"container as /app/data/). You provided: {host_path}\n"
                f"Move/copy your PDFs under {repo_data}/ and re-run."
            )
            raise typer.Exit(1)
        container_path = Path("/app/data") / relative
        script_args = ["--dir", str(container_path)]

    if collection:
        script_args.extend(["--collection", collection])

    cmd = [
        "docker", "compose", "-f", compose_file,
        "exec", "-T", "api",
        "python", "scripts/seed_qdrant.py",
        *script_args,
    ]
    render_info(f"Running: {' '.join(shlex.quote(c) for c in cmd)}")
    rc = subprocess.run(cmd, cwd=repo, check=False).returncode
    if rc != 0:
        render_error(
            f"Seed failed (exit {rc}). If the api container isn't running, "
            "start the stack with `financebench setup` or `financebench upgrade`."
        )
        raise typer.Exit(rc)
    render_success("Seed complete.")
    console.print(
        "[dim]Next: financebench chat   (queries will hit the freshly-ingested chunks)[/dim]"
    )


def _resolve_repo(repo_dir: str | None) -> Path:
    """Find the financebench repo. Same precedence as upgrade.py."""
    if repo_dir:
        return Path(repo_dir).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() and (cwd / "compose.minimal.yml").exists():
        return cwd
    return DEFAULT_CLONE_PATH
