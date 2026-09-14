"""Idle inhibition on Omarchy.

Omarchy's idle service gates on a state file, and ``omarchy-toggle-idle``
flips it. Reading the file rather than asking the command is deliberate: the
command is a toggle with no "report" mode, so the only way to learn the current
state without changing it is to look at what it writes.

The script name is ``omarchy-``-prefixed because it is Omarchy's, not ours --
D12's "always ``munin-``" rule governs scripts Munin ships, not ones it calls.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from munin.desktop.base import IdleInhibitor

__all__ = ["OmarchyIdleInhibitor", "STAY_AWAKE_STATE", "TOGGLE_IDLE"]

log = logging.getLogger("munin.desktop")

#: Relative to ``$XDG_STATE_HOME``; Omarchy's idle service gates on it.
STAY_AWAKE_STATE = "omarchy/indicators/stay-awake"

TOGGLE_IDLE = "omarchy-toggle-idle"


class OmarchyIdleInhibitor(IdleInhibitor):
    """Holds ``omarchy-toggle-idle stay-awake`` for a recording's duration."""

    method = "omarchy-stay-awake"

    def state_file(self) -> Path:
        state_home = os.environ.get("XDG_STATE_HOME") or str(
            Path.home() / ".local" / "state"
        )
        return Path(state_home) / STAY_AWAKE_STATE

    def is_inhibited(self) -> bool:
        return self.state_file().exists()

    def inhibit(self) -> None:
        self._toggle("stay-awake")

    def release(self) -> None:
        self._toggle("allow-idle")

    def _toggle(self, mode: str) -> None:
        try:
            code = self.runner([TOGGLE_IDLE, mode])
        except Exception as exc:  # noqa: BLE001 - never take a recording down
            log.warning("idle toggle failed mode=%s error=%s", mode, exc)
            return
        if code != 0:
            log.warning("idle toggle exited %d mode=%s", code, mode)

    def describe(self) -> dict[str, str]:
        return {
            "method": self.method,
            "command": TOGGLE_IDLE,
            "state_file": str(self.state_file()),
            "held": "yes" if self.is_inhibited() else "no",
        }
