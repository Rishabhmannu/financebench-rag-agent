"""financebench status — backend health + version + auth state."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from cli import credentials
from cli.api_client import DEFAULT_BASE_URL, APIClient, APIError
from cli.render import render_error

console = Console()


def status() -> None:
    """Show backend health, version, auth state, and (when logged in) thread count."""
    creds = credentials.load()
    base_url = (creds or {}).get("base_url", DEFAULT_BASE_URL)

    client = APIClient(base_url=base_url, token=(creds or {}).get("token"))
    health_status = "?"
    api_version = "?"
    semver = "?"
    git_sha = "?"
    warm_state: dict = {}
    reachable = False
    n_threads: str | int = "?"
    me_role: str = "?"

    try:
        health = client.get("/v1/health", auth_required=False)
        health_status = str(health.get("status", "?"))
        version = client.get("/version", auth_required=False)
        api_version = version.get("api_version", "?")
        semver = version.get("semver", "?")
        git_sha = version.get("git_sha", "?")
        reachable = True
        try:
            warm = client.get("/v1/warm", auth_required=False)
            warm_state = warm.get("loaded", {})
        except APIError:
            pass
        if creds:
            try:
                me = client.get("/v1/auth/me")
                me_role = me.get("role", "?")
                threads_resp = client.get("/v1/threads?limit=1")
                n_threads = threads_resp.get("total", "?")
            except APIError:
                pass
    except APIError as e:
        render_error(f"Backend at {base_url} unreachable: {e.message}")
    except Exception as e:
        render_error(f"Backend at {base_url} unreachable: {e}")
    finally:
        client.close()

    table = Table(show_header=False, show_lines=False, box=None, pad_edge=False)
    table.add_column("Field", style="dim")
    table.add_column("Value")
    table.add_row("Backend URL", base_url)
    table.add_row("Backend reachable", "yes" if reachable else "[red]no[/red]")
    if reachable:
        table.add_row("Health", health_status)
        table.add_row("API version", api_version)
        table.add_row("Backend semver", semver)
        table.add_row("git sha", git_sha)
        if warm_state:
            warm_str = ", ".join(f"{k}={v}" for k, v in warm_state.items() if "error" not in str(v))
            table.add_row("Models loaded", warm_str or "[dim](none)[/dim]")
    if creds:
        table.add_row("Logged in as", f"{creds.get('user_id', '?')} (role={me_role})")
        table.add_row("Your threads", str(n_threads))
    else:
        table.add_row("Logged in as", "[dim](not logged in)[/dim]")
    console.print(table)

    if not reachable:
        raise typer.Exit(1)
