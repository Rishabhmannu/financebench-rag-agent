#!/usr/bin/env bash
# smoke_test_wheel.sh — verify dist/*.whl actually installs + the CLI imports
#                       cleanly in a fresh virtualenv. Catches the entire class
#                       of "package builds but pip install gives a broken CLI"
#                       bugs that hit 0.1.0 (httpx-sse + prompt_toolkit gated
#                       behind a [cli] extra that pip didn't auto-pull).
#
# Run BEFORE every `twine upload` so packaging bugs don't reach PyPI.
#
# Exits non-zero on any failure with a clear error message.

set -uo pipefail

WHEEL=$(ls dist/financebench_rag_agent-*.whl 2>/dev/null | head -1)
if [ -z "${WHEEL}" ]; then
  echo "FAIL: no wheel found in dist/ — run 'python -m build' first" >&2
  exit 1
fi

# Use a Python in the supported range (3.11-3.13 per pyproject.toml). System
# python3 might be too new (e.g. 3.14 on bleeding-edge macOS installs). Prefer
# whichever supported version is available — CI uses 3.12 explicitly.
PYTHON_BIN=""
for candidate in python3.12 python3.13 python3.11; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    PYTHON_BIN=$(command -v "${candidate}")
    break
  fi
done
# Also try conda's agentic-ai env (matches local dev setup)
if [ -z "${PYTHON_BIN}" ] && [ -x "/opt/anaconda3/envs/agentic-ai/bin/python" ]; then
  PYTHON_BIN="/opt/anaconda3/envs/agentic-ai/bin/python"
fi
if [ -z "${PYTHON_BIN}" ]; then
  echo "FAIL: no python in [3.11-3.13] found on PATH. Install one and retry." >&2
  exit 1
fi
echo "[smoke] using python: ${PYTHON_BIN} ($("${PYTHON_BIN}" --version 2>&1))"

VENV_DIR="/tmp/fb-smoke-$(date +%s)"
echo "[smoke] using fresh venv: ${VENV_DIR}"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

# Pip upgrade so the dep resolver matches what fresh-machine users see
pip install --quiet --upgrade pip >/dev/null

echo "[smoke] installing ${WHEEL}"
if ! pip install --quiet "${WHEEL}" >/tmp/fb-smoke-pip.log 2>&1; then
  echo "FAIL: pip install of ${WHEEL} returned non-zero" >&2
  tail -30 /tmp/fb-smoke-pip.log >&2
  deactivate
  rm -rf "${VENV_DIR}"
  exit 1
fi

echo "[smoke] running 'financebench --help'"
if ! "${VENV_DIR}/bin/financebench" --help >/tmp/fb-smoke-help.log 2>&1; then
  echo "FAIL: 'financebench --help' broke at import time" >&2
  tail -30 /tmp/fb-smoke-help.log >&2
  deactivate
  rm -rf "${VENV_DIR}"
  exit 1
fi

echo "[smoke] running 'financebench version'"
VERSION=$("${VENV_DIR}/bin/financebench" version 2>&1)
if [ -z "${VERSION}" ]; then
  echo "FAIL: 'financebench version' returned no output" >&2
  deactivate
  rm -rf "${VENV_DIR}"
  exit 1
fi
echo "[smoke] CLI version: ${VERSION}"

# Probe a few subcommand --help outputs — verifies each commands/ module
# imports cleanly. Catches the same class of "[cli] extras missing" bugs
# from 0.1.0 where the top-level import worked but specific subcommand
# imports later broke.
for cmd in setup login chat threads approvals logout status upgrade down; do
  if ! "${VENV_DIR}/bin/financebench" "${cmd}" --help >/tmp/fb-smoke-${cmd}.log 2>&1; then
    echo "FAIL: 'financebench ${cmd} --help' broke at import time" >&2
    tail -20 /tmp/fb-smoke-${cmd}.log >&2
    deactivate
    rm -rf "${VENV_DIR}"
    exit 1
  fi
done

# Verify the install footprint isn't pulling backend bloat unintentionally
SIZE_KB=$(du -sk "${VENV_DIR}/lib"/python*/site-packages 2>/dev/null | cut -f1)
SIZE_MB=$((SIZE_KB / 1024))
echo "[smoke] site-packages size: ${SIZE_MB} MB"
if [ "${SIZE_MB}" -gt 200 ]; then
  echo "WARN: site-packages is ${SIZE_MB} MB — pre-0.1.1 the bloat target was 50 MB." >&2
  echo "WARN: check whether [backend] extras leaked into main dependencies." >&2
fi

# Cleanup
deactivate
rm -rf "${VENV_DIR}"

echo "[smoke] PASS — fresh-venv install works, all subcommands importable, size ${SIZE_MB} MB"
exit 0
