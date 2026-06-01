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
            "Exactly one of --sample / --dir / --from-hf is required."
        ),
    ),
    from_hf: str = typer.Option(
        None,
        "--from-hf",
        help=(
            "Pull a pre-vectorized corpus from a HuggingFace Hub dataset slug "
            "(e.g. cmpunkmannu/financebench-voyage-finance-2-embeddings). Skips parsing + "
            "embedding entirely. ~1-3 min download + ~1 min restore on 68k chunks."
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
    revision: str = typer.Option(
        None,
        "--revision",
        help="HF dataset revision (branch/tag/commit). Only with --from-hf. Default: main.",
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
    """Seed the active Qdrant collection with PDFs (sample, your own, or a pre-vectorized HF snapshot).

    Examples:
      financebench seed --sample
      financebench seed --dir data/raw/my-corpus
      financebench seed --dir ~/.financebench/repo/data/acme --collection acme_q3_2026
      financebench seed --from-hf cmpunkmannu/financebench-voyage-finance-2-embeddings

    Important (--dir mode): --dir must point inside <repo>/data/. The api container
    can only read what's bind-mounted from `./data` (per compose.minimal.yml). If
    your PDFs live elsewhere, move/copy them under data/ first.

    --from-hf mode: downloads a parquet + manifest from the HF Hub and bulk-upserts
    into Qdrant. Skips PDF parsing + embedding entirely. The snapshot must have been
    produced by the same embedding model your pipeline expects (the manifest is
    checked at restore time and the script aborts on dim/distance mismatch).

    Caveat (--dir / --from-hf modes): the tuned prompts + reranker are FinanceBench-
    specific, so accuracy on non-FB corpora may differ from the 72.67% headline.
    Use --collection to keep your corpus separate from the eval / demo collections.
    """
    chosen = sum(1 for x in (sample, bool(dir), bool(from_hf)) if x)
    if chosen != 1:
        render_error("Provide exactly one of --sample, --dir <path>, or --from-hf <slug>.")
        raise typer.Exit(2)

    if revision and not from_hf:
        render_error("--revision only applies with --from-hf.")
        raise typer.Exit(2)

    if not shutil.which("docker"):
        render_error("Docker is not installed or not on PATH. Run `financebench doctor`.")
        raise typer.Exit(1)

    repo = _resolve_repo(repo_dir)
    compose_file = "docker-compose.yml" if full else "compose.minimal.yml"
    if not (repo / compose_file).exists():
        render_error(f"{compose_file} not found at {repo}")
        raise typer.Exit(1)

    if from_hf:
        script_args = ["--from-hf", from_hf]
        if revision:
            script_args.extend(["--revision", revision])
        if collection:
            script_args.extend(["--collection", collection])
        script_name = "scripts/seed_from_hf.py"
    else:
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
        script_name = "scripts/seed_qdrant.py"

    cmd = [
        "docker", "compose", "-f", compose_file,
        "exec", "-T", "api",
        "python", script_name,
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
