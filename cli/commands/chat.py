"""financebench chat — REPL by default (Phase 2); --no-stream for one-shot scripting."""

from __future__ import annotations

import time
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from rich.markdown import Markdown

from cli import credentials, slash
from cli.api_client import APIClient, APIError
from cli.render import (
    console,
    render_error,
    render_final_footer,
    render_info,
    render_response,
    render_success,
)
from cli.sse_consumer import render_chat_stream

HISTORY_PATH = Path.home() / ".financebench" / "history"


def chat(
    message: str = typer.Argument(None, help="One-shot query. Omit to enter the REPL."),
    no_stream: bool = typer.Option(
        False,
        "--no-stream",
        help="Use the non-streaming /v1/chat endpoint. Useful for scripting; REPL is the default otherwise.",
    ),
    thread_id: str = typer.Option(None, "--thread-id", help="Continue an existing thread"),
) -> None:
    """Chat with the agent. REPL by default; pass a message + --no-stream for one-shot."""
    creds = credentials.load()
    if creds is None:
        # M1 feedback (0.1.0): the hardcoded "-u analyst" suggestion confused
        # users on other profiles. Make it generic + list available dev users
        # so anyone can pick the right one regardless of FB_PROFILE.
        render_error(
            "Not logged in. Run:  financebench login -u <username>\n\n"
            "Available dev accounts: analyst | finance | hr | clevel | admin"
        )
        raise typer.Exit(1)

    if message and no_stream:
        _one_shot_non_streaming(message, thread_id)
        return
    if message and not no_stream:
        _one_shot_streaming(message, thread_id)
        return

    _repl(creds)


def _one_shot_non_streaming(message: str, thread_id: str | None) -> None:
    client = APIClient()
    try:
        body: dict = {"message": message}
        if thread_id:
            body["thread_id"] = thread_id
        render_info(f"Querying {client.base_url}/v1/chat (non-streaming) ...")
        resp = client.post("/v1/chat", body)
    except APIError as e:
        if e.status_code == 401:
            render_error("Auth expired. Run: financebench login")
        else:
            render_error(f"Chat failed: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()

    render_response(
        text=resp.get("response", ""),
        sources=resp.get("sources", []),
        confidence=resp.get("confidence"),
    )
    render_final_footer({
        "sources": [],
        "confidence": None,
        "cost_usd": resp.get("cost_usd"),
        "tokens": resp.get("tokens"),
    })


def _one_shot_streaming(message: str, thread_id: str | None) -> None:
    client = APIClient()
    try:
        body: dict = {"message": message}
        if thread_id:
            body["thread_id"] = thread_id
        render_chat_stream(client.stream_chat(body))
    except APIError as e:
        if e.status_code == 401:
            render_error("Auth expired. Run: financebench login")
        else:
            render_error(f"Chat failed: {e.message}")
        raise typer.Exit(1)
    finally:
        client.close()


def _prewarm(client: APIClient) -> None:
    """Force the backend to lazy-load the BGE reranker + sparse embedder before
    the user's first query, so first-query latency matches subsequent queries
    (Phase 1 feedback)."""
    try:
        with console.status("Pre-warming backend models (BGE reranker, sparse embedder)...", spinner="dots"):
            client.get("/v1/warm", auth_required=False)
    except APIError as e:
        render_info(f"Pre-warm skipped: {e.message}")
    except Exception as e:  # noqa: BLE001
        render_info(f"Pre-warm skipped: {e}")


def _repl(creds: dict) -> None:
    user_id = creds.get("user_id", "?")
    base_url = creds.get("base_url", "http://localhost:8000")

    client = APIClient(base_url=base_url, token=creds.get("token"))

    role = "?"
    try:
        me = client.get("/v1/auth/me")
        role = me.get("role", "?")
    except APIError as e:
        render_error(f"Could not load identity from {base_url}: {e.message}")
        client.close()
        raise typer.Exit(1)

    # Pull backend version info for the banner. Best-effort — banner still
    # renders if /version is unreachable, just with "?" placeholders.
    api_version = api_semver = api_git_sha = None
    try:
        v = client.get("/version", auth_required=False)
        api_version = v.get("api_version")
        api_semver = v.get("semver")
        api_git_sha = v.get("git_sha")
    except Exception:  # noqa: BLE001
        pass

    profile = credentials.current_profile()

    from cli.render import render_startup_banner
    render_startup_banner(
        backend_url=base_url,
        api_version=api_version,
        api_semver=api_semver,
        api_git_sha=api_git_sha,
        user_id=user_id,
        role=role,
        profile=profile,
    )

    # Bug B (audit): pin the JWT into session state at REPL boot so subsequent
    # slash commands hit the backend with the same identity the user saw at
    # login. Otherwise a stale or concurrently-overwritten profile file would
    # drift the slash commands away from the REPL prompt label.
    session_state = slash.ChatSession(
        user_id=user_id, role=role, base_url=base_url, token=creds.get("token")
    )

    _prewarm(client)

    HISTORY_PATH.parent.mkdir(mode=0o700, exist_ok=True)
    prompt_session: PromptSession = PromptSession(history=FileHistory(str(HISTORY_PATH)))

    if profile == "default":
        console.print(
            "[dim]Tip: set FB_PROFILE=admin (or any name) in different terminals "
            "to keep separate identities for the multi-party HITL demo.[/dim]"
        )
    console.print("[dim]Type a question, or /help for slash commands. Ctrl+D to exit.[/dim]")

    while True:
        prompt_text = HTML(f"<ansicyan>{session_state.prompt_label}</ansicyan>&gt; ")
        try:
            text = prompt_session.prompt(prompt_text)
        except (EOFError, KeyboardInterrupt):
            render_info("Bye.")
            break

        text = text.strip()
        if not text:
            continue

        if text.startswith("/"):
            if not slash.handle(text, session_state):
                break
            # /role may have changed credentials; reload client token. Session
            # state's `token` is the source of truth (Bug B fix) — fall back to
            # disk only if the slash didn't update it.
            if session_state.user_id != user_id or session_state.role != role:
                user_id = session_state.user_id
                role = session_state.role
                client.close()
                new_token = session_state.token or (credentials.load() or {}).get("token")
                client = APIClient(base_url=base_url, token=new_token)
            continue

        _run_turn(client, text, session_state)

    client.close()


def _run_turn(client: APIClient, message: str, session: slash.ChatSession) -> None:
    body: dict = {"message": message}
    if session.thread_id:
        body["thread_id"] = session.thread_id
    try:
        terminal = render_chat_stream(client.stream_chat(body))
    except APIError as e:
        if e.status_code == 401:
            render_error("Auth expired. Run: financebench login (or use /role <name>)")
            return
        render_error(f"Chat failed: {e.message}")
        return

    session.turn_count += 1
    if terminal.get("thread_id"):
        session.thread_id = terminal["thread_id"]
    cost = terminal.get("cost_usd") or 0.0
    tokens = terminal.get("tokens") or {}
    session.session_cost_usd += float(cost)
    session.session_tokens_in += int(tokens.get("input", 0))
    session.session_tokens_out += int(tokens.get("output", 0))

    if terminal.get("type") in ("pending_approval", "hitl_interrupt"):
        _wait_for_approval(client, terminal)


def _wait_for_approval(client: APIClient, pending_event: dict) -> None:
    """Phase 3.5: requester poll-waits for an authorized approver to release
    the answer (or reject). Polls /v1/chat/result/{thread_id} every 3s for up
    to 5 minutes. Ctrl+C drops out cleanly; the pause stays alive in Postgres."""
    thread_id = pending_event.get("thread_id")
    if not thread_id:
        render_error("No thread_id in pending event; can't poll.")
        return

    approvers = pending_event.get("approvers") or []
    approver_str = ", ".join(approvers) if approvers else "an authorized role"

    console.print()
    poll_interval_s = 3
    max_wait_s = 300

    started = time.monotonic()
    try:
        with console.status(
            f"Waiting for approval by {approver_str}... "
            f"(thread {thread_id[:8]}..., Ctrl+C to drop)",
            spinner="dots",
        ) as status_ui:
            while time.monotonic() - started < max_wait_s:
                time.sleep(poll_interval_s)
                try:
                    resp = client.get(f"/v1/chat/result/{thread_id}")
                except APIError as e:
                    render_error(f"Poll failed: {e.message}")
                    return
                st = resp.get("status")
                if st in ("approved", "ready"):
                    status_ui.stop()
                    console.print()
                    decision = resp.get("decision") or {}
                    decided_by = decision.get("decided_by")
                    decided_by_role = decision.get("decided_by_role")
                    decided_at = decision.get("decided_at")
                    reason = (decision.get("reason") or "").strip()
                    # Bug E (audit): use "Released" not "Approved" on the
                    # requester side. The workflow verb (approver released the
                    # draft) is distinct from the substance verb (the AI's
                    # answer may itself say "I cannot approve this expense"),
                    # and conflating them in the same panel confused testers.
                    if decided_by:
                        suffix = f" by {decided_by} ({decided_by_role})" if decided_by_role else f" by {decided_by}"
                        suffix += f" at {decided_at}" if decided_at else ""
                        render_success(f"Draft released{suffix}:")
                        if reason:
                            console.print(f"[dim]Approver note:[/dim] {reason}")
                    else:
                        render_success("Draft released:")
                    response_text = (resp.get("response") or "").strip()
                    if response_text:
                        console.print(Markdown(response_text))
                    render_final_footer({
                        "sources": resp.get("sources") or [],
                        "confidence": resp.get("confidence"),
                    })
                    return
                if st == "rejected":
                    status_ui.stop()
                    console.print()
                    decision = resp.get("decision") or {}
                    decided_by = decision.get("decided_by")
                    decided_by_role = decision.get("decided_by_role")
                    decided_at = decision.get("decided_at")
                    reason = (decision.get("reason") or "").strip()
                    if decided_by:
                        suffix = f" by {decided_by} ({decided_by_role})" if decided_by_role else f" by {decided_by}"
                        suffix += f" at {decided_at}" if decided_at else ""
                        render_info(f"Draft withheld{suffix}.")
                    else:
                        render_info("Draft withheld by approver.")
                    if reason:
                        console.print(f"[yellow]Reason:[/yellow] {reason}")
                    response_text = (resp.get("response") or "").strip()
                    if response_text:
                        console.print(Markdown(response_text))
                    return
                # status == "pending" -> keep waiting
        # Timed out
        render_info(
            f"Timed out after {max_wait_s}s waiting for approval. The pause "
            f"is still alive in Postgres -- resume later with: financebench "
            f"chat (will pick up if no new turn) or check `financebench threads show {thread_id}`."
        )
    except KeyboardInterrupt:
        render_info(
            f"Wait cancelled. Approval pause remains; check back with "
            f"`financebench threads show {thread_id}`."
        )
