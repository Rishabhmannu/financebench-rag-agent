"""Slash command dispatch for the chat REPL.

Phase 2 shipped: /quit, /exit, /help, /clear, /role, /thread {new,show}.
Phase 3 adds: /permissions, /cost, /audit, /threads (with arrow-key selection).
HITL approve/reject is handled inline by cli/commands/chat.py when the SSE
stream returns a hitl_interrupt terminal event.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path

from rich.table import Table

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
  /permissions       Show current role's RBAC access (doc types, confidentiality, HITL threshold)
  /thread new        Reset thread (start fresh conversation)
  /thread show       Show current thread id + turn count + session cost
  /threads           List your prior conversations (Phase 3 wire-up)
  /cost [N]          Show recent LLM cost from cost_logs/cost_log.jsonl (admin role)
  /audit [N]         Tail recent events from logs/run_*.jsonl (admin role)
  /clear             Clear the screen
  /help              This help text
  /quit, /exit       Leave the REPL

When the chat pauses for HITL approval (high-stakes amount above your role
threshold), an inline a/r/k prompt fires. The thread stays paused in Postgres
until you decide.

Anything not starting with `/` is sent as a chat query to the agent.
""".strip()


# Default paths inside the repo root, relative to where the CLI is run.
# (When the api runs in the minimal compose, ./cost_logs and ./logs are
# volume-mounted from the host -- so CLI on the host can read them directly.)
_REPO_ROOT = Path.cwd()
_COST_LOG = _REPO_ROOT / "cost_logs" / "cost_log.jsonl"
_LOGS_DIR = _REPO_ROOT / "logs"


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

    if cmd == "/permissions":
        return _handle_permissions(session)

    if cmd == "/cost":
        n = _safe_int(args[0]) if args else 20
        return _handle_cost(session, n=n)

    if cmd == "/audit":
        n = _safe_int(args[0]) if args else 15
        return _handle_audit(session, n=n)

    if cmd == "/threads":
        return _handle_threads(session)

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


def _safe_int(s: str) -> int:
    try:
        return max(1, int(s))
    except (TypeError, ValueError):
        return 20


def _handle_permissions(session: ChatSession) -> bool:
    """Print the current role's RBAC access matrix via /v1/auth/me."""
    client = APIClient()
    try:
        me = client.get("/v1/auth/me")
    except APIError as e:
        render_error(f"Could not fetch permissions: {e.message}")
        return True
    finally:
        client.close()

    perms = me.get("permissions") or {}
    table = Table(show_header=False, show_lines=False, box=None, pad_edge=False)
    table.add_column("Field", style="dim")
    table.add_column("Value")
    table.add_row("User", me.get("user_id", "?"))
    table.add_row("Role", me.get("role", "?"))
    table.add_row("Department", me.get("department", "?"))
    allowed_doc = perms.get("allowed_doc_types", [])
    allowed_doc_str = "[green]ALL (*)[/green]" if "*" in allowed_doc else ", ".join(allowed_doc) or "[red]none[/red]"
    table.add_row("Allowed doc types", allowed_doc_str)
    allowed_conf = perms.get("allowed_confidentiality", [])
    allowed_conf_str = "[green]ALL (*)[/green]" if "*" in allowed_conf else ", ".join(allowed_conf) or "[red]none[/red]"
    table.add_row("Allowed confidentiality", allowed_conf_str)
    table.add_row("Max results per query", str(perms.get("max_results", "?")))
    hitl = perms.get("requires_hitl_above")
    hitl_str = f"${hitl:,}" if hitl else "[dim](no HITL gate)[/dim]"
    table.add_row("HITL threshold", hitl_str)
    console.print(table)
    return True


def _handle_cost(session: ChatSession, n: int = 20) -> bool:
    """Tail the on-disk cost_log.jsonl and render a cost summary table. Admin only."""
    if session.role != "admin":
        render_error(f"/cost is admin-only (current role: {session.role}). Use /role admin.")
        return True
    if not _COST_LOG.exists():
        render_error(f"No cost log at {_COST_LOG}. Has the api run any LLM calls yet?")
        return True

    try:
        with _COST_LOG.open() as f:
            lines = f.readlines()
    except OSError as e:
        render_error(f"Could not read cost log: {e}")
        return True

    recent = []
    for line in lines[-n:]:
        try:
            recent.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not recent:
        render_info("No cost records yet.")
        return True

    per_model: Counter[str] = Counter()
    cost_per_model: dict[str, float] = {}
    total_cost = 0.0
    total_in = 0
    total_out = 0
    for r in recent:
        model = r.get("model", "?")
        per_model[model] += 1
        cost = float(r.get("cost_usd") or 0.0)
        cost_per_model[model] = cost_per_model.get(model, 0.0) + cost
        total_cost += cost
        total_in += int(r.get("input_tokens") or 0)
        total_out += int(r.get("output_tokens") or 0)

    table = Table(title=f"Cost from last {len(recent)} LLM calls", header_style="bold cyan", title_style="bold")
    table.add_column("Model", style="cyan")
    table.add_column("Calls", justify="right", style="dim")
    table.add_column("Cost (USD)", justify="right")
    for model, calls in per_model.most_common():
        table.add_row(model, str(calls), f"${cost_per_model[model]:.4f}")
    console.print(table)
    console.print(
        f"[bold]Total:[/bold] ${total_cost:.4f} "
        f"[dim]({total_in:,} in / {total_out:,} out tokens, {len(recent)} calls)[/dim]"
    )
    if (Path.cwd() / "docker-compose.yml").exists():
        console.print("[dim]Full mode: view richer dashboards at http://localhost:3000 (Langfuse)[/dim]")
    return True


def _handle_audit(session: ChatSession, n: int = 15) -> bool:
    """Tail recent structured events from logs/run_*.jsonl (Sprint 7.19 audit
    trail). Admin only."""
    if session.role != "admin":
        render_error(f"/audit is admin-only (current role: {session.role}). Use /role admin.")
        return True
    if not _LOGS_DIR.exists():
        render_error(f"No logs dir at {_LOGS_DIR}.")
        return True

    runs = sorted(_LOGS_DIR.glob("run_*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not runs:
        render_info("No run logs yet.")
        return True

    latest = runs[-1]
    try:
        with latest.open() as f:
            lines = f.readlines()
    except OSError as e:
        render_error(f"Could not read {latest}: {e}")
        return True

    recent: list[dict] = []
    for line in lines[-n:]:
        try:
            recent.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not recent:
        render_info(f"No events in {latest.name}.")
        return True

    table = Table(title=f"Recent events from {latest.name} (last {len(recent)})",
                  header_style="bold cyan", title_style="bold")
    table.add_column("Timestamp", style="dim", overflow="fold")
    table.add_column("Stage", style="cyan")
    table.add_column("Key fields", overflow="fold")
    for r in recent:
        ts = (r.get("ts") or "")[11:19]
        stage = r.get("stage", "?")
        skip = {"ts", "run_id", "fb_id", "stage"}
        summary_bits = []
        for k, v in r.items():
            if k in skip:
                continue
            sval = str(v)
            if len(sval) > 50:
                sval = sval[:47] + "..."
            summary_bits.append(f"{k}={sval}")
            if len(summary_bits) >= 4:
                summary_bits.append("...")
                break
        table.add_row(ts, stage, " ".join(summary_bits))
    console.print(table)
    return True


def _handle_threads(session: ChatSession) -> bool:
    """List threads and optionally switch active thread via arrow-key selection."""
    client = APIClient()
    try:
        resp = client.get("/v1/threads?limit=20")
    except APIError as e:
        render_error(f"Could not list threads: {e.message}")
        return True
    finally:
        client.close()

    threads = resp.get("threads") or []
    if not threads:
        render_info("No threads yet. Ask a question to start one.")
        return True

    table = Table(title=f"Your threads ({len(threads)} of {resp.get('total', '?')})",
                  header_style="bold cyan", title_style="bold")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Thread ID", style="cyan", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Checkpoints", justify="right", style="dim")
    table.add_column("Status")
    for i, t in enumerate(threads, 1):
        tid = t.get("thread_id", "?")
        if session.thread_id and tid == session.thread_id:
            tid = f"[bold green]{tid}[/bold green]"
        status = "[yellow]paused (HITL)[/yellow]" if t.get("is_interrupted") else ""
        table.add_row(str(i), tid, (t.get("title") or "[dim](no title)[/dim]"), str(t.get("checkpoint_count", "?")), status)
    console.print(table)
    console.print("[dim]Tip: switch threads by sending a query with --thread-id, "
                  "or start fresh with /thread new.[/dim]")
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
