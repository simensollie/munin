"""Desktop notifications, through ``omarchy-notification-send``.

The wrapper supports exactly one action, ``--exec <program> [args...]``. The
underlying daemon models more, but the PoC does not bypass the wrapper, so each
of the three notifications of spec 6.3 carries one primary action and the
secondary actions live in the panel and on the keybind -- which is what
"offered alongside rather than instead" already promised.

D14: auto-stop always notifies. A recording that stops silently is
indistinguishable from a crash.

Owner: daemon workstream. Contract: PoC contracts section 8.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GLYPH", "Notification", "detected", "ending_soon", "auto_stopped", "send"]

#: U+F0EC2, the glyph ``bar/indicators/ScreenRecording.qml`` uses.
GLYPH = "\U000f0ec2"


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    action: tuple[str, ...] | None = None  # argv for --exec
    urgency: str = "normal"
    timeout_ms: int = 30000
    replaces_id: str | None = None


def detected(app_label: str, when: str) -> Notification:
    """Call detected. Primary action: ``munin start --from-detection``."""
    raise NotImplementedError


def ending_soon(seconds_left: int) -> Notification:
    """Streams gone for ``warn_seconds``. Primary action: ``munin stop``."""
    raise NotImplementedError


def auto_stopped(minutes: int, *, failed: bool = False) -> Notification:
    """Auto-stopped (D14). Primary action: ``munin start --resume``."""
    raise NotImplementedError


def send(notification: Notification, *, enabled: bool = True) -> bool:
    """Send it. Returns False and logs rather than raising if the wrapper fails.

    A notification that cannot be shown must never take a recording down with it.
    """
    raise NotImplementedError
