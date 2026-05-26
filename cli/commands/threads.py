"""financebench threads — list/show/delete prior conversations."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from cli.api_client import APIClient, APIError
from cli.render import render_error, render_info, render_success

console = Console()


def list_threads(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
) -> None:
    """List your conversation threads (newest first)."""
    client = APIClient()
    try:
        resp = client.get(f"/v1/threads?limit={limit}")
    except APIError as e:
        render_error(f"Could not list threads: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    threads = resp.get("threads") or []
    if not threads:
        render_info("No threads yet. Run `financebench chat` to start one.")
        return

    table = Table(
        title=f"Your threads ({len(threads)} of {resp.get('total', '?')})",
        header_style="bold cyan",
        title_style="bold",
    )
    table.add_column("Thread ID", style="cyan", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Checkpoints", justify="right", style="dim")
    table.add_column("Status")
    for t in threads:
        status = "[yellow]paused (HITL)[/yellow]" if t.get("is_interrupted") else ""
        table.add_row(
            t.get("thread_id", "?"),
            t.get("title") or "[dim](no title)[/dim]",
            str(t.get("checkpoint_count", "?")),
            status,
        )
    console.print(table)


def show(thread_id: str = typer.Argument(..., help="Thread id to inspect")) -> None:
    """Show the messages + interrupt state of a specific thread."""
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

    messages = resp.get("messages") or []
    console.print(f"[bold]Thread:[/bold] {resp.get('thread_id', '?')}")
    if resp.get("is_interrupted"):
        payload = resp.get("interrupt_payload") or {}
        console.print(f"[yellow]Status: paused (HITL)[/yellow]")
        if payload.get("reason"):
            console.print(f"[yellow]Reason: {payload['reason']}[/yellow]")

    if not messages:
        render_info("(no messages)")
        return

    for i, m in enumerate(messages, 1):
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
