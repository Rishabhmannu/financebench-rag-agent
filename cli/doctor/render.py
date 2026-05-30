"""Render doctor results as a grouped report (flutter doctor-style)."""

from __future__ import annotations

from cli.doctor.types import CheckResult, Status
from cli.render import console

_STATUS_STYLE = {
    Status.PASS: "green",
    Status.WARN: "yellow",
    Status.FAIL: "red",
    Status.INFO: "cyan",
}


def render_report(results: list[CheckResult], elapsed_s: float | None = None) -> None:
    """Full grouped report — used for `financebench doctor` and for setup
    integration when any non-PASS check appears."""
    groups: dict[str, list[CheckResult]] = {}
    for r in results:
        groups.setdefault(r.group, []).append(r)

    console.print()
    console.print("[bold]financebench doctor — environment check[/]")
    if elapsed_s is not None:
        console.print(f"[dim]({elapsed_s:.1f}s)[/]")
    console.print()

    # Stable group ordering — System first, then Resources, Ports, Network, Other
    ordering = ["System", "Resources", "Ports", "Network", "Other"]
    for group_name in ordering + [g for g in groups if g not in ordering]:
        items = groups.get(group_name)
        if not items:
            continue
        console.print(f"[bold]{group_name}[/]")
        for r in items:
            color = _STATUS_STYLE[r.status]
            mark = f"[{color}][{r.status.value}][/]"
            console.print(f"  {mark} [bold]{r.name:<24}[/] {r.summary}")
            if r.fix and r.status in (Status.FAIL, Status.WARN):
                console.print(f"      [dim]→ {r.fix}[/]")
        console.print()

    n_pass = sum(1 for r in results if r.status == Status.PASS)
    n_warn = sum(1 for r in results if r.status == Status.WARN)
    n_fail = sum(1 for r in results if r.status == Status.FAIL)
    n_info = sum(1 for r in results if r.status == Status.INFO)

    parts = []
    if n_pass:
        parts.append(f"[green]{n_pass} passed[/]")
    if n_warn:
        parts.append(f"[yellow]{n_warn} warning{'s' if n_warn != 1 else ''}[/]")
    if n_fail:
        parts.append(f"[red]{n_fail} blocking failure{'s' if n_fail != 1 else ''}[/]")
    if n_info:
        parts.append(f"[dim]{n_info} info[/]")
    console.print(" · ".join(parts))
    console.print()

    if n_fail:
        console.print("[red bold]Setup blocked.[/] Fix the failure(s) above and re-run.")
    elif n_warn:
        console.print(
            "[yellow]Warnings present[/] but setup can proceed. "
            "Re-run `financebench doctor` after addressing to verify."
        )


def render_clean_pass_line(elapsed_s: float) -> None:
    """One-line success — used in setup integration when nothing requires
    attention (no warnings, no failures). Hides the full table so the wizard
    flow stays terse for the happy path."""
    console.print(f"[green][✓][/] Doctor checks passed [dim]({elapsed_s:.1f}s)[/]")


def any_blocking_failed(results: list[CheckResult]) -> bool:
    return any(r.status == Status.FAIL for r in results)


def any_warnings(results: list[CheckResult]) -> bool:
    return any(r.status == Status.WARN for r in results)
