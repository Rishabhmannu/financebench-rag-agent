"""rich-based renderers for the CLI's terminal output."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console()


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
