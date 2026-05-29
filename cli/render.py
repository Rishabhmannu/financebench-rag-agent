"""rich-based renderers for the CLI's terminal output."""

from __future__ import annotations

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


# FastMCP-style boxed startup banner. Hardcoded ASCII (figlet "standard" font)
# so the logo is deterministic and ships with zero new deps.
_LOGO = r"""  _____ _                            ____                  _
 |  ___(_)_ __   __ _ _ __   ___ ___| __ )  ___ _ __   ___| |__
 | |_  | | '_ \ / _` | '_ \ / __/ _ \  _ \ / _ \ '_ \ / __| '_ \
 |  _| | | | | | (_| | | | | (_|  __/ |_) |  __/ | | | (__| | | |
 |_|   |_|_| |_|\__,_|_| |_|\___\___|____/ \___|_| |_|\___|_| |_|"""


def render_startup_banner(
    *,
    backend_url: str,
    api_version: str | None,
    api_semver: str | None,
    api_git_sha: str | None,
    user_id: str,
    role: str,
    profile: str,
) -> None:
    """REPL startup banner. Version values come from cli.__version__ (locked
    at release time by pyproject.toml) and the backend's /version response,
    so the banner auto-reflects whichever versions are live without manual
    edits per release."""
    from cli import __version__ as cli_version

    logo = Text(_LOGO, style="bold green")
    info = Text.from_markup(
        f"  [cyan]Package:[/]        financebench-rag-agent\n"
        f"  [cyan]Backend URL:[/]    [underline]{backend_url}[/]\n"
        f"  [cyan]Docs:[/]           [underline]https://github.com/Rishabhmannu/financebench-rag-agent[/]\n"
        f"\n"
        f"  [yellow]CLI version:[/]    {cli_version}\n"
        f"  [yellow]API version:[/]    {api_version or '?'}  "
        f"[dim](semver {api_semver or '?'}, sha {(api_git_sha or '?')[:7]})[/]\n"
        f"  [yellow]Logged in as:[/]   [bold]{user_id}[/]  "
        f"[dim](role={role} · Profile={profile})[/]"
    )
    console.print(
        Panel(
            Group(logo, "", info),
            title="[bold]FinanceBench RAG Agent[/]",
            title_align="left",
            border_style="bright_blue",
            padding=(1, 2),
        )
    )


def render_response(
    text: str,
    sources: list[dict] | None = None,
    confidence: float | None = None,
) -> None:
    console.print()
    console.print(Markdown(text or "_(empty response)_"))

    if sources:
        console.print()
        table = Table(title="Sources", show_lines=False, header_style="bold cyan", title_style="bold")
        table.add_column("Document", style="cyan", overflow="fold")
        table.add_column("Page", justify="right", style="dim")
        table.add_column("Section", style="dim", overflow="fold")
        table.add_column("Type", style="dim")
        for s in sources[:8]:
            if not isinstance(s, dict):
                continue
            doc = s.get("file") or s.get("filename") or s.get("source") or "?"
            page = s.get("page")
            page_str = str(page) if page is not None else "—"
            section = (s.get("section") or "")[:60]
            doc_type = s.get("doc_type") or ""
            table.add_row(str(doc), page_str, section, doc_type)
        console.print(table)

    if confidence is not None:
        color = "green" if confidence >= 0.7 else "yellow" if confidence >= 0.4 else "red"
        console.print(f"\n[{color}]Confidence: {confidence:.2f}[/{color}]")


def render_error(message: str) -> None:
    console.print(Panel(message, title="Error", border_style="red", title_align="left"))


def render_success(message: str) -> None:
    console.print(f"[bold green][OK][/bold green] {message}")


def render_info(message: str) -> None:
    console.print(f"[bold blue][INFO][/bold blue] {message}")


def render_final_response_text(payload: dict) -> None:
    """Render the response text from a final event when no tokens were streamed
    during the turn (e.g., the backend short-circuited with a fallback message
    before reaching the generator's astream path)."""
    text = (payload.get("response") or "").strip()
    if not text:
        return
    console.print()
    console.print(Markdown(text))


def render_final_footer(payload: dict) -> None:
    """Print sources table + confidence + cost footer for a /chat/stream final
    event (or a non-streaming chat response with the same shape)."""
    sources = payload.get("sources") or []
    if sources:
        console.print()
        table = Table(title="Sources", show_lines=False, header_style="bold cyan", title_style="bold")
        table.add_column("Document", style="cyan", overflow="fold")
        table.add_column("Page", justify="right", style="dim")
        table.add_column("Section", style="dim", overflow="fold")
        table.add_column("Type", style="dim")
        for s in sources[:8]:
            if not isinstance(s, dict):
                continue
            doc = s.get("file") or s.get("filename") or s.get("source") or "?"
            page = s.get("page")
            page_str = str(page) if page is not None else "—"
            section = (s.get("section") or "")[:60]
            doc_type = s.get("doc_type") or ""
            table.add_row(str(doc), page_str, section, doc_type)
        console.print(table)

    confidence = payload.get("confidence")
    cost_usd = payload.get("cost_usd")
    tokens = payload.get("tokens") or {}
    wall_clock_s = payload.get("wall_clock_s")
    stage_timings_ms = payload.get("stage_timings_ms") or {}

    footer_parts: list[str] = []
    if confidence is not None:
        color = "green" if confidence >= 0.7 else "yellow" if confidence >= 0.4 else "red"
        footer_parts.append(f"[{color}]conf {confidence:.2f}[/{color}]")
    if cost_usd is not None:
        footer_parts.append(f"[dim]${cost_usd:.4f}[/dim]")
    if tokens:
        t_in = tokens.get("input", 0)
        t_out = tokens.get("output", 0)
        footer_parts.append(f"[dim]{t_in} in / {t_out} out[/dim]")
    if wall_clock_s is not None:
        # Color-code by latency band so slow turns are visually flagged. 60s
        # is the steady-state benchmark on a warm M4 Pro; 120s+ means the
        # M1 is hot or something's cold-starting.
        wall_color = "green" if wall_clock_s < 60 else "yellow" if wall_clock_s < 120 else "red"
        footer_parts.append(f"[{wall_color}]⏱ {wall_clock_s:.1f}s[/{wall_color}]")

    if footer_parts:
        console.print()
        console.print(" · ".join(footer_parts))

    # Per-stage timing breakdown — only show stages that took >500ms so the
    # footer stays scannable. Helps the M1 case where reranker dominates.
    if stage_timings_ms:
        notable = [(k, v) for k, v in stage_timings_ms.items() if v >= 500]
        notable.sort(key=lambda kv: kv[1], reverse=True)
        if notable:
            stage_bits = []
            for stage, ms in notable[:6]:
                s = ms / 1000
                stage_bits.append(f"[dim]{stage} {s:.1f}s[/dim]")
            console.print(" · ".join(stage_bits))


def render_hitl_panel(payload: dict) -> None:
    """Render the HITL pause panel. Phase 3.5: the requester never sees the
    draft answer. Panel shows reason + amount + threshold + the list of
    approver roles whose attention is now required."""
    reason = payload.get("reason") or "Approval required"
    amount = payload.get("max_amount")
    threshold = payload.get("threshold")
    approvers = payload.get("approvers") or []
    body = f"[bold]{reason}[/bold]"
    if amount is not None and threshold is not None:
        body += (
            f"\n\n[dim]Amount referenced:[/dim] ${amount:,.0f}"
            f"\n[dim]Role threshold:[/dim]    ${threshold:,.0f}"
        )
    if approvers:
        body += (
            f"\n\n[bold]Awaiting approval by:[/bold] {', '.join(approvers)}"
            f"\n[dim](your draft answer is not shown — only an authorized "
            f"approver sees it via `financebench approvals show <id>`)[/dim]"
        )
    console.print(Panel(
        body,
        title="Pending approval (HITL)",
        border_style="yellow",
        title_align="left",
    ))


def make_status(label: str = "Connecting..."):
    """Return a fresh `console.status` context. Caller uses .update(text) to
    change the spinner label and .stop() to exit it (e.g. before printing
    streaming tokens)."""
    return console.status(label, spinner="dots")
