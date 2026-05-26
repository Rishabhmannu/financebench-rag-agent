"""Interactive arrow-key pickers shared between standalone subcommands and
in-REPL slash commands.

prompt_toolkit's default `radiolist_dialog` has wrong-feeling UX for our case:
Enter on the list TOGGLES the selection (it's RadioList semantics) rather than
submitting it, so users have to Tab over to the OK button then press Enter
again. We replace it with a custom Application where Enter on the highlighted
row submits directly. Esc / q / Ctrl+C all cancel (Esc alone is laggy on
macOS terminals due to Alt-key disambiguation; we don't rely on it solo).
"""

from __future__ import annotations

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.shortcuts import input_dialog, message_dialog
from prompt_toolkit.widgets import Frame, Label, RadioList
from rich.markdown import Markdown
from rich.panel import Panel

from cli.api_client import APIClient, APIError
from cli.render import console, render_error, render_info, render_success


def select_one(title: str, text: str, choices: list[tuple]):
    """Arrow-key single-select dialog. Enter submits the highlighted row; Esc /
    q / Ctrl+C cancels (returns None). `choices` is a list of (value, label)
    tuples; the returned value is whatever `value` was for the chosen row.

    Replaces prompt_toolkit's radiolist_dialog whose Enter-toggles-not-submits
    UX confused real-TTY testing (Phase 3.6 user feedback).
    """
    if not choices:
        return None

    radio = RadioList(values=choices)
    kb = KeyBindings()

    @kb.add("enter", eager=True)
    def _(event) -> None:
        idx = getattr(radio, "_selected_index", 0)
        if 0 <= idx < len(radio.values):
            event.app.exit(result=radio.values[idx][0])
        else:
            event.app.exit(result=None)

    @kb.add("escape", eager=True)
    @kb.add("c-c", eager=True)
    @kb.add("q", eager=True)
    def _(event) -> None:
        event.app.exit(result=None)

    layout = Layout(
        HSplit([
            Frame(
                body=HSplit([
                    Label(text=text),
                    Window(height=1),
                    radio,
                ]),
                title=title,
            ),
        ])
    )

    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        mouse_support=True,
    )
    return app.run()


def confirm_action(title: str, text: str, choices: list[tuple]):
    """Action-button dialog using the same Enter-submits-highlight UX as
    select_one. Used for Approve/Reject/Back after picking an approval."""
    return select_one(title, text, choices)


def _fetch_pending() -> list[dict] | None:
    """Fetch /v1/approvals. Returns the list or None on error (already rendered)."""
    client = APIClient()
    try:
        resp = client.get("/v1/approvals")
    except APIError as e:
        render_error(f"Could not load approvals: {e.message}")
        return None
    finally:
        client.close()
    return resp.get("approvals") or []


def _label_for_approval(a: dict) -> str:
    amount = a.get("max_amount")
    amt_str = f"${amount:>15,.0f}" if amount is not None else " " * 16
    req = (a.get("requester_user_id") or "?")[:10]
    role = (a.get("requester_role") or "?")[:8]
    query = (a.get("query") or "(no query)")[:60]
    return f"{req:10} | {role:8} | {amt_str} | {query}"


def _show_detail(thread_id: str) -> dict | None:
    """Fetch + render the full approval payload. Returns the payload dict."""
    client = APIClient()
    try:
        detail = client.get(f"/v1/approvals/{thread_id}")
    except APIError as e:
        message_dialog(title="Error", text=f"Could not load: {e.message}").run()
        return None
    finally:
        client.close()

    amount = detail.get("max_amount")
    threshold = detail.get("threshold")
    amt_str = f"${amount:,.0f}" if amount is not None else "—"
    thr_str = f"${threshold:,.0f}" if threshold is not None else "—"
    header = (
        f"[bold]Thread:[/bold] {detail.get('thread_id', '?')}\n"
        f"[bold]Requester:[/bold] {detail.get('requester_user_id', '?')} "
        f"([dim]role={detail.get('requester_role', '?')}[/dim])\n"
        f"[bold]Reason:[/bold] {detail.get('reason') or '(none)'}\n"
        f"[bold]Amount referenced:[/bold] [yellow]{amt_str}[/yellow]   "
        f"[bold]Role threshold:[/bold] [dim]{thr_str}[/dim]"
    )
    console.print()
    console.print(Panel(header, title="HITL approval review", border_style="yellow", title_align="left"))

    query = (detail.get("query") or "").strip()
    if query:
        console.print("\n[bold cyan]ORIGINAL QUERY:[/bold cyan]")
        console.print(query)

    draft = (detail.get("draft_answer") or "").strip()
    if draft:
        console.print("\n[bold green]DRAFT ANSWER (suppressed from requester):[/bold green]")
        console.print(Markdown(draft))
    return detail


def _act_on(thread_id: str) -> bool:
    """Show detail + Approve/Reject/Back picker. Returns True if the item was
    handled (approved or rejected) and should be removed from the inbox; False
    if the user chose Back (still pending)."""
    detail = _show_detail(thread_id)
    if detail is None:
        return False

    action = confirm_action(
        title=f"Decide on thread {thread_id[:8]}...",
        text="Arrow keys + Enter. Approve releases the answer to the requester. Reject sends a refusal.",
        choices=[
            ("approve", "Approve  (release the draft answer)"),
            ("reject", "Reject   (send refusal to requester)"),
            ("back", "Back     (leave pending, return to inbox)"),
        ],
    )

    if action == "back" or action is None:
        return False

    reason = ""
    if action == "reject":
        reason = (
            input_dialog(
                title="Reject reason (optional)",
                text="Add a brief reason that will be logged with the rejection:",
            ).run()
            or ""
        )

    client = APIClient()
    try:
        with console.status(f"{action.capitalize()}ing and resuming graph...", spinner="dots"):
            resp = client.post(f"/v1/hitl/{action}", {"thread_id": thread_id})
    except APIError as e:
        message_dialog(title=f"{action.capitalize()} failed", text=e.message).run()
        return False
    finally:
        client.close()

    console.print()
    if action == "approve":
        render_success(
            f"Approved by {resp.get('approver_user_id', '?')} "
            f"({resp.get('approver_role', '?')}). Released answer:"
        )
    else:
        render_info(
            f"Rejected by {resp.get('approver_user_id', '?')} "
            f"({resp.get('approver_role', '?')}). "
            + (f"Reason: {reason}" if reason else "")
        )
    response_text = (resp.get("response") or "").strip()
    if response_text:
        console.print(Markdown(response_text))
    return True


def interactive_approvals_loop() -> None:
    """The reusable interactive approver inbox. Loop until user picks Esc/q on
    the main list. Used by `financebench approvals review/watch` AND by the
    `/approvals` slash command in the chat REPL."""
    while True:
        pending = _fetch_pending()
        if pending is None:
            return
        if not pending:
            choice = confirm_action(
                title="Approvals inbox",
                text="No pending approvals for your role. Arrow keys + Enter.",
                choices=[("refresh", "Refresh"), ("quit", "Quit")],
            )
            if choice in (None, "quit"):
                return
            continue

        choices = [(a["thread_id"], _label_for_approval(a)) for a in pending]
        selected = select_one(
            title=f"Pending approvals ({len(pending)})",
            text="Arrow keys to navigate, Enter to review, Esc / q to exit.",
            choices=choices,
        )
        if selected is None:
            return

        _act_on(selected)
        # Loop refetches; the just-acted-on item drops off the list naturally
        # because it's no longer in `pending` state.


def _fetch_threads(limit: int = 30) -> list[dict] | None:
    """Fetch the caller's own threads (used by /threads picker in REPL)."""
    client = APIClient()
    try:
        resp = client.get(f"/v1/threads?limit={limit}")
    except APIError as e:
        render_error(f"Could not load threads: {e.message}")
        return None
    finally:
        client.close()
    return resp.get("threads") or []


def interactive_thread_picker(current_thread_id: str | None = None) -> str | None:
    """Show the caller's threads as arrow-key list. Returns selected thread_id
    or None if cancelled. Used by the chat REPL's `/threads` slash command."""
    threads = _fetch_threads()
    if threads is None:
        return None
    if not threads:
        message_dialog(
            title="No threads",
            text="No prior threads. Ask a question to start one.",
        ).run()
        return None

    choices: list[tuple[str, str]] = []
    for t in threads:
        tid = t.get("thread_id", "?")
        title = (t.get("title") or "(no title)")[:70]
        marker = "*" if current_thread_id and tid == current_thread_id else " "
        status = " [PAUSED]" if t.get("is_interrupted") else ""
        choices.append((tid, f"{marker} {tid[:8]}... | {title}{status}"))

    return select_one(
        title=f"Your threads ({len(threads)})",
        text="Arrow keys + Enter to switch. Current = marked with *. Esc / q to keep current.",
        choices=choices,
    )
