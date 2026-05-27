"""financebench threads — list/show/delete prior conversations."""

from __future__ import annotations

from datetime import datetime, timezone

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from cli.api_client import APIClient, APIError
from cli.render import render_error, render_info, render_success

console = Console()


def _format_relative(iso_ts: str | None) -> str:
    """Render an ISO timestamp as a relative age string (e.g. '2h ago', 'just now')."""
    if not iso_ts:
        return "[dim]—[/dim]"
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta_s = (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return iso_ts[:19] if iso_ts else "[dim]—[/dim]"
    if delta_s < 60:
        return f"{int(delta_s)}s ago"
    if delta_s < 3600:
        return f"{int(delta_s // 60)}m ago"
    if delta_s < 86400:
        return f"{int(delta_s // 3600)}h ago"
    if delta_s < 86400 * 30:
        return f"{int(delta_s // 86400)}d ago"
    return dt.strftime("%Y-%m-%d")


def list_threads(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
    all_users: bool = typer.Option(
        False, "--all", "-a",
        help="Admin only: enumerate every user's threads. Non-admin roles get their own threads regardless.",
    ),
) -> None:
    """List conversation threads (newest first).

    Track 2 enrichment: rows now include the owner identity and a relative
    last-activity timestamp. Admin can pass `--all` to enumerate cross-user
    threads (Bug A fix).
    """
    client = APIClient()
    try:
        path = "/v1/threads"
        params = [f"limit={limit}"]
        if all_users:
            params.append("all=true")
        resp = client.get(path + "?" + "&".join(params))
    except APIError as e:
        render_error(f"Could not list threads: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    threads = resp.get("threads") or []
    if not threads:
        render_info("No threads yet. Run `financebench chat` to start one.")
        return

    scope = resp.get("scope", "self")
    viewer_role = resp.get("viewer_role", "?")
    scope_label = "all users" if scope == "all" else "your own"
    table = Table(
        title=f"Threads ({len(threads)} of {resp.get('total', '?')}, scope={scope_label}, viewer={viewer_role})",
        header_style="bold cyan",
        title_style="bold",
    )
    table.add_column("Thread ID", style="cyan", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Owner", overflow="fold")
    table.add_column("Last activity")
    table.add_column("Ckpts", justify="right", style="dim")
    table.add_column("Status")
    for t in threads:
        status = "[yellow]paused (HITL)[/yellow]" if t.get("is_interrupted") else ""
        owner = t.get("owner") or {}
        owner_str = (
            f"{owner.get('user_id', '?')} "
            f"[dim]({owner.get('role', '?')} · {owner.get('department') or '—'})[/dim]"
        )
        table.add_row(
            t.get("thread_id", "?"),
            t.get("title") or "[dim](no title)[/dim]",
            owner_str,
            _format_relative(t.get("last_activity_at")),
            str(t.get("checkpoint_count", "?")),
            status,
        )
    console.print(table)
    if scope == "self" and viewer_role == "admin":
        console.print("[dim]Tip: pass --all to see threads from every user.[/dim]")


def show(thread_id: str = typer.Argument(..., help="Thread id to inspect")) -> None:
    """Show the messages + owner + HITL audit + interrupt state of a thread.

    Track 2: response now carries an owner block, turn_count, last_activity_at,
    and (if the thread ever hit hitl_gate) an audit block with submitted_at /
    decided_at / decided_by / decision / reason.
    """
    client = APIClient()
    try:
        resp = client.get(f"/v1/threads/{thread_id}")
    except APIError as e:
        if e.status_code == 404:
            render_error(f"Thread {thread_id} not found.")
        elif e.status_code == 403:
            render_error(f"Thread {thread_id} belongs to a different user.")
        else:
            render_error(f"Could not load thread: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    # Owner / activity summary panel
    owner = resp.get("owner") or {}
    owner_user = owner.get("user_id", "?")
    owner_role = owner.get("role", "?")
    owner_name = owner.get("name", "")
    owner_dept = owner.get("department", "") or "—"
    turn_count = resp.get("turn_count", 0)
    last_activity = _format_relative(resp.get("last_activity_at"))
    paused = " [yellow](paused / HITL)[/yellow]" if resp.get("is_interrupted") else ""
    summary = (
        f"[bold]Thread:[/bold] {resp.get('thread_id', '?')}{paused}\n"
        f"[bold]Owner:[/bold] {owner_user} ([cyan]{owner_role}[/cyan] · {owner_dept})"
        + (f"  [dim]{owner_name}[/dim]" if owner_name else "")
        + f"\n[bold]Turns:[/bold] {turn_count}"
        + f"   [bold]Last activity:[/bold] {last_activity}"
    )
    console.print(Panel(summary, title="Thread context", expand=False))

    # HITL audit panel — present iff hitl_gate ever fired on this thread
    audit = resp.get("audit")
    if audit:
        decision = (audit.get("decision") or "?").upper()
        decision_color = "green" if decision == "APPROVED" else ("red" if decision == "REJECTED" else "yellow")
        audit_text = (
            f"[bold]Submitted at:[/bold] {audit.get('hitl_submitted_at') or '—'}\n"
            f"[bold]Decided at:[/bold] {audit.get('decided_at') or '—'}\n"
            f"[bold]Decision:[/bold] [{decision_color}]{decision}[/{decision_color}]"
            f" by {audit.get('decided_by') or '—'} ([cyan]{audit.get('decided_by_role') or '?'}[/cyan])\n"
            f"[bold]Reason:[/bold] {audit.get('reason') or '[dim](no reason)[/dim]'}"
        )
        console.print(Panel(audit_text, title="HITL audit", border_style="yellow", expand=False))

    if resp.get("is_interrupted"):
        payload = resp.get("interrupt_payload") or {}
        if payload.get("reason"):
            console.print(f"[yellow]Pending reason:[/yellow] {payload['reason']}")

    messages = resp.get("messages") or []
    if not messages:
        render_info("(no messages yet)")
        return

    for m in messages:
        role = m.get("role", "?")
        color = "cyan" if role == "user" else "green"
        console.print(f"\n[{color}]{role.upper()}[/{color}]")
        content = m.get("content") or ""
        console.print(Markdown(content) if role == "assistant" else content)
        if m.get("sources"):
            console.print(f"[dim]  ({len(m['sources'])} source(s), confidence={m.get('confidence')})[/dim]")


def delete(
    thread_id: str = typer.Argument(..., help="Thread id to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a thread (irreversible)."""
    if not yes:
        try:
            confirm = typer.confirm(f"Delete thread {thread_id}?", default=False)
        except (EOFError, KeyboardInterrupt):
            render_info("Cancelled.")
            return
        if not confirm:
            render_info("Cancelled.")
            return

    client = APIClient()
    try:
        client.delete(f"/v1/threads/{thread_id}")
    except APIError as e:
        if e.status_code == 404:
            render_error(f"Thread {thread_id} not found.")
        elif e.status_code == 403:
            render_error(f"Thread {thread_id} belongs to a different user.")
        else:
            render_error(f"Delete failed: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    render_success(f"Deleted thread {thread_id}.")


app = typer.Typer(name="threads", help="List, show, or delete your conversation threads.", no_args_is_help=True)
app.command("list")(list_threads)
app.command("show")(show)
app.command("delete")(delete)
