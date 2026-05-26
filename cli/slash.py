"""Slash command dispatch for the chat REPL.

Phase 2 ships: /quit, /exit, /help, /clear, /role, /thread {new,show}.
Phase 3 will add /threads (with arrow-key selection), /hitl approve/reject,
/cost, /audit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from getpass import getpass

from cli import credentials
from cli.api_client import DEFAULT_BASE_URL, APIClient, APIError
from cli.render import console, render_error, render_info, render_success


@dataclass
class ChatSession:
    """REPL-scoped state. One per `financebench chat` invocation."""

    user_id: str
    role: str
    base_url: str = DEFAULT_BASE_URL
    thread_id: str | None = None
    turn_count: int = 0
    session_cost_usd: float = 0.0
    session_tokens_in: int = 0
    session_tokens_out: int = 0
    history: list[dict] = field(default_factory=list)

    @property
    def prompt_label(self) -> str:
        return f"{self.user_id}@financebench"


_HELP = """
Available slash commands:
  /role <name>       Re-login as analyst | finance | hr | clevel | admin
  /thread new        Reset thread (start fresh conversation)
  /thread show       Show current thread id + turn count + session cost
  /clear             Clear the screen
  /help              This help text
  /quit, /exit       Leave the REPL

Anything not starting with `/` is sent as a chat query to the agent.
""".strip()


def handle(text: str, session: ChatSession) -> bool:
    """Dispatch a slash command. Returns False to signal REPL exit, True to continue."""
    parts = text.strip().split()
    if not parts:
        return True
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/quit", "/exit"):
        render_info("Bye.")
        return False

    if cmd == "/help":
        console.print(_HELP)
        return True

    if cmd == "/clear":
        os.system("clear" if os.name != "nt" else "cls")
        return True

    if cmd == "/role":
        if not args:
            render_error("Usage: /role <name>  (analyst | finance | hr | clevel | admin)")
            return True
        return _handle_role(args[0], session)

    if cmd == "/thread":
        if not args:
            render_error("Usage: /thread new  |  /thread show")
            return True
        sub = args[0].lower()
        if sub == "new":
            session.thread_id = None
            session.turn_count = 0
            render_success("Started a fresh thread.")
            return True
        if sub == "show":
            tid = session.thread_id or "(none — next query starts a new thread)"
            console.print(f"thread: {tid}")
            console.print(f"turns:  {session.turn_count}")
            console.print(f"cost:   ${session.session_cost_usd:.4f}")
            console.print(f"tokens: {session.session_tokens_in} in / {session.session_tokens_out} out")
            return True
        render_error(f"Unknown /thread subcommand: {sub!r}")
        return True

    render_error(f"Unknown slash command: {cmd!r}. Try /help.")
    return True


def _handle_role(target_user: str, session: ChatSession) -> bool:
    """Re-login as a different test user. Updates session in-place. Prompts
    for the password interactively (same UX as `financebench login`)."""
    if target_user == session.user_id:
        render_info(f"Already logged in as {target_user}.")
        return True

    password = getpass(f"Password for {target_user}: ")
    client = APIClient(base_url=session.base_url, token=None)
    try:
        resp = client.post(
            "/v1/auth/login",
            {"username": target_user, "password": password},
            auth_required=False,
        )
    except APIError as e:
        render_error(f"Re-login failed: {e.message}")
        return True
    finally:
        client.close()

    token = resp.get("access_token")
    if not token:
        render_error("Login response missing access_token.")
        return True

    me_client = APIClient(base_url=session.base_url, token=token)
    new_role = "?"
    try:
        me = me_client.get("/v1/auth/me")
        new_role = me.get("role", "?")
    except APIError:
        pass
    finally:
        me_client.close()

    credentials.save(token=token, user_id=target_user, base_url=session.base_url)
    session.user_id = target_user
    session.role = new_role
    session.thread_id = None
    session.turn_count = 0
    render_success(f"Switched to {target_user} (role={new_role}). Thread reset.")
    return True
