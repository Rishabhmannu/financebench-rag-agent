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

from rich.panel import Panel
from rich.table import Table

from cli import credentials
from cli.api_client import DEFAULT_BASE_URL, APIClient, APIError
from cli.render import console, render_error, render_info, render_success


@dataclass
class ChatSession:
    """REPL-scoped state. One per `financebench chat` invocation.

    Bug B (audit): `token` is pinned at REPL boot and used by EVERY slash
    command's APIClient. Without this, the default `APIClient()` re-reads
    credentials from disk on each call — if a concurrent process or a
    `login -u <other>` in the same shell overwrites the profile file,
    the REPL keeps showing the old `user@` prompt while slash commands
    silently start hitting the backend as a different user (test 7 in
    cli-test.txt repro'd this exactly)."""

    user_id: str
    role: str
    base_url: str = DEFAULT_BASE_URL
    token: str | None = None
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
  /status            Backend version + sha + identity + session totals (on-demand banner)
  /role <name>       Re-login as analyst | finance | hr | clevel | admin
  /permissions       Show current role's RBAC access (doc types, confidentiality, HITL threshold)
  /thread new        Reset thread (start fresh conversation)
  /thread show       Thread context + per-turn timeline (wall clock, cost, sources, confidence)
  /threads           Switch active thread (arrow-key picker)
  /timings           Per-stage latency histogram across this REPL session (debug slow queries)
  /approvals         Open the approver inbox (admin/clevel only; arrow-key picker)
  /cost [N]          Show recent LLM cost from cost_logs/cost_log.jsonl (admin role)
  /audit [N]         Tail recent events from logs/run_*.jsonl (admin role)
  /clear             Clear the screen
  /help              This help text
  /quit, /exit       Leave the REPL

If your role has a HITL threshold (finance: $100K, clevel: $1M), high-stakes
queries pause and an authorized approver in another session must release them.
Your REPL polls /v1/chat/result and renders the answer once decided.

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

    if cmd == "/status":
        return _handle_status(session)

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

    if cmd == "/approvals":
        return _handle_approvals_slash(session)

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
            return _handle_thread_show(session)
        render_error(f"Unknown /thread subcommand: {sub!r}")
        return True

    if cmd == "/timings":
        return _handle_timings(session)

    render_error(f"Unknown slash command: {cmd!r}. Try /help.")
    return True


def _safe_int(s: str) -> int:
    try:
        return max(1, int(s))
    except (TypeError, ValueError):
        return 20


def _handle_status(session: ChatSession) -> bool:
    """On-demand snapshot of backend version + identity + session totals.

    0.1.8: surfaces the same info as the boot banner but at REPL time. M1
    test7 user typed `/status` (intuitive name) and got 'Unknown slash
    command' because the banner-equivalent didn't exist as a slash."""
    import os

    from cli import __version__ as cli_version
    from cli.credentials import current_profile

    api_version = api_semver = api_git_sha = None
    client = APIClient(base_url=session.base_url, token=session.token)
    try:
        v = client.get("/version", auth_required=False)
        api_version = v.get("api_version")
        api_semver = v.get("semver")
        api_git_sha = v.get("git_sha")
    except Exception:  # noqa: BLE001
        pass  # best-effort; render placeholders below
    finally:
        client.close()

    table = Table(show_header=False, show_lines=False, box=None, pad_edge=False)
    table.add_column("Field", style="dim")
    table.add_column("Value")
    table.add_row("Backend URL", session.base_url)
    table.add_row(
        "API",
        f"v{api_version or '?'}  (semver {api_semver or '?'}, sha {(api_git_sha or '?')[:7]})",
    )
    table.add_row("CLI", cli_version)
    table.add_row("Logged in as", f"{session.user_id}  (role={session.role})")
    table.add_row("Profile", current_profile())
    fb_profile_env = os.environ.get("FB_PROFILE", "")
    if fb_profile_env and fb_profile_env != "default":
        table.add_row("FB_PROFILE", fb_profile_env)
    table.add_row("Thread", session.thread_id or "[dim](new — first query starts one)[/dim]")
    table.add_row("Turns this session", str(session.turn_count))
    table.add_row(
        "Cumulative cost",
        f"${session.session_cost_usd:.4f}",
    )
    table.add_row(
        "Cumulative tokens",
        f"{session.session_tokens_in:,} in / {session.session_tokens_out:,} out",
    )
    console.print(table)
    return True


def _handle_permissions(session: ChatSession) -> bool:
    """Print the current role's RBAC access matrix via /v1/auth/me."""
    client = APIClient(base_url=session.base_url, token=session.token)
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
    """Arrow-key picker over the caller's threads. Switches session.thread_id
    to whatever's selected. Esc to keep current. Phase 3.6 promoted this from
    the list-only Phase 3 implementation now that the radiolist_dialog pattern
    is proven safe inside the active PromptSession."""
    from cli.interactive import interactive_thread_picker
    selected = interactive_thread_picker(
        current_thread_id=session.thread_id,
        token=session.token,
        base_url=session.base_url,
    )
    if selected is None:
        render_info("Kept current thread.")
        return True
    if selected == session.thread_id:
        render_info("Already on that thread.")
        return True
    session.thread_id = selected
    session.turn_count = 0  # subsequent turns will append to the new thread
    render_success(f"Switched to thread {selected[:8]}...")
    return True


def _handle_approvals_slash(session: ChatSession) -> bool:
    """Open the interactive approver inbox without leaving the REPL.

    Authorization is enforced server-side: the /v1/approvals endpoint returns
    only items the caller can approve. analyst/finance/hr see an empty inbox
    (no approval authority); clevel sees finance/hr/analyst requests; admin
    sees everything. Self-approval is blocked by the backend regardless.
    """
    from cli.interactive import interactive_approvals_loop
    interactive_approvals_loop(token=session.token, base_url=session.base_url)
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
    session.token = token   # Bug B (audit): keep session.token in lockstep with the new JWT
    session.thread_id = None
    session.turn_count = 0
    render_success(f"Switched to {target_user} (role={new_role}). Thread reset.")
    return True


def _handle_thread_show(session: ChatSession) -> bool:
    """0.1.2 enrichment per M1 test feedback (`/thread show didn't give much
    info`). Replaces the previous 4-line output with:
      - Identity panel: thread_id, owner (from /v1/threads/{id}), turns, cost,
        tokens, last activity
      - Turn-by-turn timeline: query preview, wall-clock, cost, sources,
        confidence — sourced from session.history (populated by _run_turn)
      - HITL audit panel when the thread has a decision recorded
    """
    tid = session.thread_id
    if not tid:
        render_info("No active thread yet. Run a query first.")
        return True

    # Identity + HITL audit from the backend
    owner_block = None
    audit_block = None
    last_activity = None
    try:
        client = APIClient(base_url=session.base_url, token=session.token)
        try:
            resp = client.get(f"/v1/threads/{tid}")
            owner_block = resp.get("owner") or {}
            audit_block = resp.get("audit")
            last_activity = resp.get("last_activity_at")
        finally:
            client.close()
    except APIError as e:
        render_info(f"(Could not fetch thread metadata: {e.message})")

    # Identity panel
    summary_lines = [
        f"[bold]Thread:[/bold] {tid}",
        f"[bold]Turns:[/bold] {session.turn_count}   "
        f"[bold]Cost:[/bold] [yellow]${session.session_cost_usd:.4f}[/yellow]   "
        f"[bold]Tokens:[/bold] [dim]{session.session_tokens_in} in / {session.session_tokens_out} out[/dim]",
    ]
    if owner_block:
        owner_str = (
            f"{owner_block.get('user_id', '?')} "
            f"([cyan]{owner_block.get('role', '?')}[/cyan] · {owner_block.get('department') or '—'})"
        )
        summary_lines.append(f"[bold]Owner:[/bold] {owner_str}")
    if last_activity:
        summary_lines.append(f"[bold]Last activity:[/bold] {last_activity}")
    console.print(Panel("\n".join(summary_lines), title="Thread context", expand=False))

    # HITL audit if applicable
    if audit_block:
        decision = (audit_block.get("decision") or "?").upper()
        decision_color = "green" if decision == "APPROVED" else ("red" if decision == "REJECTED" else "yellow")
        console.print(Panel(
            f"[bold]Submitted at:[/bold] {audit_block.get('hitl_submitted_at') or '—'}\n"
            f"[bold]Decided at:[/bold] {audit_block.get('decided_at') or '—'}\n"
            f"[bold]Decision:[/bold] [{decision_color}]{decision}[/{decision_color}]"
            f" by {audit_block.get('decided_by') or '—'} ([cyan]{audit_block.get('decided_by_role') or '?'}[/cyan])\n"
            f"[bold]Reason:[/bold] {audit_block.get('reason') or '[dim](no reason)[/dim]'}",
            title="HITL audit",
            border_style="yellow",
            expand=False,
        ))

    # Per-turn timeline from session.history (REPL-local, may be empty if
    # the REPL was just opened and the thread is a continuation from earlier)
    turns_for_thread = [t for t in session.history if t.get("thread_id") == tid]
    if turns_for_thread:
        table = Table(title="Turns this REPL session", header_style="bold cyan", title_style="bold")
        table.add_column("#", style="dim", justify="right")
        table.add_column("Query", overflow="fold")
        table.add_column("Wall", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Tokens", justify="right", style="dim")
        table.add_column("Sources", justify="right", style="dim")
        table.add_column("Conf", justify="right")
        for t in turns_for_thread:
            wall = t.get("wall_clock_s")
            wall_str = f"{wall:.1f}s" if wall is not None else "—"
            conf = t.get("confidence")
            conf_str = f"{conf:.2f}" if conf is not None else "—"
            table.add_row(
                str(t.get("turn", "?")),
                t.get("query_preview", "") or "(empty)",
                wall_str,
                f"${t.get('cost_usd', 0):.4f}",
                f"{t.get('tokens_in', 0):,}/{t.get('tokens_out', 0):,}",
                str(t.get("n_sources", 0)),
                conf_str,
            )
        console.print(table)
    else:
        console.print(
            "[dim](No turns recorded in this REPL session. Continuation threads "
            "show only metadata above; per-turn timing is captured from the "
            "current session forward.)[/dim]"
        )

    return True


def _handle_timings(session: ChatSession) -> bool:
    """0.1.2 — per-stage latency histogram across all turns in this REPL.
    Useful for diagnosing slow stages on a given machine (M1 vs M4 etc.)."""
    if not session.history:
        render_info("No turns recorded yet. Run a query first.")
        return True

    # Aggregate stage timings across all turns
    from collections import defaultdict
    per_stage_ms: dict[str, list[int]] = defaultdict(list)
    wall_clocks: list[float] = []
    for t in session.history:
        wc = t.get("wall_clock_s")
        if wc is not None:
            wall_clocks.append(float(wc))
        for stage, ms in (t.get("stage_timings_ms") or {}).items():
            per_stage_ms[stage].append(int(ms))

    if not per_stage_ms:
        render_info("No per-stage timing data — turns may have been served by an older backend version.")
        return True

    def _pct(xs: list[int], q: float) -> int:
        if not xs:
            return 0
        s = sorted(xs)
        i = int((len(s) - 1) * q)
        return s[i]

    table = Table(title=f"Stage timings across {len(session.history)} turn(s)", header_style="bold cyan", title_style="bold")
    table.add_column("Stage", style="cyan")
    table.add_column("Calls", justify="right", style="dim")
    table.add_column("Avg (ms)", justify="right")
    table.add_column("p50", justify="right", style="dim")
    table.add_column("p95", justify="right", style="dim")
    table.add_column("Max", justify="right", style="dim")

    # Sort by average descending — slowest stage first
    stages_sorted = sorted(
        per_stage_ms.items(),
        key=lambda kv: sum(kv[1]) / max(len(kv[1]), 1),
        reverse=True,
    )
    for stage, samples in stages_sorted:
        avg = sum(samples) // len(samples)
        table.add_row(
            stage,
            str(len(samples)),
            f"{avg:,}",
            f"{_pct(samples, 0.5):,}",
            f"{_pct(samples, 0.95):,}",
            f"{max(samples):,}",
        )
    console.print(table)

    if wall_clocks:
        avg_wall = sum(wall_clocks) / len(wall_clocks)
        console.print(
            f"[dim]Wall clock per turn: avg {avg_wall:.1f}s · "
            f"min {min(wall_clocks):.1f}s · max {max(wall_clocks):.1f}s[/dim]"
        )

    return True
