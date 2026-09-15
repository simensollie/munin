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

import logging
import shutil
import subprocess
import zlib
from dataclasses import dataclass
from typing import Callable, Sequence

__all__ = [
    "GLYPH",
    "SENDER",
    "Notification",
    "detected",
    "ending_soon",
    "auto_stopped",
    "track_failed",
    "capture_widened",
    "replace_id_for",
    "format_clock",
    "build_argv",
    "send",
]

log = logging.getLogger("munin.notify")

#: U+F0EC2, the glyph ``bar/indicators/ScreenRecording.qml`` uses.
GLYPH = "\U000f0ec2"

#: The wrapper. Never ``notify-send`` directly: the wrapper is what carries the
#: Omarchy theming and the single ``--exec`` action the shell understands.
SENDER = "omarchy-notification-send"

#: Type of the injected runner, so tests can capture argv without a desktop.
Runner = Callable[[Sequence[str]], int]


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    action: tuple[str, ...] | None = None  # argv for --exec
    urgency: str = "normal"
    timeout_ms: int = 30000
    replaces_id: str | None = None


def format_clock(seconds: int) -> str:
    """``M:SS`` for a duration under an hour, ``H:MM:SS`` above it."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def replace_id_for(session_id: str | None) -> str | None:
    """A stable numeric id per session, for ``-r``.

    The wrapper passes ``-r`` through to the notification daemon, whose replace
    id is an unsigned 32-bit integer, so the session id is hashed rather than
    sent as text. Stable for the life of a session, which is what makes the three
    notifications replace each other instead of stacking.
    """
    if not session_id:
        return None
    return str(zlib.crc32(session_id.encode("utf-8")) & 0x7FFFFFFF)


def detected(app_label: str, when: str, *, session_id: str | None = None) -> Notification:
    """Call detected. Primary action: ``munin start --from-detection``."""
    return Notification(
        title="Meeting detected",
        body=f"{app_label} call, {when}. Click to record.",
        action=("munin", "start", "--from-detection"),
        urgency="normal",
        timeout_ms=30000,
        replaces_id=replace_id_for(session_id),
    )


def ending_soon(seconds_left: int, *, session_id: str | None = None) -> Notification:
    """Streams gone for ``warn_seconds``. Primary action: ``munin stop``."""
    return Notification(
        title="Meeting seems over",
        body=f"Recording stops in {format_clock(seconds_left)}. Click to stop now.",
        action=("munin", "stop"),
        urgency="normal",
        timeout_ms=60000,
        replaces_id=replace_id_for(session_id),
    )


def auto_stopped(
    minutes: int, *, failed: bool = False, session_id: str | None = None
) -> Notification:
    """Auto-stopped (D14). Primary action: ``munin start --resume``."""
    if failed:
        body = f"{minutes} min saved, but finishing failed. The audio is kept."
    else:
        body = f"{minutes} min saved. Click to resume."
    return Notification(
        title="Recording stopped",
        body=body,
        action=("munin", "start", "--resume"),
        urgency="critical" if failed else "normal",
        timeout_ms=30000,
        replaces_id=replace_id_for(session_id),
    )


def track_failed(
    kind: str, detail: str, *, session_id: str | None = None
) -> Notification:
    """A capture track died mid-meeting. Primary action: ``munin stop``.

    Only reachable for the app track: a dead microphone ends the session, and
    :func:`auto_stopped` says so. Sent once per track per session, because the
    condition does not clear -- a dead process stays dead until the next segment.
    """
    who = {"mic": "Your microphone", "app": "The meeting audio"}.get(kind, f"The {kind} track")
    return Notification(
        title="Recording problem",
        body=f"{who} stopped recording. The rest continues. Click to stop.",
        action=("munin", "stop"),
        urgency="critical",
        timeout_ms=30000,
        replaces_id=replace_id_for(session_id),
    )


def capture_widened(app_label: str, *, session_id: str | None = None) -> Notification:
    """The app's own stream could not be bound, so the desktop mix is being taken.

    Compliance-relevant, not cosmetic (spec 12): a sink-monitor track holds
    every other application's audio too -- people who were never in the meeting
    and saw no prompt. ``session.json`` records it as ``app_source`` after the
    fact; the user has to be told while it is happening, so they can stop.
    """
    return Notification(
        title="Recording all desktop audio",
        body=(
            f"Could not record only {app_label}. All sound from this computer "
            "is included. Click to stop."
        ),
        action=("munin", "stop"),
        urgency="critical",
        timeout_ms=30000,
        replaces_id=replace_id_for(session_id),
    )


def recording_system_output(*, session_id: str | None = None) -> Notification:
    """An ad-hoc start found no call to bind, so the whole output mix is taken.

    Deliberate rather than a failure (``[capture] adhoc_app_source``), but the
    same compliance point as :func:`capture_widened` applies: whatever else this
    machine plays is on the meeting track, so the user is told while they can
    still stop.
    """
    return Notification(
        title="No meeting found, recording desktop audio",
        body="All sound from this computer is included. Click to stop.",
        action=("munin", "stop"),
        urgency="normal",
        timeout_ms=15000,
        replaces_id=replace_id_for(session_id),
    )


def build_argv(notification: Notification, *, glyph: str = GLYPH) -> list[str]:
    """The exact command line, in the order the wrapper accepts.

    ``--exec`` swallows everything after it, so it is always last.
    """
    argv: list[str] = [
        SENDER,
        "-g",
        glyph,
        "-u",
        notification.urgency,
        "-t",
        str(notification.timeout_ms),
    ]
    if notification.replaces_id:
        argv += ["-r", notification.replaces_id]
    argv += [notification.title, notification.body]
    if notification.action:
        argv += ["--exec", *notification.action]
    return argv


def _run(argv: Sequence[str]) -> int:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode


def send(
    notification: Notification,
    *,
    enabled: bool = True,
    glyph: str = GLYPH,
    runner: Runner | None = None,
) -> bool:
    """Send it. Returns False and logs rather than raising if the wrapper fails.

    A notification that cannot be shown must never take a recording down with it,
    so every failure path here is a log line and a ``False``.
    """
    if not enabled:
        log.debug("notifications disabled title=%s", notification.title)
        return False
    if runner is None and shutil.which(SENDER) is None:
        log.warning(
            "%s is not on PATH; notification not shown title=%s body=%s",
            SENDER,
            notification.title,
            notification.body,
        )
        return False
    argv = build_argv(notification, glyph=glyph)
    try:
        code = (runner or _run)(argv)
    except Exception as exc:  # noqa: BLE001 - never take the recording down
        log.warning("notification failed title=%s error=%s", notification.title, exc)
        return False
    if code != 0:
        log.warning("notification exited %d title=%s", code, notification.title)
        return False
    log.info("notification sent title=%s", notification.title)
    return True
