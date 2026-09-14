"""``munin doctor``: every open question becomes a line of output.

Worth building first (spec 15). It turns "is this set up correctly" into one
command and makes a broken install self-describing six months later. The check
table is per-platform from the start, even while only one column is populated
(spec 16.5).

``doctor`` always drives ``detect/linux.py`` directly, whatever
``detection.source`` says -- that is how a broken plugin is told apart from a
broken detector.

Owner: install workstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from munin.config import Config

__all__ = ["Check", "CheckResult", "run_checks", "main"]

Status = Literal["ok", "warn", "fail", "skip"]


@dataclass(frozen=True)
class CheckResult:
    """One line of ``munin doctor`` output."""

    name: str
    status: Status
    detail: str
    platform: str = "all"


@dataclass(frozen=True)
class Check:
    name: str
    platform: str  # "all" or a sys.platform value
    run: object  # Callable[[Config], CheckResult]


def run_checks(config: Config) -> list[CheckResult]:
    """Tools, Python version, data root, config, disk, PipeWire, detection, plugin,
    unit, backends. Every check returns a line; none raises."""
    raise NotImplementedError


def main(config: Config, *, as_json: bool = False) -> int:
    """Print the table. Exits 5 if any check failed (PoC contracts section 11)."""
    raise NotImplementedError
