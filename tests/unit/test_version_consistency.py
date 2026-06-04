"""Every place the version is hard-coded must match pyproject.toml.

0.3.4 shipped a re-cut because the FastAPI `app.version` was the one of six
version strings missed in the bump; the release verify gate caught it, but only
after a failed publish. This test moves that check to PR time. Pure string
parsing — no heavy imports, runs in the standard unit-test step.

Keep this list in sync with scripts/internal/bump_version.py.
"""
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SEMVER = r"(\d+\.\d+\.\d+)"

# (relative path, regex with one capture group for the version)
VERSION_SITES = [
    ("cli/__init__.py", rf'__version__ = "{SEMVER}"'),
    ("src/api/main.py", rf'version="{SEMVER}"'),          # FastAPI app.version
    ("Dockerfile", rf'version="{SEMVER}"'),               # LABEL version
    ("compose.minimal.yml", rf"FB_IMAGE_TAG:-{SEMVER}"),
    ("docker-compose.yml", rf"FB_IMAGE_TAG:-{SEMVER}"),
]


def _pyproject_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


@pytest.mark.parametrize("rel_path,pattern", VERSION_SITES)
def test_version_matches_pyproject(rel_path, pattern):
    canonical = _pyproject_version()
    text = (ROOT / rel_path).read_text()
    m = re.search(pattern, text)
    assert m, f"no version string matching {pattern!r} found in {rel_path}"
    assert m.group(1) == canonical, (
        f"{rel_path} declares {m.group(1)} but pyproject.toml is {canonical} — "
        f"bump all sites together (scripts/internal/bump_version.py)"
    )
