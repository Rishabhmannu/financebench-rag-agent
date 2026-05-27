"""financebench logs — list / show / locate captured session logs.

These complement scripts/fb-record (a script(1) wrapper that captures the
full terminal session — REPL, TUIs, ANSI, the lot — into a file under
~/.financebench/cli_sessions/).

Typical workflow when something misbehaves and you want to share with a
debugger (human or LLM):

    fb-record financebench chat            # do your testing
    financebench logs show latest --clean  # print clean text
    financebench logs path latest          # copy to your docs/ or paste
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import typer

from cli.render import console, render_error, render_info

LOGS_DIR = Path.home() / ".financebench" / "cli_sessions"

# Match SGR (color), most CSI escape sequences, and the bare \r that script(1)
# scatters through the file as cursor positioning. Good enough for plain-text
# reading; not a full ANSI parser.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>()][0-9A-Za-z]?")


def _resolve_session_path(session_id: str) -> Path | None:
    """Resolve 'latest' or '<id>' to a Path; None if not found."""
    if not LOGS_DIR.exists():
        return None
    if session_id == "latest":
        files = sorted(LOGS_DIR.glob("session_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None
    # Allow either bare id (no .log) or full filename
    candidate = LOGS_DIR / (session_id if session_id.endswith(".log") else f"{session_id}.log")
    return candidate if candidate.exists() else None


def list_sessions(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200, help="Max sessions to list"),
) -> None:
    """List recently captured terminal sessions, newest first."""
    from rich.table import Table

    if not LOGS_DIR.exists():
        render_info("No sessions yet. Capture one with:  fb-record financebench chat")
        return
    files = sorted(
        LOGS_DIR.glob("session_*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    if not files:
        render_info("No sessions yet. Capture one with:  fb-record financebench chat")
        return

    table = Table(title=f"Recent sessions ({len(files)})", title_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True, overflow="fold")
    table.add_column("Captured at")
    table.add_column("Size", justify="right", style="dim")
    table.add_column("Command (sanitized)", overflow="fold")
    for p in files:
        size = p.stat().st_size
        size_str = f"{size:,}b" if size < 10_000 else f"{size // 1024:,}KB"
        ts = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        # Filename shape: session_<YYYYMMDD-HHMMSS>_<sanitized_cmd>.log
        stem_parts = p.stem.split("_", 2)
        cmd = stem_parts[2] if len(stem_parts) > 2 else "?"
        table.add_row(p.stem, ts, size_str, cmd[:60])
    console.print(table)
    console.print(
        "[dim]Show one with:  financebench logs show <id|latest> [--clean][/dim]"
    )


def show(
    session_id: str = typer.Argument(..., help="Session id (from `logs list`) or 'latest'"),
    clean: bool = typer.Option(
        False,
        "--clean",
        help="Strip ANSI escape codes for plain-text reading (recommended when copying into a bug report)",
    ),
) -> None:
    """Print a captured session to stdout."""
    path = _resolve_session_path(session_id)
    if path is None:
        render_error(f"Session not found: {session_id}. Try `financebench logs list`.")
        raise typer.Exit(1)
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        render_error(f"Could not read {path}: {e}")
        raise typer.Exit(1)
    if clean:
        text = _ANSI_RE.sub("", text)
    # Use typer.echo (not console.print) so it goes through plain stdout and
    # pipes cleanly into `pbcopy` / `tee` / `grep` etc.
    typer.echo(text)


def path_cmd(
    session_id: str = typer.Argument("latest", help="Session id or 'latest' (default)"),
) -> None:
    """Print the absolute path to a session log (for `cp`, `pbcopy <`, opening in an editor, ...)."""
    path = _resolve_session_path(session_id)
    if path is None:
        render_error(f"Session not found: {session_id}. Try `financebench logs list`.")
        raise typer.Exit(1)
    typer.echo(str(path))


app = typer.Typer(
    name="logs",
    help="List/show/locate terminal-session logs captured via `fb-record`.",
    no_args_is_help=True,
)
app.command("list")(list_sessions)
app.command("show")(show)
app.command("path")(path_cmd)
