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
    """Show backend health, version, and current auth state."""
    creds = credentials.load()
    base_url = (creds or {}).get("base_url", DEFAULT_BASE_URL)

    client = APIClient(base_url=base_url, token=None)
    health_status = "?"
    api_version = "?"
    semver = "?"
    git_sha = "?"
    reachable = False

    try:
        health = client.get("/v1/health", auth_required=False)
        health_status = str(health.get("status", "?"))
        version = client.get("/version", auth_required=False)
        api_version = version.get("api_version", "?")
        semver = version.get("semver", "?")
        git_sha = version.get("git_sha", "?")
        reachable = True
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
    if creds:
        table.add_row("Logged in as", f"{creds.get('user_id', '?')}")
    else:
        table.add_row("Logged in as", "[dim](not logged in)[/dim]")
    console.print(table)

    if not reachable:
        raise typer.Exit(1)
