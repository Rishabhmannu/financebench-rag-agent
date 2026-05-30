"""financebench setup — first-run wizard.

Phase 4 deliverable per DEPLOYMENT_PLAN.md Section 12. Walks a new user
through: choose/locate the repo, collect API keys, write `.env`, bring
up the minimal docker stack, wait for health, pre-warm models, seed
the sample corpus. Aims for ~5 min wall-clock on a warm Docker cache,
~10 min cold.

`setup --full` flips from `compose.minimal.yml` (4 services) to the
canonical `docker-compose.yml` (11 services with the Langfuse stack)
for users who want the observability UI.

Idempotent: re-running just updates `.env` (if you say yes) and
restarts the stack. Doesn't delete data volumes.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import typer
from prompt_toolkit import prompt as pt_prompt

from cli.render import console, render_error, render_info, render_success

# Where we clone the repo for users who installed via `pip install
# financebench-rag-cli` and don't already have a local git checkout. M1
# architecture per DEPLOYMENT_PLAN.md Section 18.4.
DEFAULT_CLONE_PATH = Path.home() / ".financebench" / "repo"
REPO_URL = "https://github.com/Rishabhmannu/financebench-rag-agent.git"

# 0.1.4: each prompt carries the URL to obtain a key + the expected prefix
# so we can format-check pasted values and warn on typos / wrong-provider
# pastes. Modern terminals (iTerm2, macOS Terminal, VSCode terminal) auto-link
# the URL to clickable. Input is masked at paste time (is_password=True below).
_API_KEY_PROMPTS: list[tuple[str, str, bool, str, tuple[str, ...]]] = [
    ("OPENAI_API_KEY",
     "OpenAI API key (required — embeddings + gpt-4o-mini)",
     True,
     "https://platform.openai.com/api-keys",
     ("sk-proj-", "sk-")),
    ("ANTHROPIC_API_KEY",
     "Anthropic API key (required — Claude Sonnet generator)",
     True,
     "https://console.anthropic.com/settings/keys",
     ("sk-ant-",)),
    ("VOYAGE_API_KEY",
     "Voyage API key (optional — voyage-finance-2 embeddings)",
     False,
     "https://dash.voyageai.com/api-keys",
     ("pa-",)),
    ("GROQ_API_KEY",
     "Groq API key (optional — free tier; falls back to OpenAI)",
     False,
     "https://console.groq.com/keys",
     ("gsk_",)),
]

_HEALTHCHECK_TIMEOUT_S = 360  # cold BGE download can take a few minutes
_HEALTHCHECK_INTERVAL_S = 5


def setup(
    full: bool = typer.Option(
        False, "--full", help="Use the full docker-compose.yml (11 services with Langfuse). Default is the 4-service minimal stack."
    ),
    repo_dir: str = typer.Option(
        None, "--repo-dir", help="Path to an existing repo checkout. Defaults to ~/.financebench/repo (cloned if missing) or the current directory if it looks like the project root."
    ),
    skip_seed: bool = typer.Option(
        False, "--skip-seed", help="Don't run the sample corpus seed at the end (useful for re-runs)."
    ),
    force_seed: bool = typer.Option(
        False, "--force-seed", help="Re-seed even if the collection already exists with points. Default skips if a non-empty collection is detected."
    ),
) -> None:
    """Interactive first-run wizard: clones/locates the repo, collects keys,
    brings up the stack, pre-warms, seeds, and verifies. ~5 min on warm
    Docker cache."""
    console.print("[bold]financebench setup[/bold] — one-time wizard.\n")

    repo_path = _resolve_repo_dir(repo_dir)
    console.print(f"[dim]Using repo at:[/dim] {repo_path}")

    env_path = repo_path / ".env"
    _wizard_env_file(env_path)

    compose_file = "docker-compose.yml" if full else "compose.minimal.yml"
    _ensure_compose_file_exists(repo_path, compose_file)

    _bring_up_stack(repo_path, compose_file)
    _wait_for_health()
    _prewarm()

    if not skip_seed:
        _seed_sample_corpus(repo_path, compose_file, force=force_seed)

    # Verify everything is actually wired up before declaring setup done.
    # Pre-0.1.1 the wizard happily reported success even when the seed had
    # silently failed or peft was missing — users only discovered the broken
    # state when chat queries started returning "An error occurred...".
    verified = _verify_setup()

    console.print()
    if verified:
        render_success("Setup complete. Try: financebench chat")
    else:
        render_error(
            "Setup completed with WARNINGS — chat may not work correctly. "
            "See the lines above + `docker compose logs api` for details."
        )
    console.print(
        "[dim]Tip: set FB_PROFILE=admin (or any name) in different terminals "
        "to keep separate identities for the multi-party HITL demo.[/dim]"
    )


def _refresh_clone(repo_path: Path) -> None:
    """git pull --ff-only on the resolved repo so `pip install -U` upgraders
    pick up the latest Dockerfile / pyproject.toml / .env.example.

    0.1.2 placed this only in the DEFAULT_CLONE_PATH branch; users who cd'd
    into ~/.financebench/repo before running `financebench setup` took the
    cwd branch and silently skipped the pull, ending up rebuilding from
    stale source. Calling this from both branches fixes that. --ff-only is
    safe — if the user has local commits ahead or uncommitted changes, it
    fails harmlessly and the wizard continues."""
    if not (repo_path / ".git").exists() or not shutil.which("git"):
        return
    try:
        rc = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if rc.returncode == 0:
            msg = rc.stdout.strip().split("\n")[-1] if rc.stdout.strip() else "(up to date)"
            render_info(f"git pull: {msg}")
        else:
            render_info(f"git pull skipped: {(rc.stderr or rc.stdout).strip()[:100]}")
    except Exception:  # noqa: BLE001
        pass


def _resolve_repo_dir(repo_dir: str | None) -> Path:
    """Find or create the repo checkout we'll operate on.

    Precedence: explicit --repo-dir > current dir if it looks like project
    root > ~/.financebench/repo (clone if missing).
    """
    if repo_dir:
        p = Path(repo_dir).expanduser().resolve()
        if not (p / "pyproject.toml").exists() or not (p / "src" / "api" / "main.py").exists():
            render_error(f"--repo-dir {p} doesn't look like a financebench-rag-agent checkout.")
            raise typer.Exit(1)
        return p

    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() and (cwd / "src" / "api" / "main.py").exists():
        render_info(f"Detected project checkout in current directory: {cwd}")
        _refresh_clone(cwd)
        return cwd

    if DEFAULT_CLONE_PATH.exists() and (DEFAULT_CLONE_PATH / "src" / "api" / "main.py").exists():
        render_info(f"Using existing clone at {DEFAULT_CLONE_PATH}")
        _refresh_clone(DEFAULT_CLONE_PATH)
        return DEFAULT_CLONE_PATH

    if not shutil.which("git"):
        render_error("git is not installed. Install it via Xcode Command Line Tools or Homebrew, then re-run.")
        raise typer.Exit(1)

    render_info(f"Cloning {REPO_URL} → {DEFAULT_CLONE_PATH} ...")
    DEFAULT_CLONE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rc = subprocess.run(
        ["git", "clone", REPO_URL, str(DEFAULT_CLONE_PATH)],
        check=False,
    ).returncode
    if rc != 0:
        render_error(f"git clone failed (exit {rc}). Check your network + that the repo URL is reachable.")
        raise typer.Exit(1)
    return DEFAULT_CLONE_PATH


def _wizard_env_file(env_path: Path) -> None:
    """Prompt for API keys; write/update .env. Existing keys preserved
    unless the user enters a new value."""
    existing = _parse_env_file(env_path) if env_path.exists() else {}

    if existing:
        render_info(f".env already exists at {env_path}. Press Enter at any prompt to keep the current value.")
        _migrate_stale_env_defaults(existing)
    else:
        render_info(f"No .env found. Creating {env_path}.")

    new_values: dict[str, str] = dict(existing)
    for key, description, required, signup_url, expected_prefixes in _API_KEY_PROMPTS:
        current = existing.get(key, "")
        masked = ("•" * 6 + current[-4:]) if current else "[not set]"
        # 0.1.4: print URL on its own line so the terminal can auto-link it.
        # is_password=True echoes pasted/typed chars as •, so the key stays
        # out of terminal scrollback (the M1 test cycle exposed plaintext
        # keys in shared logs — never again).
        console.print(f"  [bold]{description}[/bold]")
        console.print(f"  [dim]Get one at: {signup_url}[/dim]")
        try:
            entered = pt_prompt(
                f"  [current: {masked}] > ",
                is_password=True,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            render_info("Setup cancelled.")
            raise typer.Exit(1)
        if entered:
            if not any(entered.startswith(p) for p in expected_prefixes):
                prefix_hint = " or ".join(f"`{p}...`" for p in expected_prefixes)
                console.print(
                    f"  [yellow]Warning:[/] that doesn't look like a {key} value "
                    f"(expected to start with {prefix_hint}). Saved anyway — "
                    f"re-run setup to correct."
                )
            new_values[key] = entered
        elif required and not current:
            render_error(f"{key} is required to start the API.")
            raise typer.Exit(1)

    if not env_path.exists():
        # Bootstrap a minimal .env using .env.example as the template if available
        example = env_path.parent / ".env.example"
        if example.exists():
            shutil.copyfile(example, env_path)

    _write_env_file(env_path, new_values)
    render_success(f".env saved at {env_path} ({len(new_values)} keys set)")


# 0.1.2 — stale .env detection. Upgraders from 0.1.0 carry over defaults that
# changed in 0.1.1 / 0.1.2. Wizard preserves their existing values for safety
# (we don't silently rewrite their config), but warns + prompts to update.
_STALE_DEFAULTS = [
    # (env_key, stale_value, new_value, why_it_matters)
    (
        "EMBEDDING_MODEL", "text-embedding-3-large", "text-embedding-3-small",
        "3-large costs ~6x per token. 3-small is the new default in 0.1.1+.",
    ),
    (
        "EMBEDDING_DIMENSIONS", "3072", "1536",
        "Must match the model. Re-seed required if changed (delete + re-seed corpus).",
    ),
    (
        "USE_GROQ_FAST_PATH", "true", "false",
        "Groq free tier rate-limits surprise installers; OpenAI is the safe default.",
    ),
    (
        "RERANKER_ADAPTER_PATH", "data/models/reranker_ft_v1", "",
        "FT v1 regressed -5.34pp per Sprint 7.19 audit. Stock BGE is production.",
    ),
]


def _migrate_stale_env_defaults(existing: dict[str, str]) -> None:
    """Detect known-stale .env values from older releases and offer to update.

    We don't silently rewrite — embedding-dim changes need a re-seed, and
    flipping providers mid-config could break things. Just warn the user
    with the recommended values; they keep manual control."""
    flagged = []
    for key, stale, new, why in _STALE_DEFAULTS:
        current = (existing.get(key) or "").strip()
        if current.lower() == stale.lower():
            flagged.append((key, stale, new, why))

    if not flagged:
        return

    render_info(
        "Detected .env values from a previous release (these still work, "
        "but newer defaults are recommended):"
    )
    for key, stale, new, why in flagged:
        new_str = new if new else "(commented out / unset)"
        console.print(f"  [yellow]{key}={stale}[/yellow] → recommended [green]{new_str}[/green]")
        console.print(f"    [dim]{why}[/dim]")
    console.print(
        "\n[dim]To migrate, edit "
        f"{Path.home() / '.financebench' / 'repo' / '.env'} manually. "
        "If you change EMBEDDING_MODEL/DIMENSIONS you'll also need to drop "
        "+ re-seed the qdrant collection (the dim-mismatch boot check will "
        "tell you exactly which command).[/dim]\n"
    )


def _parse_env_file(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env_file(p: Path, values: dict[str, str]) -> None:
    """Rewrite .env, preserving any non-API-KEY lines from the original."""
    lines: list[str] = []
    seen_keys: set[str] = set()
    if p.exists():
        for line in p.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                lines.append(f"{key}={values[key]}")
                seen_keys.add(key)
            else:
                lines.append(line)
    for key, value in values.items():
        if key not in seen_keys:
            lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n")
    os.chmod(p, 0o600)


def _ensure_compose_file_exists(repo_path: Path, compose_file: str) -> None:
    if not (repo_path / compose_file).exists():
        render_error(f"{compose_file} not found at {repo_path}. The repo checkout may be incomplete or on an old branch.")
        raise typer.Exit(1)


def _bring_up_stack(repo_path: Path, compose_file: str) -> None:
    if not shutil.which("docker"):
        render_error("Docker is not installed or not on PATH. Install Docker Desktop and try again.")
        raise typer.Exit(1)

    # 0.1.5: capture the host repo's git sha and surface it to docker compose
    # via the GIT_SHA env var, which compose substitutes into the api service's
    # build.args block (see compose.minimal.yml + docker-compose.yml). The
    # Dockerfile's ARG GIT_SHA → ENV GIT_SHA wiring then carries it into the
    # container where _git_sha() in src/api/main.py reads it. Without this the
    # banner reports "sha unknown" because the container has no .git/ to call
    # `git rev-parse HEAD` against. Best-effort — failures fall back to "unknown".
    env = os.environ.copy()
    if shutil.which("git"):
        try:
            env["GIT_SHA"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:  # noqa: BLE001
            pass

    # 0.1.2: always pass --build so an image rebuild fires when the underlying
    # Dockerfile / pyproject.toml changes. Without this, `pip install -U` users
    # silently kept running the 0.1.0 api image even after the wheel was
    # upgraded — the M1 hit this with the peft import error.
    cmd = ["docker", "compose", "-f", compose_file, "up", "-d", "--build"]
    render_info(f"Bringing up the stack: {' '.join(shlex.quote(c) for c in cmd)}")
    render_info(
        "(First-time builds take 5-15 min on Apple Silicon — pip resolves "
        "torch + transformers + langgraph. Subsequent runs use cached layers.)"
    )
    rc = subprocess.run(cmd, cwd=repo_path, env=env, check=False).returncode
    if rc != 0:
        render_error(f"docker compose up --build failed (exit {rc}). Check Docker Desktop is running.")
        raise typer.Exit(1)
    render_success("Containers started.")


def _wait_for_health() -> None:
    import httpx

    started = time.monotonic()
    render_info(
        f"Waiting for /v1/health (timeout {_HEALTHCHECK_TIMEOUT_S}s). "
        f"First boot downloads the BGE reranker (~570MB) and is the slowest step."
    )
    with console.status("Checking /v1/health...", spinner="dots") as status_ui:
        while time.monotonic() - started < _HEALTHCHECK_TIMEOUT_S:
            try:
                r = httpx.get("http://localhost:8000/v1/health", timeout=5.0)
                if r.status_code == 200:
                    status_ui.stop()
                    render_success(f"API healthy after {int(time.monotonic() - started)}s.")
                    return
            except Exception:
                pass
            time.sleep(_HEALTHCHECK_INTERVAL_S)
    render_error(
        f"API didn't become healthy within {_HEALTHCHECK_TIMEOUT_S}s. "
        f"Check `docker compose -f compose.minimal.yml logs api`."
    )
    raise typer.Exit(1)


def _prewarm() -> None:
    import httpx

    try:
        with console.status("Pre-warming BGE reranker + sparse embedder...", spinner="dots"):
            httpx.get("http://localhost:8000/v1/warm", timeout=120.0)
    except Exception as exc:  # noqa: BLE001
        render_info(f"Pre-warm skipped ({exc}). First chat will load models lazily.")
        return
    render_success("Models loaded.")


def _seed_sample_corpus(repo_path: Path, compose_file: str, force: bool = False) -> None:
    """Seed the default Qdrant collection with the 8 sample PDFs by running
    scripts/seed_qdrant.py inside the api container. ~60s + ~$0.001 in
    embedding cost on text-embedding-3-small.

    Idempotent: when `force` is False and the collection already exists with
    points, the seed is skipped — re-runs of `financebench setup` after a
    successful first setup don't re-pay the embedding cost or re-write the
    same 179 chunks. Pass --force-seed to override.
    """
    import httpx

    if not force:
        collection_name = os.environ.get("QDRANT_COLLECTION", "financial_docs")
        try:
            r = httpx.get(
                f"http://localhost:6333/collections/{collection_name}",
                timeout=5.0,
            )
            if r.status_code == 200:
                points = (r.json().get("result") or {}).get("points_count", 0)
                if points and points > 0:
                    render_info(
                        f"Collection '{collection_name}' already has {points} points — skipping seed. "
                        f"Pass --force-seed to re-ingest."
                    )
                    return
        except Exception:  # noqa: BLE001
            pass  # qdrant unreachable / collection missing — fall through to seed

    cmd = [
        "docker", "compose", "-f", compose_file,
        "exec", "-T", "api",
        "python", "scripts/seed_qdrant.py", "--sample",
    ]
    render_info("Seeding sample corpus (8 PDFs, ~60s)...")
    rc = subprocess.run(cmd, cwd=repo_path, check=False).returncode
    if rc != 0:
        render_error(f"Seed failed (exit {rc}). You can retry later with `financebench setup --skip-seed` + manual seed.")
        return
    render_success("Corpus seeded.")


def _verify_setup() -> bool:
    """Post-setup verification — confirms the things that need to be true
    for a chat query to succeed. Returns True if everything looks healthy,
    False if any check failed (caller prints a generic warning).

    Three checks, in order of failure cost (cheapest first):
    1. /v1/health returns 200            — backend is up
    2. /v1/warm components all loaded    — reranker + embedder + LLMs didn't error
    3. qdrant collection exists w/ points — chat queries will find chunks

    Pre-0.1.1 the wizard didn't probe any of these; users discovered failures
    when chat returned the generic "An error occurred..." after a 60s wait.
    """
    import httpx

    ok = True

    # 1. Health
    try:
        r = httpx.get("http://localhost:8000/v1/health", timeout=5.0)
        if r.status_code == 200:
            render_success("Backend health: OK")
        else:
            render_error(f"Backend health: HTTP {r.status_code}")
            ok = False
    except Exception as exc:  # noqa: BLE001
        render_error(f"Backend health: unreachable ({type(exc).__name__})")
        return False  # nothing else will work

    # 1b. Version match — backend semver vs CLI __version__. Catches the
    # "wizard ran, docker cache reused a stale image, banner reports 0.1.0
    # while pip installed 0.1.3" failure mode that bit the M1 test cycle.
    try:
        from cli import __version__ as cli_version

        r = httpx.get("http://localhost:8000/version", timeout=5.0)
        if r.status_code == 200:
            backend_semver = (r.json() or {}).get("semver")
            if backend_semver and backend_semver != cli_version:
                render_error(
                    f"Version mismatch: CLI is {cli_version} but backend is {backend_semver}. "
                    f"Run `cd {Path.home() / '.financebench' / 'repo'} && git pull && "
                    f"docker compose -f compose.minimal.yml up -d --build` to rebuild."
                )
                ok = False
    except Exception:  # noqa: BLE001
        pass  # /version is best-effort; don't block setup on it

    # 2. /v1/warm component check
    try:
        r = httpx.get("http://localhost:8000/v1/warm", timeout=120.0)
        loaded = (r.json() or {}).get("loaded", {})
        errors = {k: v for k, v in loaded.items() if isinstance(v, str) and v.startswith("error:")}
        if errors:
            render_error("Components failed to load:")
            for k, v in errors.items():
                console.print(f"  [red]{k}:[/red] {v}")
            ok = False
        else:
            render_success(f"Components loaded: {', '.join(loaded.keys())}")
    except Exception as exc:  # noqa: BLE001
        render_error(f"/v1/warm probe failed ({type(exc).__name__})")
        ok = False

    # 3. Qdrant collection exists + has points
    collection_name = os.environ.get("QDRANT_COLLECTION", "financial_docs")
    try:
        r = httpx.get(
            f"http://localhost:6333/collections/{collection_name}",
            timeout=5.0,
        )
        if r.status_code == 200:
            points = (r.json().get("result") or {}).get("points_count", 0)
            if points and points > 0:
                render_success(f"Qdrant collection '{collection_name}': {points} chunks")
            else:
                render_error(
                    f"Qdrant collection '{collection_name}' exists but has 0 points. "
                    f"Re-run with --force-seed."
                )
                ok = False
        else:
            render_error(
                f"Qdrant collection '{collection_name}' not found (HTTP {r.status_code}). "
                f"Re-run `financebench setup` (the seed step likely failed)."
            )
            ok = False
    except Exception as exc:  # noqa: BLE001
        render_error(f"Qdrant probe failed ({type(exc).__name__})")
        ok = False

    return ok
