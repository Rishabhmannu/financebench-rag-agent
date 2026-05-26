"""financebench approvals — Phase 3.5 multi-party HITL.

Approver inbox + decision commands. Authorized only for roles with
`can_approve_for` entries (admin = any role, clevel = finance/hr/analyst,
others = none).

Usage:
  financebench approvals list                 # inbox
  financebench approvals show <thread_id>     # full review payload
  financebench approvals approve <id> [--reason "..."]
  financebench approvals reject  <id> [--reason "..."]
  financebench approvals watch [--interval 3] # long-running inbox
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from cli.api_client import APIClient, APIError
from cli.render import render_error, render_info, render_success

console = Console()


def list_approvals(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
) -> None:
    """List pending HITL interrupts your role is authorized to approve."""
    client = APIClient()
    try:
        resp = client.get("/v1/approvals")
    except APIError as e:
        render_error(f"Could not list approvals: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    pending = resp.get("approvals") or []
    if not pending:
        render_info("Inbox empty. No pending approvals for your role.")
        return

    table = Table(
        title=f"Pending approvals ({len(pending)})",
        header_style="bold yellow",
        title_style="bold",
    )
    table.add_column("Thread ID", style="cyan", overflow="fold")
    table.add_column("Requester", style="dim")
    table.add_column("Role", style="dim")
    table.add_column("Query", overflow="fold")
    table.add_column("Amount", justify="right", style="yellow")
    table.add_column("Threshold", justify="right", style="dim")
    for a in pending[:limit]:
        amount = a.get("max_amount")
        threshold = a.get("threshold")
        amt_str = f"${amount:,.0f}" if amount is not None else "—"
        thr_str = f"${threshold:,.0f}" if threshold is not None else "—"
        table.add_row(
            a.get("thread_id", "?"),
            a.get("requester_user_id", "?"),
            a.get("requester_role", "?"),
            a.get("query") or "[dim](no query)[/dim]",
            amt_str,
            thr_str,
        )
    console.print(table)
    console.print("[dim]Tip: `financebench approvals show <thread_id>` to see the draft answer.[/dim]")


def show(thread_id: str = typer.Argument(..., help="Thread id of the pending approval")) -> None:
    """Show the full review payload for a pending approval: query, draft answer,
    requester, amount, threshold."""
    client = APIClient()
    try:
        resp = client.get(f"/v1/approvals/{thread_id}")
    except APIError as e:
        if e.status_code == 404:
            render_error(f"Thread {thread_id} not found.")
        elif e.status_code == 403:
            render_error(f"Your role cannot approve this thread: {e.message}")
        elif e.status_code == 409:
            render_error(f"Thread {thread_id} is not awaiting approval.")
        else:
            render_error(f"Could not load approval: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    amount = resp.get("max_amount")
    threshold = resp.get("threshold")
    amt_str = f"${amount:,.0f}" if amount is not None else "—"
    thr_str = f"${threshold:,.0f}" if threshold is not None else "—"
    header = (
        f"[bold]Thread:[/bold] {resp.get('thread_id', '?')}\n"
        f"[bold]Requester:[/bold] {resp.get('requester_user_id', '?')} "
        f"([dim]role={resp.get('requester_role', '?')}[/dim])\n"
        f"[bold]Reason:[/bold] {resp.get('reason') or '(none)'}\n"
        f"[bold]Amount referenced:[/bold] [yellow]{amt_str}[/yellow]   "
        f"[bold]Role threshold:[/bold] [dim]{thr_str}[/dim]"
    )
    console.print(Panel(header, title="HITL approval review", border_style="yellow", title_align="left"))

    query = (resp.get("query") or "").strip()
    if query:
        console.print()
        console.print("[bold cyan]ORIGINAL QUERY:[/bold cyan]")
        console.print(query)

    draft = (resp.get("draft_answer") or "").strip()
    if draft:
        console.print()
        console.print("[bold green]DRAFT ANSWER (suppressed from requester):[/bold green]")
        console.print(Markdown(draft))
    else:
        console.print("\n[dim](no draft answer in state)[/dim]")

    console.print()
    console.print(
        "[dim]To decide: `financebench approvals approve "
        f"{resp.get('thread_id', '<id>')}` or `... reject <id>`.[/dim]"
    )


def approve(
    thread_id: str = typer.Argument(..., help="Thread id to approve"),
    reason: str = typer.Option("", "--reason", "-r", help="Optional reason (logged)"),
) -> None:
    """Approve a pending HITL request. Resumes the graph and releases the answer
    to the requester. Caller's role must can_approve_for the requester's role."""
    client = APIClient()
    try:
        with console.status("Approving and resuming graph...", spinner="dots"):
            resp = client.post("/v1/hitl/approve", {"thread_id": thread_id})
    except APIError as e:
        if e.status_code == 403:
            render_error(f"Authorization failed: {e.message}")
        elif e.status_code == 404:
            render_error(f"Thread {thread_id} not found.")
        else:
            render_error(f"Approve failed: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    render_success(
        f"Approved by {resp.get('approver_user_id', '?')} ({resp.get('approver_role', '?')}). "
        "Requester will see the answer once they poll /v1/chat/result or refresh."
    )
    response_text = (resp.get("response") or "").strip()
    if response_text:
        console.print()
        console.print("[bold green]Released answer:[/bold green]")
        console.print(Markdown(response_text))


def reject(
    thread_id: str = typer.Argument(..., help="Thread id to reject"),
    reason: str = typer.Option("", "--reason", "-r", help="Optional reason (logged)"),
) -> None:
    """Reject a pending HITL request. Requester will see a rejection message
    instead of the answer."""
    client = APIClient()
    try:
        with console.status("Rejecting...", spinner="dots"):
            resp = client.post("/v1/hitl/reject", {"thread_id": thread_id})
    except APIError as e:
        if e.status_code == 403:
            render_error(f"Authorization failed: {e.message}")
        elif e.status_code == 404:
            render_error(f"Thread {thread_id} not found.")
        else:
            render_error(f"Reject failed: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    render_info(
        f"Rejected by {resp.get('approver_user_id', '?')} ({resp.get('approver_role', '?')})."
    )
    response_text = (resp.get("response") or "").strip()
    if response_text:
        console.print()
        console.print("[dim]Rejection message sent to requester:[/dim]")
        console.print(Markdown(response_text))


def review() -> None:
    """Interactive approver inbox — arrow keys to select, Enter to review,
    buttons to Approve / Reject / Back. No copy-pasting thread IDs. After each
    decision, the list refreshes and you stay in the loop until Esc."""
    from cli.interactive import interactive_approvals_loop
    interactive_approvals_loop()


def watch() -> None:
    """Alias for `review` — kept for backwards-compatibility with Phase 3.5
    docs. The behavior is identical (interactive arrow-key TUI, NOT the old
    blocking poll-loop that swallowed the shell)."""
    review()


app = typer.Typer(
    name="approvals",
    help="Approver inbox for multi-party HITL (Phase 3.5).",
    no_args_is_help=True,
)
app.command("list")(list_approvals)
app.command("show")(show)
app.command("approve")(approve)
app.command("reject")(reject)
app.command("review")(review)
app.command("watch")(watch)
