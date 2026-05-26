"""financebench logout — clear stored JWT."""

from __future__ import annotations

from cli import credentials
from cli.render import render_info, render_success


def logout() -> None:
    """Clear stored JWT from ~/.financebench/credentials.json."""
    if credentials.clear():
        render_success("Logged out (credentials removed).")
    else:
        render_info("Not logged in; nothing to clear.")
