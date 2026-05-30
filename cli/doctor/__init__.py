"""financebench doctor — environment preflight checks.

Read-only diagnostic for the host environment. Catches user-side issues
(missing docker, busy ports, low disk) before the wizard touches anything.

The 0.1.x install-path campaign showed that install-time bugs are a distinct
class from runtime bugs: the boot-banner audit-first protocol catches the
latter, but install-time issues need their own probe. Doctor is that probe.
"""

from __future__ import annotations

import time

from cli.doctor.checks import (
    check_buildkit,
    check_cli_version,
    check_disk_space,
    check_docker_compose_v2,
    check_docker_installed,
    check_docker_running,
    check_git,
    check_platform,
    check_port_free,
    check_url_reachable,
)
from cli.doctor.render import (
    any_blocking_failed,
    any_warnings,
    render_clean_pass_line,
    render_report,
)
from cli.doctor.types import CheckResult, Status, Tier

__all__ = [
    "CheckResult",
    "Status",
    "Tier",
    "run_all_checks",
    "render_report",
    "render_clean_pass_line",
    "any_blocking_failed",
    "any_warnings",
]


# Ports the minimal stack binds to. Only 8000 is BLOCKING (api won't start
# without it); the data-service ports surface as warnings — those usually
# indicate stale containers or a brew-installed local equivalent.
_SERVICE_PORTS = [
    (8000, "api", True),       # blocking
    (6333, "qdrant", False),
    (5432, "postgres", False),
    (6380, "redis", False),
]

_NETWORK_TARGETS = [
    ("PyPI", "https://pypi.org/simple/"),
    ("GitHub", "https://github.com/"),
    ("Docker Hub", "https://registry-1.docker.io/v2/"),
]


def run_all_checks(skip_network: bool = False) -> list[CheckResult]:
    """Run every check in order. Each check times itself."""
    results: list[CheckResult] = []

    def _run(fn, *args, **kwargs):
        t0 = time.monotonic()
        r = fn(*args, **kwargs)
        r.elapsed_ms = int((time.monotonic() - t0) * 1000)
        results.append(r)

    # System
    _run(check_platform)
    _run(check_docker_installed)
    _run(check_docker_running)
    _run(check_docker_compose_v2)
    _run(check_git)
    _run(check_buildkit)

    # Resources
    _run(check_disk_space)

    # Ports
    for port, service, blocking in _SERVICE_PORTS:
        _run(check_port_free, port, service, blocking=blocking)

    # Network (skippable)
    if not skip_network:
        for name, url in _NETWORK_TARGETS:
            _run(check_url_reachable, name, url)
        _run(check_cli_version)

    return results
