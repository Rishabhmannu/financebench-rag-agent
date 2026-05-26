"""JWT + base-URL storage at ~/.financebench/credentials.json (0600)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

CREDS_DIR = Path.home() / ".financebench"
CREDS_FILE = CREDS_DIR / "credentials.json"


def save(token: str, user_id: str, base_url: str) -> None:
    CREDS_DIR.mkdir(mode=0o700, exist_ok=True)
    payload = {"token": token, "user_id": user_id, "base_url": base_url}
    CREDS_FILE.write_text(json.dumps(payload))
    os.chmod(CREDS_FILE, stat.S_IRUSR | stat.S_IWUSR)


def load() -> dict | None:
    if not CREDS_FILE.exists():
        return None
    try:
        return json.loads(CREDS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear() -> bool:
    if CREDS_FILE.exists():
        CREDS_FILE.unlink()
        return True
    return False
