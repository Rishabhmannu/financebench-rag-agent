"""`financebench doctor` — environment preflight diagnosis.

Read-only command. Runs the full check battery from `cli.doctor` and renders
a flutter-doctor-style grouped report. Exits 0 on clean / warnings-only,
1 on any blocking failure (so it composes cleanly with shell scripts).
"""

from __future__ import annotations

import time

import typer

from cli.doctor import any_blocking_failed, render_report, run_all_checks


def doctor(
    skip_network: bool = typer.Option(
        False,
        "--skip-network",
        help="Skip PyPI / GitHub / Docker Hub reachability checks (offline mode).",
    ),
) -> None:
    """Diagnose the environment for install / runtime issues.

    Read-only. Reports system, resource, port, and network status without
    touching anything. Exits non-zero on blocking failures.
    """
    t0 = time.monotonic()
    results = run_all_checks(skip_network=skip_network)
    elapsed = time.monotonic() - t0
    render_report(results, elapsed_s=elapsed)
    if any_blocking_failed(results):
        raise typer.Exit(1)
    # 0.1.8: when standalone doctor finishes clean (or warnings-only), point
    # the user at the next step. When called from setup (via _run_doctor in
    # cli/commands/setup.py), this hint is skipped — setup continues its own
    # flow, this command isn't on the path.
    from cli.render import console
    console.print(
        "[dim]Next: financebench setup   (bring up the stack — wizard handles env, build, seed, verify)[/dim]"
    )
