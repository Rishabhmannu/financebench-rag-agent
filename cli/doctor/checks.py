"""Individual doctor checks. Each function returns a CheckResult.

Each check is small, side-effect-free (read-only), and self-contained so it
can be unit-tested in isolation by mocking the dependency (`shutil.which`,
`socket.bind`, `httpx.head`, etc.).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

from cli.doctor.types import CheckResult, Status, Tier

# Path the wizard writes to (so doctor can re-use the dir for cache files).
_FINANCEBENCH_DIR = Path.home() / ".financebench"
_VERSION_CACHE_FILE = _FINANCEBENCH_DIR / "version_check.json"
_VERSION_CACHE_TTL_S = 3600

# Disk threshold (chosen after the 0.1.x install-path cycle).
MIN_FREE_DISK_GB = 6.0


# --- System ---------------------------------------------------------------

def check_platform() -> CheckResult:
    machine = platform.machine()
    system = platform.system()
    release = platform.release()
    note = ""
    if machine in ("arm64", "aarch64") and system == "Darwin":
        note = " — BGE on CPU (~30s first warm, ~500ms after)"
    elif machine in ("arm64", "aarch64") and system == "Linux":
        note = " — ARM64 Linux; should work, less tested than macOS"
    return CheckResult(
        name="Platform",
        status=Status.INFO,
        tier=Tier.INFO,
        summary=f"{machine} · {system} {release}{note}",
        group="System",
    )


def check_docker_installed() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult(
            name="Docker",
            status=Status.FAIL,
            tier=Tier.BLOCKING,
            summary="Not installed or not on PATH",
            fix="Install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/",
            group="System",
        )
    try:
        out = subprocess.check_output(
            ["docker", "--version"], text=True, stderr=subprocess.DEVNULL, timeout=3
        ).strip()
    except Exception:  # noqa: BLE001
        out = "installed"
    return CheckResult(
        name="Docker",
        status=Status.PASS,
        tier=Tier.BLOCKING,
        summary=out,
        group="System",
    )


def check_docker_running() -> CheckResult:
    try:
        subprocess.check_output(
            ["docker", "info"], text=True, stderr=subprocess.DEVNULL, timeout=4
        )
        return CheckResult(
            name="Docker daemon",
            status=Status.PASS,
            tier=Tier.BLOCKING,
            summary="Running",
            group="System",
        )
    except Exception:  # noqa: BLE001
        return CheckResult(
            name="Docker daemon",
            status=Status.FAIL,
            tier=Tier.BLOCKING,
            summary="Not running",
            fix="Start Docker Desktop and wait for the whale icon to stop animating",
            group="System",
        )


def check_docker_compose_v2() -> CheckResult:
    try:
        out = subprocess.check_output(
            ["docker", "compose", "version"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip()
        first = out.split("\n", 1)[0]
        return CheckResult(
            name="Docker Compose",
            status=Status.PASS,
            tier=Tier.BLOCKING,
            summary=first,
            group="System",
        )
    except Exception:  # noqa: BLE001
        return CheckResult(
            name="Docker Compose",
            status=Status.FAIL,
            tier=Tier.BLOCKING,
            summary="v2 not available",
            fix="Update Docker Desktop; the standalone v1 `docker-compose` is unsupported",
            group="System",
        )


def check_git() -> CheckResult:
    if not shutil.which("git"):
        return CheckResult(
            name="Git",
            status=Status.FAIL,
            tier=Tier.BLOCKING,
            summary="Not installed",
            fix="macOS: `xcode-select --install`   ·   Linux: `apt install git`",
            group="System",
        )
    try:
        out = subprocess.check_output(
            ["git", "--version"], text=True, stderr=subprocess.DEVNULL, timeout=2
        ).strip()
    except Exception:  # noqa: BLE001
        out = "installed"
    return CheckResult(
        name="Git",
        status=Status.PASS,
        tier=Tier.BLOCKING,
        summary=out,
        group="System",
    )


def check_buildkit() -> CheckResult:
    has_buildx = False
    try:
        subprocess.check_output(
            ["docker", "buildx", "version"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        has_buildx = True
    except Exception:  # noqa: BLE001
        pass

    env_set = os.environ.get("DOCKER_BUILDKIT") == "1"

    # 0.1.8: modern Docker Desktop enables Buildkit by default (no env var
    # needed). Previously we recommended `export DOCKER_BUILDKIT=1` when the
    # env wasn't set, which was confusing for users on Docker Desktop 23+
    # where Buildkit is the default. Now we only flag if buildx is actually
    # missing — the env var nudge is gone.
    if has_buildx:
        return CheckResult(
            name="Docker Buildkit",
            status=Status.INFO,
            tier=Tier.INFO,
            summary="Available",
            group="System",
        )
    return CheckResult(
        name="Docker Buildkit",
        status=Status.INFO,
        tier=Tier.INFO,
        summary="Unavailable (Docker Desktop 23+ should have it; older docker may need manual install)",
        group="System",
    )


# --- Resources ------------------------------------------------------------

def check_disk_space(min_free_gb: float = MIN_FREE_DISK_GB) -> CheckResult:
    home = Path.home()
    usage = shutil.disk_usage(home)
    free_gb = usage.free / (1024 ** 3)
    if free_gb >= min_free_gb:
        return CheckResult(
            name="Disk space",
            status=Status.PASS,
            tier=Tier.BLOCKING,
            summary=f"{free_gb:.1f} GB free in $HOME",
            group="Resources",
        )
    return CheckResult(
        name="Disk space",
        status=Status.FAIL,
        tier=Tier.BLOCKING,
        summary=f"{free_gb:.1f} GB free in $HOME (need ≥ {min_free_gb:.0f} GB)",
        fix="Free up disk space; image build needs ~3-4 GB plus volumes ~1 GB",
        group="Resources",
    )


# 0.1.7: RAM check removed. The previous psutil.virtual_memory().available
# reading was too pessimistic on macOS (the OS aggressively caches in RAM and
# reports `available` low even when memory pressure is fine, plus it pages to
# SSD-backed swap under pressure). On 16 GB Apple Silicon, "3.2 GB available"
# triggered a WARN that was almost always false — the system had plenty of
# headroom. Removing the check entirely is cleaner than trying to engineer a
# threshold + platform-specific swap logic for what's effectively a non-issue
# in practice. If we ever see a real OOM regression, a cross-platform RAM
# check can come back via vm_stat parsing on Darwin and meminfo on Linux.


# --- Ports ----------------------------------------------------------------

# Container-name suffixes that docker compose generates for our four services
# (project_name + "-" + service + "-1"). Used by _find_own_stack_container
# to recognize "this port is held by MY stack, not a stranger".
_OWN_SERVICE_SUFFIXES = ("-api-1", "-qdrant-1", "-postgres-1", "-redis-1")


def _published_ports(ports_str: str) -> set[int]:
    """Parse the `docker ps {{.Ports}}` column into the set of published host
    ports.

    The column can contain single ports, ranges, IPv6 + IPv4 dual entries,
    and unpublished ports. Handle each:

      "0.0.0.0:6333->6333/tcp"                                    -> {6333}
      "0.0.0.0:6333-6334->6333-6334/tcp"                          -> {6333, 6334}
      "0.0.0.0:8000->8000/tcp, [::]:8000->8000/tcp"               -> {8000}
      "6333/tcp"                                                  -> set() (not published)
      ""                                                          -> set()

    0.1.8 doctor used `f":{port}->" in ports_str` substring matching, which
    silently misses range-published ports like qdrant's `6333-6334`. Test9
    docker ps confirmed: `repo-qdrant-1  0.0.0.0:6333-6334->6333-6334/tcp`
    — `:6333->` doesn't match `:6333-6334->`. This proper parse fixes it.
    """
    out: set[int] = set()
    for entry in ports_str.split(","):
        entry = entry.strip()
        if "->" not in entry:
            continue  # unpublished port
        left = entry.split("->", 1)[0]
        if ":" not in left:
            continue
        spec = left.rsplit(":", 1)[1].strip()
        if not spec:
            continue
        if "-" in spec:
            try:
                lo, hi = spec.split("-", 1)
                out.update(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(spec))
            except ValueError:
                continue
    return out


def _find_own_stack_container(port: int) -> str | None:
    """Return the container name (e.g. 'repo-api-1') if `port` is published by
    one of our compose stack containers. Otherwise None.

    Pre-0.1.7, when the user's own stack was running, doctor would lsof the
    port, find that Docker Desktop's backend (com.docker.backend) held it,
    and report "kill <pid>" as the fix — which would terminate Docker
    Desktop and break the user's stack. Now we check `docker ps` first and
    distinguish "ours, fine" from "someone else's, problem".
    """
    if not shutil.which("docker"):
        return None
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except Exception:  # noqa: BLE001
        return None

    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, ports = line.split("\t", 1)
        if not any(name.endswith(suffix) for suffix in _OWN_SERVICE_SUFFIXES):
            continue
        if port in _published_ports(ports):
            return name
    return None


def _lsof_owner(port: int) -> str | None:
    """Find the PID + command name holding `port`, or None if unknowable.

    Uses `lsof -ti :PORT` to enumerate PIDs, then `ps -p PID -o comm=` for the
    process name. Both are macOS + Linux compatible.
    """
    if not shutil.which("lsof"):
        return None
    try:
        pids_out = subprocess.check_output(
            ["lsof", "-ti", f":{port}"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        if not pids_out:
            return None
        pid = pids_out.splitlines()[0]
        try:
            cmd = subprocess.check_output(
                ["ps", "-p", pid, "-o", "comm="],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).strip()
            return f"PID {pid} ({cmd})"
        except Exception:  # noqa: BLE001
            return f"PID {pid}"
    except Exception:  # noqa: BLE001
        return None


def check_port_free(port: int, service: str, blocking: bool = False) -> CheckResult:
    """Try to bind 127.0.0.1:port. If busy, check if our own stack holds it
    before reporting a conflict."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.close()
        return CheckResult(
            name=f"Port {port} ({service})",
            status=Status.PASS,
            tier=Tier.BLOCKING if blocking else Tier.WARNING,
            summary="free",
            group="Ports",
        )
    except OSError:
        sock.close()
        # 0.1.7: distinguish "our own running stack" from "real conflict". Our
        # wizard's `up -d --build` is idempotent — re-running with our stack
        # already up is fine. Pre-0.1.7, doctor would lsof the port, find
        # Docker Desktop's backend PID, and recommend killing it (which would
        # take Docker Desktop down). Now: docker ps lookup first.
        own_container = _find_own_stack_container(port)
        if own_container is not None:
            return CheckResult(
                name=f"Port {port} ({service})",
                status=Status.PASS,
                tier=Tier.BLOCKING if blocking else Tier.WARNING,
                summary=f"in use by {own_container} (your running stack)",
                group="Ports",
            )
        owner = _lsof_owner(port) or "unknown process"
        pid_hint = owner.split("PID ")[-1].split(" ")[0] if "PID" in owner else "<pid>"
        return CheckResult(
            name=f"Port {port} ({service})",
            status=Status.FAIL if blocking else Status.WARN,
            tier=Tier.BLOCKING if blocking else Tier.WARNING,
            summary=f"In use by {owner}",
            fix=f"kill {pid_hint}  OR  override the {service} port in compose.minimal.yml",
            group="Ports",
        )


# --- Network --------------------------------------------------------------

def check_url_reachable(name: str, url: str, timeout: float = 2.0) -> CheckResult:
    """HEAD request to `url`. Anything <500 counts as reachable."""
    import httpx  # noqa: PLC0415
    try:
        r = httpx.head(url, timeout=timeout, follow_redirects=True)
        if r.status_code < 500:
            return CheckResult(
                name=name,
                status=Status.PASS,
                tier=Tier.WARNING,
                summary="Reachable",
                group="Network",
            )
    except Exception:  # noqa: BLE001
        pass
    return CheckResult(
        name=name,
        status=Status.WARN,
        tier=Tier.WARNING,
        summary="Unreachable",
        fix=f"Check internet / corporate proxy / VPN. Wizard needs {name} reachable.",
        group="Network",
    )


def check_api_key(key_name: str, required: bool, env_path: Path | None = None) -> CheckResult:
    """Live Layer 2 probe of an API key against its provider.

    0.2.3: reads the key from .env (or os.environ if .env not found), runs
    the matching probe from cli.key_probe. Catches revoked / wrong-account /
    expired keys that pass the wizard's prefix check at install time.
    """
    from cli.key_probe import PROBES, ProbeStatus  # noqa: PLC0415

    # Read from .env first (wizard writes there), fall back to live env.
    value = ""
    if env_path and env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith(f"{key_name}=") and not line.startswith("#"):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass
    if not value:
        value = os.environ.get(key_name, "")

    tier = Tier.BLOCKING if required else Tier.INFO

    if not value:
        if required:
            return CheckResult(
                name=key_name,
                status=Status.FAIL,
                tier=tier,
                summary="Not set",
                fix="Run `financebench setup` and provide the key when prompted.",
                group="API keys",
            )
        return CheckResult(
            name=key_name,
            status=Status.INFO,
            tier=Tier.INFO,
            summary="Not set (optional)",
            group="API keys",
        )

    probe = PROBES.get(key_name)
    if probe is None:
        return CheckResult(
            name=key_name,
            status=Status.INFO,
            tier=Tier.INFO,
            summary="Set (no probe configured)",
            group="API keys",
        )

    result = probe(value)
    if result.status is ProbeStatus.OK:
        return CheckResult(
            name=key_name,
            status=Status.PASS,
            tier=Tier.WARNING if required else Tier.INFO,
            summary=f"Live probe: accepted by provider (•••{value[-4:]})",
            group="API keys",
        )
    if result.status is ProbeStatus.BAD_KEY:
        return CheckResult(
            name=key_name,
            status=Status.FAIL if required else Status.WARN,
            tier=tier,
            summary=f"Live probe: {result.message}",
            fix="Re-run `financebench setup` with a fresh key from the linked dashboard.",
            group="API keys",
        )
    # NETWORK_ERROR
    return CheckResult(
        name=key_name,
        status=Status.WARN,
        tier=Tier.WARNING,
        summary=f"Live probe skipped: {result.message}",
        fix="Re-run doctor when network is available. Key format may still be valid.",
        group="API keys",
    )


def check_cli_version(cache_file: Path = _VERSION_CACHE_FILE) -> CheckResult:
    """Compare installed CLI version to latest on PyPI. Caches result 1 hour."""
    import httpx  # noqa: PLC0415

    from cli import __version__ as current

    # Cache lookup
    latest = None
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if time.time() - float(cached.get("timestamp", 0)) < _VERSION_CACHE_TTL_S:
                latest = cached.get("latest")
        except Exception:  # noqa: BLE001
            pass

    if latest is None:
        try:
            r = httpx.get(
                "https://pypi.org/pypi/financebench-rag-agent/json",
                timeout=2.0,
            )
            if r.status_code == 200:
                latest = r.json().get("info", {}).get("version")
                if latest:
                    try:
                        cache_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        cache_file.write_text(
                            json.dumps({"timestamp": time.time(), "latest": latest})
                        )
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass

    if latest is None:
        return CheckResult(
            name="CLI version",
            status=Status.INFO,
            tier=Tier.INFO,
            summary=f"{current} (couldn't reach PyPI)",
            group="Network",
        )
    if current == latest:
        return CheckResult(
            name="CLI version",
            status=Status.INFO,
            tier=Tier.INFO,
            summary=f"{current} (latest)",
            group="Network",
        )
    return CheckResult(
        name="CLI version",
        status=Status.WARN,
        tier=Tier.WARNING,
        summary=f"{current} (latest is {latest})",
        fix="pip install --upgrade financebench-rag-agent",
        group="Network",
    )
