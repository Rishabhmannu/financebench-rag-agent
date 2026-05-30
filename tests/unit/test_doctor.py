"""Unit tests for `cli.doctor`. Each check is mocked at its external boundary
(`shutil.which`, `subprocess.check_output`, `socket.bind`, `httpx.head`,
`httpx.get`) so the suite runs without docker / network / a populated env."""

from __future__ import annotations

import json
import socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from cli.doctor import checks
from cli.doctor.types import Status, Tier


# --- System checks ---------------------------------------------------------

def test_platform_returns_info():
    r = checks.check_platform()
    assert r.status == Status.INFO
    assert r.group == "System"
    assert r.summary  # non-empty


@patch("cli.doctor.checks.shutil.which", return_value=None)
def test_docker_not_installed(_which):
    r = checks.check_docker_installed()
    assert r.status == Status.FAIL
    assert r.tier == Tier.BLOCKING
    assert r.fix and "docker.com" in r.fix


@patch("cli.doctor.checks.subprocess.check_output", return_value="Docker version 28.3.4")
@patch("cli.doctor.checks.shutil.which", return_value="/usr/local/bin/docker")
def test_docker_installed(_which, _check_output):
    r = checks.check_docker_installed()
    assert r.status == Status.PASS
    assert "28.3.4" in r.summary


@patch("cli.doctor.checks.subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "docker"))
def test_docker_daemon_down(_check_output):
    r = checks.check_docker_running()
    assert r.status == Status.FAIL
    assert "Start Docker Desktop" in (r.fix or "")


@patch("cli.doctor.checks.subprocess.check_output", return_value="ok")
def test_docker_daemon_running(_check_output):
    r = checks.check_docker_running()
    assert r.status == Status.PASS


@patch("cli.doctor.checks.subprocess.check_output", return_value="Docker Compose version v2.34.0")
def test_docker_compose_v2_present(_check_output):
    r = checks.check_docker_compose_v2()
    assert r.status == Status.PASS
    assert "v2.34.0" in r.summary


@patch("cli.doctor.checks.subprocess.check_output", side_effect=FileNotFoundError)
def test_docker_compose_v2_missing(_check_output):
    r = checks.check_docker_compose_v2()
    assert r.status == Status.FAIL


@patch("cli.doctor.checks.shutil.which", return_value=None)
def test_git_missing(_which):
    r = checks.check_git()
    assert r.status == Status.FAIL
    assert r.fix and "xcode-select" in r.fix


# --- Resources -------------------------------------------------------------

def test_disk_space_pass(tmp_path, monkeypatch):
    # Mock shutil.disk_usage to return >= 6 GB free
    fake = MagicMock(free=10 * (1024 ** 3), total=100 * (1024 ** 3), used=90 * (1024 ** 3))
    monkeypatch.setattr(checks.shutil, "disk_usage", lambda _: fake)
    r = checks.check_disk_space(min_free_gb=6.0)
    assert r.status == Status.PASS


def test_disk_space_fail(monkeypatch):
    fake = MagicMock(free=2 * (1024 ** 3), total=100 * (1024 ** 3), used=98 * (1024 ** 3))
    monkeypatch.setattr(checks.shutil, "disk_usage", lambda _: fake)
    r = checks.check_disk_space(min_free_gb=6.0)
    assert r.status == Status.FAIL
    assert r.tier == Tier.BLOCKING
    assert "2.0 GB" in r.summary


# --- Ports -----------------------------------------------------------------

def test_port_free():
    # Use a high random port that's almost certainly free
    r = checks.check_port_free(54323, "test_service", blocking=False)
    assert r.status == Status.PASS
    assert r.tier == Tier.WARNING  # non-blocking by default


def test_port_blocked_by_stranger():
    """Port held by something that isn't our compose stack → FAIL."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        # Force "not our stack" so we hit the FAIL path
        with patch("cli.doctor.checks._find_own_stack_container", return_value=None):
            r = checks.check_port_free(port, "test_service", blocking=True)
        assert r.status == Status.FAIL
        assert r.tier == Tier.BLOCKING
        assert "In use" in r.summary
    finally:
        s.close()


def test_port_blocked_by_own_stack_reports_pass():
    """Port held by our own running compose container → PASS, not FAIL.

    0.1.7 regression fix: pre-0.1.7, doctor would lsof the port, find Docker
    Desktop's backend PID, and recommend `kill <docker pid>` — which would
    kill Docker Desktop itself.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        with patch(
            "cli.doctor.checks._find_own_stack_container", return_value="repo-api-1"
        ):
            r = checks.check_port_free(port, "api", blocking=True)
        assert r.status == Status.PASS
        assert "repo-api-1" in r.summary
        assert "your running stack" in r.summary
        assert r.fix is None  # no "kill" recipe for our own stack
    finally:
        s.close()


def test_find_own_stack_container_matches_compose_service_pattern():
    """`docker ps` output with our compose-generated names → matched."""
    fake_output = (
        "repo-api-1\t0.0.0.0:8000->8000/tcp\n"
        "repo-qdrant-1\t0.0.0.0:6333->6333/tcp, 0.0.0.0:6334->6334/tcp\n"
        "repo-postgres-1\t0.0.0.0:5432->5432/tcp\n"
        "other-app-1\t0.0.0.0:9999->9999/tcp\n"
    )
    with (
        patch("cli.doctor.checks.shutil.which", return_value="/usr/local/bin/docker"),
        patch("cli.doctor.checks.subprocess.check_output", return_value=fake_output),
    ):
        assert checks._find_own_stack_container(8000) == "repo-api-1"
        assert checks._find_own_stack_container(6333) == "repo-qdrant-1"
        assert checks._find_own_stack_container(5432) == "repo-postgres-1"
        # Port 9999 is held by 'other-app-1' — doesn't match our service suffixes
        assert checks._find_own_stack_container(9999) is None
        # Port not held by anyone in the output
        assert checks._find_own_stack_container(7777) is None


def test_find_own_stack_container_handles_port_range_format():
    """Docker collapses adjacent published ports into a range — the actual
    format M1 test9 produced for qdrant. Pre-0.2.0 our substring matcher
    missed `:6333->` because the string was `:6333-6334->`."""
    fake_output = (
        "repo-qdrant-1\t0.0.0.0:6333-6334->6333-6334/tcp, [::]:6333-6334->6333-6334/tcp\n"
    )
    with (
        patch("cli.doctor.checks.shutil.which", return_value="/usr/local/bin/docker"),
        patch("cli.doctor.checks.subprocess.check_output", return_value=fake_output),
    ):
        assert checks._find_own_stack_container(6333) == "repo-qdrant-1"
        assert checks._find_own_stack_container(6334) == "repo-qdrant-1"
        # Port 6335 isn't in the range
        assert checks._find_own_stack_container(6335) is None


def test_published_ports_parses_common_formats():
    """Direct unit test of the helper for clarity on what each input maps to."""
    assert checks._published_ports("0.0.0.0:8000->8000/tcp") == {8000}
    assert checks._published_ports(
        "0.0.0.0:6333-6334->6333-6334/tcp, [::]:6333-6334->6333-6334/tcp"
    ) == {6333, 6334}
    assert checks._published_ports("0.0.0.0:5432->5432/tcp, [::]:5432->5432/tcp") == {5432}
    # Unpublished port — no `->` in entry
    assert checks._published_ports("6333/tcp") == set()
    # Empty input
    assert checks._published_ports("") == set()
    # Malformed entry shouldn't crash
    assert checks._published_ports("garbage->somewhere") == set()


def test_find_own_stack_container_handles_no_docker():
    """If docker isn't on PATH, helper returns None — no crash."""
    with patch("cli.doctor.checks.shutil.which", return_value=None):
        assert checks._find_own_stack_container(8000) is None


def test_find_own_stack_container_handles_docker_error():
    """If `docker ps` errors, helper returns None — no crash."""
    with (
        patch("cli.doctor.checks.shutil.which", return_value="/usr/local/bin/docker"),
        patch("cli.doctor.checks.subprocess.check_output", side_effect=Exception("docker down")),
    ):
        assert checks._find_own_stack_container(8000) is None


# --- Network ---------------------------------------------------------------

def test_url_reachable_pass():
    fake_response = MagicMock(status_code=200)
    with patch("httpx.head", return_value=fake_response):
        r = checks.check_url_reachable("TestSite", "https://example.com")
    assert r.status == Status.PASS


def test_url_reachable_fail():
    with patch("httpx.head", side_effect=Exception("timeout")):
        r = checks.check_url_reachable("TestSite", "https://example.com")
    assert r.status == Status.WARN
    assert r.fix


def test_cli_version_uses_cache_when_fresh(tmp_path):
    cache = tmp_path / "version_check.json"
    import time as _time
    cache.write_text(json.dumps({"timestamp": _time.time(), "latest": "99.99.99"}))
    with patch("httpx.get") as mock_get:
        r = checks.check_cli_version(cache_file=cache)
        mock_get.assert_not_called()
    # current CLI version is < 99.99.99, so should WARN about being outdated
    assert r.status == Status.WARN
    assert "99.99.99" in r.summary


def test_cli_version_fetches_when_cache_stale(tmp_path, monkeypatch):
    cache = tmp_path / "version_check.json"
    fake_response = MagicMock(status_code=200)
    from cli import __version__ as current
    fake_response.json.return_value = {"info": {"version": current}}
    with patch("httpx.get", return_value=fake_response):
        r = checks.check_cli_version(cache_file=cache)
    assert r.status == Status.INFO
    assert "latest" in r.summary
    # Verify cache got written
    assert cache.exists()


def test_cli_version_handles_pypi_unreachable(tmp_path):
    cache = tmp_path / "version_check.json"
    with patch("httpx.get", side_effect=Exception("dns failure")):
        r = checks.check_cli_version(cache_file=cache)
    assert r.status == Status.INFO
    assert "couldn't reach PyPI" in r.summary


# --- run_all_checks integration -------------------------------------------

def test_run_all_checks_skip_network_excludes_network_targets():
    from cli.doctor import run_all_checks
    # Skip network so we don't make real http calls in this test
    results = run_all_checks(skip_network=True)
    names = [r.name for r in results]
    assert "PyPI" not in names
    assert "GitHub" not in names
    assert "Docker Hub" not in names
    assert "CLI version" not in names
    # System checks should still be present
    assert "Platform" in names


def test_run_all_checks_records_elapsed_per_check():
    from cli.doctor import run_all_checks
    results = run_all_checks(skip_network=True)
    # Every result should have a non-negative elapsed_ms set by the wrapper
    for r in results:
        assert r.elapsed_ms >= 0
