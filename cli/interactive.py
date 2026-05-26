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

from datetime import datetime, timezone

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


def _format_age(iso_ts: str | None) -> str:
    """Render an ISO-8601 timestamp as 'Xs/m/h/d ago' relative to now."""
    if not iso_ts:
        return "?"
    try:
        if iso_ts.endswith("Z"):
            iso_ts = iso_ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta_s = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta_s < 0:
            return "0s ago"
        if delta_s < 60:
            return f"{int(delta_s)}s ago"
        if delta_s < 3600:
            return f"{int(delta_s / 60)}m ago"
        if delta_s < 86400:
            h = int(delta_s / 3600)
            m = int((delta_s % 3600) / 60)
            return f"{h}h {m}m ago"
        d = int(delta_s / 86400)
        return f"{d}d ago"
    except Exception:  # noqa: BLE001
        return "?"


def _age_color(iso_ts: str | None) -> str:
    """Color hint for an age timestamp. yellow >5min, red >15min."""
    if not iso_ts:
        return "dim"
    try:
        if iso_ts.endswith("Z"):
            iso_ts = iso_ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta_s = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta_s > 900:
            return "red"
        if delta_s > 300:
            return "yellow"
        return "green"
    except Exception:  # noqa: BLE001
        return "dim"


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


_APPROVALS_HEADER = (
    f"{'NAME':<16} {'ROLE':<8} {'DEPT':<10} {'AGE':>10}   {'AMOUNT':>16}   QUERY"
)


def _label_for_approval(a: dict) -> str:
    amount = a.get("max_amount")
    amt_str = f"${amount:>15,.0f}" if amount is not None else " " * 16
    name = (a.get("requester_name") or a.get("requester_user_id") or "?")[:16]
    role = (a.get("requester_role") or "?")[:8]
    dept = (a.get("requester_department") or "")[:10]
    age = _format_age(a.get("submitted_at"))
    query = (a.get("query") or "(no query)")[:50]
    return f"{name:<16} {role:<8} {dept:<10} {age:>10}   {amt_str}   {query}"


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

    requester_name = detail.get("requester_name") or detail.get("requester_user_id") or "?"
    requester_dept = detail.get("requester_department") or ""
    requester_line = (
        f"[bold]Requester:[/bold] {requester_name} "
        f"([dim]id={detail.get('requester_user_id', '?')} | "
        f"role={detail.get('requester_role', '?')}"
        + (f" | dept={requester_dept}" if requester_dept else "")
        + "[/dim])"
    )

    submitted_at = detail.get("submitted_at")
    age = _format_age(submitted_at)
    age_color = _age_color(submitted_at)
    age_line = (
        f"[bold]Submitted:[/bold] [{age_color}]{age}[/{age_color}]"
        + (f"  [dim]({submitted_at} UTC)[/dim]" if submitted_at else "")
    )

    confidence = detail.get("confidence")
    if confidence is not None:
        conf_color = "green" if confidence >= 0.7 else "yellow" if confidence >= 0.4 else "red"
        conf_line = f"[bold]Draft grounding:[/bold] [{conf_color}]{confidence:.2f}[/{conf_color}]"
    else:
        conf_line = "[bold]Draft grounding:[/bold] [dim](not scored)[/dim]"

    sources_count = detail.get("sources_count", 0)
    source_files = detail.get("source_files") or []
    sources_line = (
        f"[bold]Sources cited:[/bold] {sources_count}"
        + (f"  [dim]({', '.join(source_files[:4])}{'...' if len(source_files) > 4 else ''})[/dim]" if source_files else "")
    )

    retrieval_warning = ""
    if detail.get("retrieval_fallback_used"):
        retrieval_warning = "\n[bold red]WARNING:[/bold red] retrieval used relaxed filters — draft may be weakly grounded"

    header = (
        f"[bold]Thread:[/bold] {detail.get('thread_id', '?')}\n"
        f"{requester_line}\n"
        f"{age_line}\n"
        f"[bold]Reason:[/bold] {detail.get('reason') or '(none)'}\n"
        f"[bold]Amount referenced:[/bold] [yellow]{amt_str}[/yellow]   "
        f"[bold]Role threshold:[/bold] [dim]{thr_str}[/dim]\n"
        f"{conf_line}\n"
        f"{sources_line}"
        f"{retrieval_warning}"
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
        while True:
            entered = input_dialog(
                title="Reject reason (REQUIRED)",
                text=(
                    "Provide a clear reason for rejection. The requester will see this.\n"
                    "Leave blank to cancel and return to the inbox."
                ),
            ).run()
            if entered is None or not entered.strip():
                message_dialog(
                    title="Reject cancelled",
                    text="A non-empty reason is required to reject. Returning to inbox.",
                ).run()
                return False
            reason = entered.strip()
            break
    elif action == "approve":
        # Optional approval note — leave empty to skip
        entered = input_dialog(
            title="Approval note (optional)",
            text="Optional note that will be logged with the approval. Press Enter or cancel to skip.",
        ).run()
        reason = (entered or "").strip()

    body = {"thread_id": thread_id}
    if reason:
        body["reason"] = reason

    client = APIClient()
    try:
        with console.status(f"{action.capitalize()}ing and resuming graph...", spinner="dots"):
            resp = client.post(f"/v1/hitl/{action}", body)
    except APIError as e:
        message_dialog(title=f"{action.capitalize()} failed", text=e.message).run()
        return False
    finally:
        client.close()

    console.print()
    decided_at = resp.get("decided_at") or ""
    decided_at_local = f" at {decided_at}" if decided_at else ""
    if action == "approve":
        render_success(
            f"Approved by {resp.get('approver_user_id', '?')} "
            f"({resp.get('approver_role', '?')}){decided_at_local}. Released answer:"
        )
    else:
        render_info(
            f"Rejected by {resp.get('approver_user_id', '?')} "
            f"({resp.get('approver_role', '?')}){decided_at_local}.\n[dim]Reason:[/dim] {reason}"
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
            text=(
                "Arrow keys to navigate, Enter to review, Esc / q to exit.\n\n"
                f"    {_APPROVALS_HEADER}"
            ),
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
