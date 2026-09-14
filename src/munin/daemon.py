"""``munin-rec``: detect, ask, capture. Never transcribes.

The daemon owns the state machine and every timer -- the grace period, the
one-minute warning, the resume window. Detection evidence reaches it either from
the shell plugin (``munin event call-started``) or from ``detect/linux.py``
polling, chosen by ``detection.source``; both produce the same internal event, so
the timers have one implementation.

Suggest, never auto-record (D4): a detected call raises a notification and
nothing else. Only ``start`` begins capture.

Owner: daemon workstream. Contracts: sections 4, 6, 7, 8, 12.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from munin.config import Config

__all__ = ["Daemon", "RuntimeState", "main"]


@dataclass
class RuntimeState:
    """The plugin-facing view, written atomically to ``state.json`` on every
    transition and on daemon start and clean exit. No timer ever writes it:
    ``started_at`` lets the plugin compute elapsed time itself."""

    state: str = "idle"  # idle|detected|recording|ending|captured|transcribing|done|failed
    since: datetime | None = None
    started_at: datetime | None = None
    title: str | None = None
    session: str | None = None
    session_id: str | None = None
    segment: int = 0
    detected_app: dict | None = None
    grace_deadline: datetime | None = None
    queue_depth: int = 0
    last_error: str | None = None
    idle_was_inhibited: bool = False
    daemon_pid: int = 0

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def write(self) -> None:
        """Atomic tmp + rename into ``$XDG_RUNTIME_DIR/munin/state.json``."""
        raise NotImplementedError


class Daemon:
    """The recorder process."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = RuntimeState()

    # --- IPC handlers, one per command in ``ipc.COMMANDS`` ---------------
    def handle_ping(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def handle_status(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def handle_start(self, args: dict[str, Any]) -> dict[str, Any]:
        """``title``, ``resume``, ``from_detection``. Refuses below min_free_mb."""
        raise NotImplementedError

    def handle_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def handle_toggle(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def handle_event(self, args: dict[str, Any]) -> dict[str, Any]:
        """``call-started`` / ``call-ended`` evidence from the plugin or poller."""
        raise NotImplementedError

    def handle_list(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    # --- timers and lifecycle -------------------------------------------
    def tick(self, *, now: datetime | None = None) -> None:
        """One pass of the timers: grace period, warning, resume window, poll."""
        raise NotImplementedError

    def inhibit_idle(self, on: bool) -> None:
        """Hold ``omarchy-toggle-idle stay-awake`` for the recording's duration.

        Records whether stay-awake was already set before touching it and
        restores that prior state on stop, so a user who keeps the machine awake
        permanently does not lose it when a recording ends.
        """
        raise NotImplementedError

    def run(self) -> int:
        """Bind the socket, write ``state.json``, serve until SIGTERM."""
        raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    """``munin-rec`` entry point. Also reached as ``munin daemon``."""
    raise NotImplementedError
