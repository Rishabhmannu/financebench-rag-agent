from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(Enum):
    PASS = "✓"
    WARN = "!"
    FAIL = "✗"
    # 0.1.6: `i` would be rendered by rich as the italic tag — `[i]name[/]`
    # would italicize "name" instead of printing the literal marker. Use the
    # middle-dot character (matches the footer separator in cli/render.py).
    INFO = "·"


class Tier(Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


@dataclass
class CheckResult:
    name: str
    status: Status
    tier: Tier
    summary: str
    fix: str | None = None
    group: str = "Other"
    elapsed_ms: int = 0
    metadata: dict = field(default_factory=dict)
