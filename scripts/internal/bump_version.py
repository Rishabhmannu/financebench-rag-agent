"""Bump the project version across every hard-coded site at once.

Six places carry the version: pyproject.toml, cli/__init__.py, the FastAPI
app.version in src/api/main.py, the Dockerfile LABEL, and the FB_IMAGE_TAG
default in both compose files. Bumping them by hand is how 0.3.4 shipped with a
mismatched app.version. This updates all of them and verifies the result.

    python scripts/internal/bump_version.py 0.3.5          # apply
    python scripts/internal/bump_version.py 0.3.5 --check  # dry-run (print diffs)

Kept in sync with tests/unit/test_version_consistency.py.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEMVER = r"\d+\.\d+\.\d+"

# (relative path, regex matching the version with the literal split out so we
# can substitute just the number). Each pattern has exactly one capture group
# wrapping the old version.
SITES = [
    ("pyproject.toml", rf'(?m)^(version = ")({SEMVER})(")'),
    ("cli/__init__.py", rf'(__version__ = ")({SEMVER})(")'),
    ("src/api/main.py", rf'(version=")({SEMVER})(")'),
    ("Dockerfile", rf'(version=")({SEMVER})(")'),
    ("compose.minimal.yml", rf"(FB_IMAGE_TAG:-)({SEMVER})(\}})"),
    ("docker-compose.yml", rf"(FB_IMAGE_TAG:-)({SEMVER})(\}})"),
]


def current_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("version", help="new version, e.g. 0.3.5")
    ap.add_argument("--check", action="store_true", help="dry-run; print what would change")
    args = ap.parse_args()

    if not re.fullmatch(SEMVER, args.version):
        print(f"error: {args.version!r} is not a valid X.Y.Z version", file=sys.stderr)
        return 2

    old = current_version()
    print(f"current: {old}  ->  new: {args.version}\n")

    failures = []
    for rel, pattern in SITES:
        path = ROOT / rel
        text = path.read_text()
        new_text, n = re.subn(pattern, lambda m: m.group(1) + args.version + m.group(3), text)
        if n == 0:
            failures.append(f"{rel}: no version string matched {pattern!r}")
            continue
        print(f"  {rel}: {n} occurrence(s)")
        if not args.check:
            path.write_text(new_text)

    if failures:
        print("\nERROR — some sites did not match (the test would fail):", file=sys.stderr)
        for f in failures:
            print("  " + f, file=sys.stderr)
        return 1

    print("\n" + ("dry-run only, nothing written (--check)" if args.check else "done."))
    print("Next: run `pytest tests/unit/test_version_consistency.py`, commit, then tag.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
