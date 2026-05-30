"""financebench logout — clear stored JWT."""

from __future__ import annotations

from cli import credentials
from cli.render import console, render_info, render_success


def logout() -> None:
    """Clear stored JWT from ~/.financebench/credentials.json."""
    if credentials.clear():
        render_success("Logged out (credentials removed).")
        console.print("[dim]Run `financebench login -u <role>` when ready (analyst | finance | hr | clevel | admin).[/dim]")
    else:
        render_info("Not logged in; nothing to clear.")
