"""``munin-rec``: detect, ask, capture. Never transcribes.

The daemon owns the state machine and every timer -- the grace period, the
one-minute warning, the resume window. Detection evidence reaches it either from
the shell plugin (``munin event call-started``) or from ``detect/linux.py``
polling, chosen by ``detection.source``; both produce the same internal event, so
the timers have one implementation.

Suggest, never auto-record (D4): a detected call raises a notification and
nothing else. Only ``start`` begins capture.

One thread, one loop: :class:`munin.ipc.Server` returns from ``poll()`` after at
most a second whether or not a request arrived, and :meth:`Daemon.tick` runs the
timers on the way round. Nothing is shared between threads because there is only
one, which is why none of this locks.

Development aid: setting ``MUNIN_FAKE_CAPTURE=1`` swaps the platform capturer for
an in-process one that writes silent Opus tracks. It exists so the daemon, the
socket and the state machine can be exercised end to end on a machine without a
meeting, and it is never used unless that variable is set.

Owner: daemon workstream. Contracts: sections 4, 6, 7, 8, 12.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from munin import m365, notify
from munin.capture.base import (
    SYSTEM_OUTPUT_HANDLE,
    CaptureError,
    CaptureResult,
    CaptureTarget,
    Capturer,
    SessionRef,
    segment_filenames,
)
from munin.config import Config
from munin.desktop import IdleInhibitor, get_idle_inhibitor
from munin.ipc import AlreadyRunning, IpcError, Server, socket_path
from munin.notify import Notification
from munin.spool import StateError

__all__ = ["Daemon", "RuntimeState", "main"]

log = logging.getLogger("munin.daemon")

#: How often the loop wakes to run the timers.
POLL_INTERVAL = 1.0

#: How often ``state.json`` is refreshed when nothing has changed, so a reader
#: can tell a live daemon from a stale file. Elapsed time is still computed by
#: the plugin from ``started_at``; no timer writes a *changing* field here.
HEARTBEAT_SECONDS = 30.0

#: How much recording has to exist before a split can take a meeting off it.
#: A notification clicked twice, or a keybind pressed twice, is the reason this
#: is not zero: the second click would otherwise leave a session of a second or
#: two behind, with a directory, a record and an inbox entry of its own.
MIN_SPLIT_SECONDS = 5.0

#: How often a live capture's tracks are checked for a process that has died.
#: Cheap by construction -- :meth:`Capturer.failed_tracks` spawns nothing -- but
#: there is no point asking more often than a track can plausibly fail.
HEALTH_POLL_SECONDS = 5.0

#: The runtime states ``state.json`` may carry (contracts section 7.2).
RUNTIME_STATES: tuple[str, ...] = (
    "idle",
    "detected",
    "recording",
    "ending",
    "captured",
    "transcribing",
    "done",
    "failed",
)


def _now() -> datetime:
    """Local wall clock, timezone-aware. Never naive, never ``Z`` (contracts 2)."""
    return datetime.now().astimezone()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat(timespec="seconds")


def _state_file(explicit: Path | str | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/munin/state.json``.

    :mod:`munin.paths` owns the rule. It raises when ``XDG_RUNTIME_DIR`` is
    unset, which the daemon reports as an internal error rather than falling
    back to ``/tmp`` -- a state file that survives a reboot would tell the bar
    a recording is live when no daemon is running.
    """
    if explicit is not None:
        return Path(explicit)
    from munin import paths

    try:
        return paths.state_path()
    except RuntimeError as exc:
        raise IpcError("internal", str(exc)) from exc


def _write_atomic(path: Path, payload: str) -> None:
    """tmp + ``os.replace`` in the same directory: a reader never sees a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    #: The resolved ``[[detection.apps]]`` table, so the shell plugin matches on
    #: the user's rules instead of a copy compiled into its own source. D13 says
    #: another meeting application is a row in ``config.toml``, never code, and
    #: the plugin is the default detection source -- it cannot read the config
    #: itself, so the daemon, which owns it, publishes it here.
    detection_rules: list[dict] = field(default_factory=list)
    #: ``[transcribe] backend``. "none" tells the bar that nothing will drain
    #: the queue, so a captured session is an end state to render quietly,
    #: not work in progress to spin for.
    transcription_backend: str = "none"
    #: ``[detection] resume_window_seconds`` (D15), so the panel can say how
    #: long *Resume* still means "the same meeting" and offer *New recording*
    #: beside it -- otherwise a click meant as "new" inside the window glues
    #: the audio onto the previous session.
    resume_window_seconds: int = 600

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "state": self.state,
            "since": _iso(self.since),
            "started_at": _iso(self.started_at),
            "title": self.title,
            "session": self.session,
            "session_id": self.session_id,
            "segment": self.segment,
            "detected_app": self.detected_app,
            "grace_deadline": _iso(self.grace_deadline),
            "queue_depth": self.queue_depth,
            "last_error": self.last_error,
            "idle_was_inhibited": self.idle_was_inhibited,
            "detection_rules": list(self.detection_rules),
            "transcription_backend": self.transcription_backend,
            "resume_window_seconds": self.resume_window_seconds,
            "updated_at": _iso(_now()),
            "daemon_pid": self.daemon_pid,
        }

    def write(self, path: Path | None = None) -> None:
        """Atomic tmp + rename into ``$XDG_RUNTIME_DIR/munin/state.json``."""
        target = _state_file(path)
        _write_atomic(target, json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n")


class SilentCapturer(Capturer):
    """A capturer that records nothing, for ``MUNIN_FAKE_CAPTURE=1``.

    It writes two real, playable, silent Opus files of the segment's true
    duration so that the session layout, the checksums and the worker's view of
    it are exactly what a real capture produces. It is a development aid for
    driving the daemon without a meeting -- never a fallback for a broken
    PipeWire, which must fail loudly instead.
    """

    method = "fake-silence"

    def __init__(self, mic: CaptureTarget, app: CaptureTarget | None, **kwargs: Any) -> None:
        super().__init__(mic, app, **kwargs)
        self._started_at: datetime | None = None
        self._paths: tuple[Path, Path] | None = None
        self._index = 0

    @property
    def is_running(self) -> bool:
        return self._started_at is not None

    def start(self, session: SessionRef, segment_index: int) -> None:
        if self.is_running:
            raise CaptureError("capture is already running")
        mic_name, app_name = segment_filenames(segment_index)
        directory = Path(session.directory)
        directory.mkdir(parents=True, exist_ok=True)
        self._paths = (directory / mic_name, directory / app_name)
        self._index = segment_index
        self._started_at = _now()

    def stop(self) -> CaptureResult:
        if not self.is_running or self._paths is None or self._started_at is None:
            raise CaptureError("capture is not running")
        stopped_at = _now()
        duration = max(0.1, (stopped_at - self._started_at).total_seconds())
        for path in self._paths:
            _write_silence(path, duration, self.sample_rate, self.bitrate_kbps)
        result = CaptureResult(
            segment_index=self._index,
            mic_path=self._paths[0],
            app_path=self._paths[1],
            started_at=self._started_at,
            stopped_at=stopped_at,
            duration_seconds=duration,
        )
        self._started_at = None
        self._paths = None
        return result

    def describe(self) -> dict[str, str]:
        return {
            "method": self.method,
            "mic": self.mic.label,
            "app": self.app.label if self.app else "none",
            "encoder": "ffmpeg-libopus (silence)",
            # Honest about what is on the app track, so a developer exercising
            # MUNIN_FAKE_CAPTURE sees a populated app_source rather than the
            # null that means "unknown" (contracts section 3).
            "app_source": "silent",
        }


def _write_silence(path: Path, seconds: float, rate: int, bitrate_kbps: int) -> None:
    """A valid silent Opus file, or an empty placeholder if ffmpeg is missing."""
    argv = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r={rate}:cl=mono",
        "-t", f"{seconds:.3f}",
        "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k",
        str(path),
    ]
    try:
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        log.warning("fake capture could not encode silence path=%s error=%s", path, exc)
        path.write_bytes(b"")


#: ``segments[].app_source`` values that mean "this application and nothing
#: else" (contracts section 3). Anything outside this set widened the capture
#: beyond the meeting, and the user is told while they can still stop.
ISOLATED_APP_SOURCES = frozenset({"process-sink", "stream"})

CapturerFactory = Callable[[CaptureTarget, CaptureTarget | None], Capturer]
Notifier = Callable[[Notification], bool]
Runner = Callable[[Sequence[str]], int]


class Daemon:
    """The recorder process."""

    def __init__(
        self,
        config: Config,
        *,
        spool: Any | None = None,
        capturer_factory: CapturerFactory | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] | None = None,
        detector: Any | None = None,
        idle_runner: Runner | None = None,
        idle_inhibitor: IdleInhibitor | None = None,
        socket_path_override: Path | None = None,
        state_path_override: Path | None = None,
    ) -> None:
        self.config = config
        self.state = RuntimeState(
            daemon_pid=os.getpid(),
            detection_rules=_rule_rows(config),
            transcription_backend=config.transcribe.backend,
            resume_window_seconds=config.detection.resume_window_seconds,
        )
        self.clock = clock or _now
        self.spool = spool if spool is not None else _default_spool(config)
        self.capturer_factory = capturer_factory or _default_capturer_factory(config)
        self.notifier = notifier or self._default_notifier
        self.idle_runner = idle_runner or _run_quiet
        # Platform-free: which command holds the session awake lives in
        # munin.desktop, alongside capture/ and detect/ (spec 16.5).
        self.idle_inhibitor = idle_inhibitor or get_idle_inhibitor()(self.idle_runner)
        self._detector = detector
        self._socket_path = socket_path_override
        self._state_path = state_path_override

        self.session: Any | None = None
        self.capturer: Capturer | None = None
        self._segment_index = 0
        self._detected: dict[str, Any] | None = None
        #: A *different* call seen while this one is being recorded (D26). Held
        #: aside rather than written over ``_detected``, which describes the
        #: meeting on the record; this is only a candidate until the user says
        #: it is a meeting of its own.
        self._pending_call: dict[str, Any] | None = None
        #: Call keys already offered as a split for this session, so the 5 s
        #: detection poll asks once rather than every sweep.
        self._split_offered: set[tuple[Any, ...]] = set()
        self._playback_handle: str | None = None
        self._ending_since: datetime | None = None
        self._warned = False
        self._last_poll: datetime | None = None
        self._last_health_poll: datetime | None = None
        self._health_warned: set[str] = set()
        self._last_state_write: datetime | None = None
        self._stopping = False
        #: Did *this* process take the stay-awake hold? Only what we took may be
        #: released -- ``state.idle_was_inhibited`` records the prior state, and
        #: a daemon that never recorded has no business calling ``allow-idle``.
        self._idle_held = False

    # --- collaborators ---------------------------------------------------
    def _default_notifier(self, notification: Notification) -> bool:
        return notify.send(
            notification,
            enabled=self.config.notifications.enabled,
            glyph=self.config.notifications.glyph,
        )

    @property
    def detector(self) -> Any | None:
        """Lazily built, and only when ``detection.source == "daemon"``."""
        if self._detector is None:
            from munin.detect import get_detector

            self._detector = get_detector()(self.config.app_rules)
        return self._detector

    @property
    def socket_path(self) -> Path:
        return socket_path(self._socket_path)

    @property
    def is_recording(self) -> bool:
        return self.state.state in ("recording", "ending") and self.session is not None

    # --- IPC handlers, one per command in ``ipc.COMMANDS`` ---------------
    def handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "ping": self.handle_ping,
            "status": self.handle_status,
            "start": self.handle_start,
            "stop": self.handle_stop,
            "split": self.handle_split,
            "toggle": self.handle_toggle,
            "event": self.handle_event,
            "list": self.handle_list,
        }

    def handle_ping(self, args: dict[str, Any]) -> dict[str, Any]:
        from munin import __version__

        return {"pid": os.getpid(), "version": __version__}

    def handle_status(self, args: dict[str, Any]) -> dict[str, Any]:
        self._refresh_queue_depth()
        return self.state.to_dict()

    def handle_start(self, args: dict[str, Any]) -> dict[str, Any]:
        title = args.get("title")
        if title is not None and not isinstance(title, str):
            raise IpcError("bad_request", "title must be a string")
        resume = bool(args.get("resume"))
        from_detection = bool(args.get("from_detection"))

        if self.is_recording:
            # Idempotent on purpose: the notification action, the keybind and the
            # panel can all fire twice, and a second "start" during a recording
            # means the user wants to be recording, which they already are.
            log.info("start ignored, already recording session=%s", self.state.session_id)
            return self._start_payload(resumed=False)

        self._assert_space()
        now = self.clock()
        session = None
        resumed = False
        if resume:
            session = self._resumable(now)
            if session is None:
                log.info("resume window has closed; starting a fresh session")
        if session is not None:
            try:
                self._reopen(session)
                resumed = True
            except StateError as exc:
                # The worker claimed the session between ``resumable()`` and
                # here (D15: ownership is by state, and the loser of the race
                # re-reads rather than overwrites). The resume window has
                # effectively closed, so open a fresh session -- which is what
                # a closed window does anyway, and what the CLI already reports.
                log.info(
                    "could not resume session=%s (%s); starting a fresh one",
                    getattr(session, "id", None),
                    exc,
                )
                session = None
        # An ad-hoc start is usually the keybind pressed *during* a call the
        # daemon was never told about. Look once before binding anything, so
        # the app track is the meeting rather than silence or the whole mix.
        adopted = False
        if not from_detection and self._detected is None:
            adopted = self._adopt_live_call(now)
        if session is None:
            session = self._create(
                title=title, from_detection=from_detection, adopted=adopted, now=now
            )

        self.session = session
        self._segment_index = len(getattr(session, "segments", []) or []) + 1
        segment = self.spool.add_segment(session, self._segment_index)

        mic, app = self._targets()
        try:
            capturer = self.capturer_factory(mic, app)
            capturer.start(session, self._segment_index)
        except NotImplementedError as exc:
            # The platform capturer is not built on this machine. Say so rather
            # than letting an abstract method reach the user as a traceback.
            message = f"capture is not implemented on this platform: {exc}"
            self._fail(session, "capture_failed", message)
            raise IpcError("capture_failed", message) from exc
        except CaptureError as exc:
            self._fail(session, "capture_failed", str(exc))
            raise IpcError("capture_failed", str(exc)) from exc

        self.capturer = capturer
        self._last_health_poll = None
        self._health_warned = set()
        session.capture_method = getattr(capturer, "method", None)
        if getattr(session, "started_at", None) is None:
            session.started_at = getattr(segment, "started_at", None) or now
        self._save(session)

        self.inhibit_idle(True)

        self.state.state = "recording"
        self.state.since = now
        # The current segment's start, not the session's: after a resume the bar
        # must show how long this stretch has been running, not the wall-clock
        # span across the gap.
        self.state.started_at = getattr(segment, "started_at", None) or now
        self.state.title = getattr(session, "title", None)
        self.state.session = str(getattr(session, "directory", ""))
        self.state.session_id = getattr(session, "id", None)
        self.state.segment = self._segment_index
        self.state.grace_deadline = None
        self.state.last_error = None
        self._ending_since = None
        self._warned = False
        # Whatever was offered as a split belongs to the session that just
        # ended; this one has been asked nothing yet.
        self._split_offered = set()
        self._pending_call = None
        self._write_state()
        self._warn_if_capture_widened(capturer, app)
        log.info(
            "recording started session=%s segment=%d resumed=%s source=%s",
            self.state.session_id,
            self._segment_index,
            resumed,
            "detected" if from_detection else "adhoc",
        )
        return self._start_payload(resumed=resumed)

    def handle_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.is_recording:
            raise IpcError("not_recording", "nothing is being recorded")
        return self._finish(auto=False)

    def handle_split(self, args: dict[str, Any]) -> dict[str, Any]:
        """Finish the running recording here and start the next one (D26).

        The cheap half of the split: the boundary is now, so there is no audio
        to cut. One meeting is closed exactly as ``stop`` would close it -- same
        checksums, same inbox link, same worker path -- and the next is opened
        with whatever the detector has since seen, which is usually the call
        that triggered the prompt.

        Between the two there is a gap of well under a second, in which nothing
        is captured. That is the honest cost of the design: two encoders on one
        source, overlapping, would cost a good deal more than the half-sentence
        spoken while the user walks between meetings.
        """
        title = args.get("title")
        if title is not None and not isinstance(title, str):
            raise IpcError("bad_request", "title must be a string")
        if not self.is_recording:
            raise IpcError("not_recording", "nothing is being recorded")
        # A notification clicked twice, or a keybind pressed twice, would
        # otherwise leave a session of a second or two behind between the two
        # meetings -- a directory, a record and an inbox entry for nothing.
        elapsed = self._seconds_recording()
        if elapsed is not None and elapsed < MIN_SPLIT_SECONDS:
            raise IpcError(
                "too_soon",
                f"this recording is {int(elapsed)} s old; there is nothing to "
                "split off yet",
            )

        closed_id = getattr(self.session, "id", None)
        pending = self._pending_call
        finished = self._finish(
            auto=False,
            reason="split",
            note="split here: a new meeting was detected while recording",
        )

        # The next meeting is the call that prompted the split, if there was
        # one. Promoting it now is what makes the new session carry the right
        # application on its record rather than the one that has just ended.
        if pending is not None:
            self._detected = pending
            self.state.detected_app = {
                "app_id": pending.get("app_id"),
                "label": pending.get("label"),
                "pid": pending.get("pid"),
            }
        self._pending_call = None

        try:
            started = self.handle_start(
                {"title": title, "from_detection": pending is not None}
            )
        except IpcError as exc:
            # The first meeting is already captured and queued; only the second
            # recording failed to start. Say both halves of that, because "split
            # failed" would read as "the last hour is gone".
            log.error(
                "split closed session=%s but could not start the next one: %s",
                closed_id,
                exc,
            )
            raise IpcError(
                exc.code,
                f"{exc.message}; {closed_id} was saved, but the next recording "
                "did not start",
            ) from exc

        opened_id = started.get("session_id")
        self._carry_app_onto(started.get("session_id"))
        self._link_split(closed_id, opened_id)
        payload = dict(started)
        payload["closed"] = finished.get("session")
        payload["closed_id"] = closed_id
        payload["closed_duration_seconds"] = finished.get("duration_seconds")
        log.info("split session=%s into %s", closed_id, opened_id)
        return payload

    def _seconds_recording(self) -> float | None:
        """How long the current stretch has been running, or ``None``."""
        started = self.state.started_at
        if started is None:
            return None
        return max(0.0, (self.clock() - started).total_seconds())

    def _carry_app_onto(self, opened_id: str | None) -> None:
        """Put the live call on the new session's record when nothing named it.

        A split made from the panel rather than from a prompt has no *new* call
        behind it, so ``_create`` writes no ``app`` -- but the daemon is still
        bound to the application it was recording, and the capturer is about to
        take that application's audio. ``session.json`` has to say which
        application the app track holds; the alternative is a record that claims
        less than the capture does (spec 12).
        """
        if opened_id is None or self.session is None:
            return
        if getattr(self.session, "app", None) or self._detected is None:
            return
        detected = self._detected
        try:
            self.session.update(
                app={
                    "app_id": detected.get("app_id"),
                    "label": detected.get("label"),
                    "matched_by": detected.get("matched_by"),
                    "pid": detected.get("pid"),
                    "client_name": detected.get("client_name"),
                    "binary": detected.get("binary"),
                    "window_class": detected.get("window_class"),
                    "window_title": detected.get("window_title"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - the recording is not at risk
            log.warning("could not record the application on session=%s error=%s",
                        opened_id, exc)

    def _link_split(self, closed_id: str | None, opened_id: str | None) -> None:
        """Write the provenance both ways, without failing the split over it.

        Both recordings are already safe by the time this runs, so a store that
        refuses the link is a legible warning rather than an error the user can
        act on. The ``history[]`` rows on both sessions already carry the note;
        these two fields are what makes the pair machine-readable (spec 12).
        """
        if opened_id is None or closed_id is None:
            return
        try:
            closed = self.spool.load(closed_id)
            # Under the session's own lock, and re-read inside it: the worker
            # may have claimed this session (captured -> pending) in the
            # milliseconds since it was captured, and writing a snapshot from
            # before that would roll its state and its history row back.
            with closed.lock():
                fresh = closed.reload()
                fresh.split_into = [opened_id]
                fresh.save()
        except Exception as exc:  # noqa: BLE001 - the audio is not at risk
            log.warning("could not record the split on session=%s error=%s", closed_id, exc)
        if self.session is None:
            return
        try:
            self.session.update(
                split_from={
                    "session": closed_id,
                    "kind": "live",
                    "part": 2,
                    "offset_seconds": None,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record the split on session=%s error=%s", opened_id, exc)

    def handle_toggle(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.is_recording:
            payload = self.handle_stop({})
            payload["action"] = "stopped"
            return payload
        payload = self.handle_start({"title": args.get("title")})
        payload["action"] = "started"
        return payload

    def handle_event(self, args: dict[str, Any]) -> dict[str, Any]:
        """``call-started`` / ``call-ended`` evidence from the plugin or poller."""
        event = args.get("event")
        if event not in ("call-started", "call-ended"):
            raise IpcError("bad_request", "event must be call-started or call-ended")
        if not self.config.detection.enabled:
            return {"accepted": False, "state": self.state.state}
        pid = args.get("pid")
        if pid is not None and not isinstance(pid, int):
            raise IpcError("bad_request", "pid must be an integer")
        if event == "call-started":
            accepted = self.on_call_started(
                pid=pid,
                app=args.get("app"),
                title=args.get("title"),
                app_id=args.get("app_id"),
                handle=args.get("handle"),
            )
        else:
            accepted = self.on_call_ended(pid=pid)
        return {"accepted": accepted, "state": self.state.state}

    def handle_list(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = args.get("limit", 20)
        if not isinstance(limit, int) or limit < 1:
            raise IpcError("bad_request", "limit must be a positive integer")
        sessions = []
        for session in self.spool.iter_sessions(limit=limit):
            sessions.append(
                {
                    "id": getattr(session, "id", None),
                    "state": getattr(session, "state", None),
                    "title": getattr(session, "title", None),
                    "started_at": _iso(getattr(session, "started_at", None)),
                    "duration_seconds": getattr(session, "duration_seconds", None),
                    "path": str(getattr(session, "directory", "")),
                    "pending_reason": getattr(session, "pending_reason", None),
                }
            )
        return {"sessions": sessions}

    # --- detection events ------------------------------------------------
    def on_call_started(
        self,
        *,
        pid: int | None = None,
        app: str | None = None,
        title: str | None = None,
        app_id: str | None = None,
        handle: str | None = None,
    ) -> bool:
        """A call went live, or came back during the grace period."""
        detected = {
            "app_id": app_id or "unknown",
            "label": app or "Meeting",
            "pid": pid,
        }
        if handle:
            self._playback_handle = str(handle)
        if title:
            detected["window_title"] = title

        if self.state.state == "ending":
            # The stream came back, or the user chose "Keep recording" -- or the
            # next meeting started inside the grace period, which looks exactly
            # the same from here and is the commonest way two meetings end up in
            # one file. Keep recording either way (never lose audio), then ask.
            self._cancel_grace()
            self._offer_split(detected)
            return True
        if self.is_recording:
            self._offer_split(detected)
            return True

        first_sighting = self._detected is None or self._detected.get("pid") != pid
        self._detected = detected
        self.state.detected_app = {
            "app_id": detected["app_id"],
            "label": detected["label"],
            "pid": pid,
        }
        self.state.state = "detected"
        self.state.since = self.clock()
        self._write_state()
        if first_sighting:
            # D4: suggest, never auto-record.
            event = self._calendar_event(self.clock())
            self.notifier(
                notify.detected(
                    detected["label"],
                    self.clock().strftime("%H:%M"),
                    subject=event.subject if event is not None else None,
                )
            )
        log.info("call detected pid=%s app=%s", pid, detected["label"])
        return True

    def _most_interesting(self, identified: list[Any]) -> Any:
        """Which live call the poller should report, when there is more than one.

        Normally the first, which is what a single call has always meant. While
        recording, the D26 case is precisely *two* calls live at once -- the
        meeting being recorded and the one the user has just joined -- and the
        scan returns them in PipeWire's order, not in the order they started.
        Reporting the first would report the meeting already on the record, over
        and over, and the second meeting would never be seen at all.
        """
        current = self._call_key(self._detected)
        if current is None or not self.is_recording:
            return identified[0]
        for call in identified:
            window = getattr(call, "window", None)
            key = (
                getattr(call.evidence, "pid", None),
                getattr(window, "title", None) if window else None,
            )
            if key != current:
                return call
        return identified[0]

    def _call_key(self, detected: dict[str, Any] | None) -> tuple[Any, ...] | None:
        """What makes one call distinguishable from the next, or ``None``.

        ``(pid, window_title)``, because neither alone is enough: two
        consecutive meetings in the same Teams client share a pid and differ
        only in the title, and two tabs differ in pid while the title may be
        generic. A key is only formed for evidence that carries a pid -- the
        panel's *Keep recording* button sends a bare ``munin event
        call-started`` with no pid, and that is an answer about this meeting,
        never the announcement of another one.
        """
        if not detected or detected.get("pid") is None:
            return None
        return (detected.get("pid"), detected.get("window_title"))

    def _offer_split(self, detected: dict[str, Any]) -> bool:
        """D26: a different call went live mid-recording. Suggest, never act.

        The daemon keeps recording and sends one notification per distinct call;
        the split happens only if the user clicks it (D4). The risk taken
        deliberately: a meeting application that rewrites its own window title
        mid-call -- Teams does, when screen sharing starts -- reads as a new
        call here and costs the user one prompt they dismiss. The reverse error
        is the expensive one, so the false positive is the one to have.
        """
        session_key = self._call_key(self._detected)
        incoming = self._call_key(detected)
        if incoming is None or session_key is None:
            # An ad-hoc recording nobody identified an application for: a call
            # going live during it is at least as likely to be the meeting being
            # joined late as a second one, and there is nothing to compare it
            # with. ``on_call_ended`` declines the mirror case for the same
            # reason.
            return False
        if incoming == session_key or incoming in self._split_offered:
            return False
        if incoming[0] == session_key[0]:
            # Same process, different title: either the user walked into the
            # next meeting in the same client, or the application renamed its
            # own window. Those are indistinguishable from here, and which one
            # is common is spec 14's open question 13 -- unanswered because
            # nothing has ever counted it on real hardware. This line is the
            # counter: a week of `grep "renamed or next meeting" ~/munin/munin.log`
            # against meetings the user remembers says whether the false
            # positive is one stray prompt a month or one a meeting.
            log.info(
                "renamed or next meeting: same pid, new title session=%s pid=%s "
                "from=%r to=%r",
                self.state.session_id,
                incoming[0],
                session_key[1],
                incoming[1],
            )
        self._split_offered.add(incoming)
        self._pending_call = detected
        log.info(
            "a different call went live while recording session=%s pid=%s app=%s",
            self.state.session_id,
            detected.get("pid"),
            detected.get("label"),
        )
        self.notifier(
            notify.new_meeting(
                detected["label"],
                self.clock().strftime("%H:%M"),
                session_id=self.state.session_id,
            )
        )
        return True

    def on_call_ended(self, *, pid: int | None = None) -> bool:
        """The app's streams disappeared. Starts the grace period if recording."""
        if (
            pid is not None
            and self._pending_call is not None
            and self._pending_call.get("pid") == pid
        ):
            # The meeting that was offered as a split has gone. Keeping it would
            # mean a later click bound the new session to a dead application:
            # the record would name it and the capturer would fall back to the
            # desktop mix, which is the mismatch spec 12 cares about.
            log.info("the call offered as a split ended pid=%s", pid)
            self._pending_call = None
        if self.is_recording and self.state.state == "recording":
            session_pid = (self.state.detected_app or {}).get("pid")
            if session_pid is None:
                # Nothing the detector identified is behind this recording -- an
                # ad-hoc ``munin start`` in a room with no call. "The call ended"
                # is then not a statement about this session, and acting on it
                # would auto-stop every ad-hoc recording one grace period in.
                return False
            if pid is not None and pid != session_pid:
                return False
            self._begin_grace()
            return True
        if self.is_recording:
            # Already in the grace period: the timer owns the ending from here.
            return False
        if self._detected is not None or self._playback_handle is not None:
            watched = (self.state.detected_app or {}).get("pid")
            if pid is not None and watched is not None and pid != watched:
                return False
            was_detected = self.state.state == "detected"
            # Forget the call, handle included. A playback handle kept past the
            # call's life would be handed to the next recording, where the node
            # is gone and pw-record answers a dead --target with the whole
            # desktop mix (capture/linux.py, and contracts section 3's
            # ``app_source``) -- audio from people who were never in a meeting.
            self._forget_detected()
            if was_detected:
                self._refresh_idle_state()
            return True
        return False

    def _forget_detected(self) -> None:
        """Drop every trace of the call the daemon was watching."""
        self._detected = None
        self._pending_call = None
        self._split_offered = set()
        self._playback_handle = None
        self.state.detected_app = None

    def _begin_grace(self) -> None:
        now = self.clock()
        self._ending_since = now
        self._warned = False
        deadline = now + timedelta(seconds=self.config.detection.grace_seconds)
        if self.session is not None:
            self._transition(self.session, "ending")
        self.state.state = "ending"
        self.state.since = now
        self.state.grace_deadline = deadline
        self._write_state()
        log.info(
            "grace period started session=%s deadline=%s",
            self.state.session_id,
            _iso(deadline),
        )

    def _cancel_grace(self) -> None:
        now = self.clock()
        if self.session is not None:
            self._transition(self.session, "recording")
        self._ending_since = None
        self._warned = False
        self.state.state = "recording"
        self.state.since = now
        self.state.grace_deadline = None
        self._write_state()
        log.info("grace period cancelled session=%s", self.state.session_id)

    # --- timers and lifecycle -------------------------------------------
    def tick(self, *, now: datetime | None = None) -> None:
        """One pass of the timers: grace period, warning, resume window, poll."""
        current = now or self.clock()
        if self.state.state == "ending" and self._ending_since is not None:
            elapsed = (current - self._ending_since).total_seconds()
            grace = self.config.detection.grace_seconds
            warn_at = max(0, grace - self.config.detection.warn_seconds)
            if not self._warned and elapsed >= warn_at:
                self._warned = True
                remaining = int(max(0, grace - elapsed))
                self.notifier(
                    notify.ending_soon(remaining, session_id=self.state.session_id)
                )
                log.info("warned that the recording stops in %ds", remaining)
            if elapsed >= grace:
                log.info("grace period expired; stopping session=%s", self.state.session_id)
                try:
                    self._finish(auto=True)
                except IpcError as exc:
                    # Nobody asked for this stop, so there is no reply to carry
                    # the error: it is logged, the session is already marked
                    # failed, and the loop keeps running.
                    log.error("auto-stop failed code=%s message=%s", exc.code, exc)
                return

        self._poll_capture_health(current)
        self._poll_detection(current)
        self._heartbeat(current)

    def _poll_capture_health(self, now: datetime) -> None:
        """Notice a track whose processes have died, while the meeting runs.

        ffmpeg's Ogg-Opus muxer writes nothing to disk until it closes the file,
        so an encoder killed five minutes into an hour leaves a zero-byte file
        and no other symptom. Without this the user finds out at ``stop``, an
        hour later, with nothing to keep.

        A dead microphone ends the session there and then and notifies (D14: a
        recording that stops silently is indistinguishable from a crash). A dead
        app track is surfaced but not fatal -- the mic side is still a record of
        the meeting, and throwing it away to punish the other track helps nobody.
        """
        if not self.is_recording or self.capturer is None:
            return
        if self._last_health_poll is not None:
            if (now - self._last_health_poll).total_seconds() < HEALTH_POLL_SECONDS:
                return
        self._last_health_poll = now
        probe = getattr(self.capturer, "failed_tracks", None)
        if probe is None:  # a capturer that cannot answer is not a failing one
            return
        try:
            failed = list(probe())
        except Exception as exc:  # noqa: BLE001 - a broken probe is not a dead track
            log.warning("could not read capture health error=%s", exc)
            return
        dead = {str(getattr(track, "kind", "")): track for track in failed}
        if not dead:
            return

        if "mic" in dead:
            detail = str(getattr(dead["mic"], "detail", "the recorder went away"))
            log.error(
                "microphone capture died mid-recording session=%s detail=%s",
                self.state.session_id,
                detail,
            )
            try:
                self._finish(auto=True, reason=f"microphone capture died: {detail}")
            except IpcError as exc:
                # Same reasoning as the grace-period auto-stop: nobody is
                # holding a socket for this, so it is logged, not raised.
                log.error(
                    "could not finish after a dead microphone code=%s message=%s",
                    exc.code,
                    exc,
                )
            return

        for kind, track in dead.items():
            if kind in self._health_warned:
                continue
            self._health_warned.add(kind)
            detail = str(getattr(track, "detail", "the recorder went away"))
            log.error(
                "the %s track died mid-recording session=%s detail=%s",
                kind,
                self.state.session_id,
                detail,
            )
            self.state.last_error = f"the {kind} track died: {detail}"
            self._write_state()
            self.notifier(
                notify.track_failed(kind, detail, session_id=self.state.session_id)
            )

    def _poll_detection(self, now: datetime) -> None:
        detection = self.config.detection
        if not detection.enabled or detection.source != "daemon":
            return
        if self._last_poll is not None:
            if (now - self._last_poll).total_seconds() < detection.poll_seconds:
                return
        self._last_poll = now
        try:
            calls = self.detector.scan(now=now)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - a broken probe is not fatal
            log.warning("detection poll failed error=%s", exc)
            return
        identified = [call for call in calls if call.identity is not None]
        if identified:
            call = self._most_interesting(identified)
            self.on_call_started(
                pid=call.evidence.pid,
                app=call.identity.label,
                app_id=call.identity.app_id,
                title=call.window.title if call.window else None,
                handle=call.evidence.playback_handle,
            )
        else:
            # Only a call the detector actually identified can be ended by the
            # detector. Without this gate an ad-hoc recording -- which has no
            # ``detected_app`` -- enters the grace period on the first poll and
            # auto-stops ``grace_seconds`` later, silently truncating it.
            watched = (self.state.detected_app or {}).get("pid")
            if watched is not None:
                self.on_call_ended(pid=watched)

    def _heartbeat(self, now: datetime) -> None:
        """Keep ``state.json`` fresh, and mirror what the worker has done since.

        Nothing that changes second by second is written here -- the plugin still
        computes elapsed time from ``started_at``. What this does buy is a bar
        that follows the session out of the daemon's hands and into the worker's,
        and a file a reader can tell is stale.
        """
        if self._last_state_write is not None:
            if (now - self._last_state_write).total_seconds() < HEARTBEAT_SECONDS:
                return
        if self.state.state in ("recording", "ending", "detected"):
            self._write_state()
        else:
            self._refresh_idle_state()

    def inhibit_idle(self, on: bool) -> None:
        """Keep the session awake for the recording's duration.

        Records whether the session was already being held awake before touching
        it and restores that prior state on stop, so a user who keeps the machine
        awake permanently does not lose it when a recording ends. Which command
        does the holding is the platform's business (:mod:`munin.desktop`).

        Release only what this process took. ``idle_was_inhibited`` answers "was
        it held before?", which is not the same question as "did we hold it?": a
        daemon that never recorded has both answers False, and releasing on that
        would switch off a stay-awake the *user* set by hand at every
        ``systemctl --user restart`` and every logout.
        """
        if not self.config.idle.inhibit:
            return
        if on:
            self.state.idle_was_inhibited = self.idle_inhibitor.is_inhibited()
            if self.state.idle_was_inhibited:
                log.info("idle was already inhibited; leaving it alone")
                return
            self.idle_inhibitor.inhibit()
            self._idle_held = True
        else:
            if self.state.idle_was_inhibited:
                log.info("idle was inhibited before this recording; leaving it held")
            elif self._idle_held:
                self.idle_inhibitor.release()
            self._idle_held = False
            self.state.idle_was_inhibited = False

    def run(self) -> int:
        """Bind the socket, write ``state.json``, serve until SIGTERM."""
        server = Server(self.socket_path, self.handlers())
        try:
            server.bind()
        except AlreadyRunning as exc:
            log.error("%s", exc)
            print(str(exc), file=sys.stderr)
            return 3
        except OSError as exc:
            # Most often AF_UNIX's 108-byte path limit, which is worth naming:
            # the path in the message is the whole diagnosis.
            message = f"cannot bind {self.socket_path}: {exc}"
            log.error("%s", message)
            print(message, file=sys.stderr)
            return 3
        self._install_signals()
        self._recover()
        self._refresh_idle_state()
        log.info("munin-rec ready pid=%d socket=%s", os.getpid(), self.socket_path)
        try:
            server.serve_forever(
                on_idle=self.tick,
                poll_interval=POLL_INTERVAL,
                should_stop=lambda: self._stopping,
            )
        finally:
            self.shutdown()
            server.close()
        log.info("munin-rec stopped")
        return 0

    def _recover(self) -> None:
        """Spec 11: adopt whatever the last munin-rec left open.

        A SIGKILL, an OOM kill or a power cut skips :meth:`shutdown` entirely
        and leaves a session at ``recording`` or ``ending``. Nothing downstream
        rescues it -- the worker only ever looks at ``captured`` and ``pending``
        -- so without this the audio is stranded on disk forever and the bar
        shows a live recording with no capturer behind it. ``munin-work`` does
        the same thing with ``transcribing`` before it drains.

        Guarded: one unreadable session must not stop the daemon from binding.
        """
        # The audio server first: a killed daemon can leave the meeting
        # application's audio routed through a capture device that no longer
        # has an owner, which the user hears (or stops hearing) immediately.
        try:
            from munin.capture import recover_capture

            for note in recover_capture():
                log.info("capture recovery: %s", note)
        except Exception as exc:  # noqa: BLE001 - never block startup on cleanup
            log.warning("capture recovery failed error=%s", exc)

        try:
            recovered = self.spool.recover_for_daemon()
        except Exception as exc:  # noqa: BLE001 - a broken session is not fatal
            log.warning("crash recovery failed error=%s", exc)
            return
        for session in recovered or []:
            log.info(
                "recovered an interrupted session=%s state=%s",
                getattr(session, "id", None),
                getattr(session, "state", None),
            )
        self._refresh_queue_depth()

    def shutdown(self) -> None:
        """Stop capture cleanly and leave a conformant session behind.

        A SIGTERM mid-recording must never lose a segment: the encoders are
        stopped, the segment is finalised and the session reaches ``captured``
        before the process exits.
        """
        if self.is_recording:
            log.info("shutting down mid-recording; finalising session=%s", self.state.session_id)
            try:
                self._finish(auto=True, reason="shutdown")
            except Exception:  # noqa: BLE001 - exit must still happen
                log.exception("could not finalise the recording during shutdown")
        else:
            self.inhibit_idle(False)
        self._write_state()

    def _install_signals(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            log.info("signal received signum=%d", signum)
            self._stopping = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except ValueError:  # pragma: no cover - not the main thread
                pass

    # --- session plumbing ------------------------------------------------
    def _targets(self) -> tuple[CaptureTarget, CaptureTarget | None]:
        mic = CaptureTarget(
            kind="mic",
            handle=self.config.capture.mic_source,
            label="microphone",
        )
        app: CaptureTarget | None = None
        # ``_detected`` is the daemon's belief that a call is live; a handle or
        # a pid without it is debris from a finished call, and binding it would
        # either miss the meeting or widen capture to the desktop mix. With a
        # live detection, a pid alone is enough: the capturer gives that
        # process its own sink (process-sink) and only needs a stream handle
        # for the older one-node fallback. Measured 2026-09-15: an event with
        # --pid but no --handle otherwise fell through to the desktop mix.
        detected = self._detected
        if detected is not None and (self._playback_handle or detected.get("pid")):
            app = CaptureTarget(
                kind="app",
                handle=self._playback_handle or "",
                label=str(detected.get("label", "meeting audio")),
                pid=detected.get("pid"),
                app_id=detected.get("app_id"),
            )
        elif self.config.capture.adhoc_app_source == "system-output":
            # No call to bind to. Record the whole output mix rather than
            # silence: the user pressed record, and a meeting track with
            # nothing on it is the one outcome that cannot be repaired later.
            app = CaptureTarget(kind="app", handle=SYSTEM_OUTPUT_HANDLE, label="system output")
        return mic, app

    def _adopt_live_call(self, now: datetime) -> bool:
        """One detector scan for an ad-hoc start; adopt a live call if there is one.

        An identified call wins; failing that, a *single* unidentified live call
        is taken, because the user has just said "record" and one process holding
        both a playback and a capture stream is the only candidate. Two or more
        unidentified calls is ambiguous and nothing is adopted. Any probe failure
        is a warning, never a reason not to record.
        """
        if not self.config.detection.enabled:
            return False
        try:
            calls = self.detector.scan(now=now)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - a broken probe must not block a recording
            log.warning("could not look for a live call before an ad-hoc start error=%s", exc)
            return False
        identified = [call for call in calls if call.identity is not None]
        unidentified = [call for call in calls if call.identity is None]
        if identified:
            call = identified[0]
        elif len(unidentified) == 1:
            call = unidentified[0]
        else:
            return False
        evidence = call.evidence
        if not evidence.playback_handle:
            return False
        identity = call.identity
        window = call.window
        self._detected = {
            "app_id": identity.app_id if identity else "unknown",
            "label": identity.label if identity else (evidence.client_name or "Meeting"),
            "matched_by": identity.matched_by if identity else None,
            "pid": evidence.pid,
            "client_name": evidence.client_name,
            "binary": evidence.binary,
            "window_class": window.window_class if window else None,
            "window_title": window.title if window else None,
        }
        self._playback_handle = str(evidence.playback_handle)
        self.state.detected_app = {
            "app_id": self._detected["app_id"],
            "label": self._detected["label"],
            "pid": evidence.pid,
        }
        log.info(
            "ad-hoc start bound to a live call pid=%s app=%s identified=%s",
            evidence.pid,
            self._detected["label"],
            identity is not None,
        )
        return True

    def _create(
        self,
        *,
        title: str | None,
        from_detection: bool,
        now: datetime,
        adopted: bool = False,
    ) -> Any:
        detected = self._detected if (from_detection or adopted) else None
        event = self._calendar_event(now) if title is None else None
        enrichment = None
        if event is not None:
            # The invite's subject is the one name that is right every time; the
            # label plus the clock below is what an ad-hoc call is left with.
            title = event.subject
            enrichment = {
                "title_from": "calendar",
                "original_title": None,
                "decided_at": _iso(now),
            }
        if title is None and detected is not None:
            title = f"{detected['label']} {now.strftime('%H:%M')}"
        if title is None:
            title = f"Meeting {now.strftime('%H:%M')}"
        app = None
        if detected is not None:
            app = {
                "app_id": detected.get("app_id"),
                "label": detected.get("label"),
                "matched_by": detected.get("matched_by"),
                "pid": detected.get("pid"),
                "client_name": detected.get("client_name"),
                "binary": detected.get("binary"),
                "window_class": detected.get("window_class"),
                "window_title": detected.get("window_title"),
            }
        session = self.spool.create(
            title=title,
            source="detected" if from_detection else "adhoc",
            app=app,
            now=now,
            calendar_event_id=event.id if event is not None else None,
            enrichment=enrichment,
        )
        if getattr(session, "state", None) != "recording":
            self._transition(session, "recording")
        return session

    def _calendar_event(self, now: datetime) -> Any:
        """The cached calendar event overlapping ``now``, or ``None``.

        A local file read, never a network call: munin-work keeps the copy
        fresh, and this loop must not wait on Microsoft (spec 7.6). A copy the
        worker has not refreshed for a while reads as empty, so a stale slot
        never names a meeting.
        """
        settings = self.config.m365
        if not settings.enabled:
            return None
        max_age = timedelta(seconds=max(900, 3 * settings.calendar_refresh_seconds))
        try:
            events = m365.read_calendar(self.config.home, max_age=max_age, now=now)
            return m365.match_event(events, now)
        except Exception as exc:  # noqa: BLE001 - enrichment must never stop a start
            log.warning("calendar lookup failed: %s", exc)
            return None

    def _reopen(self, session: Any) -> None:
        """D15: a resume is the one state that moves backwards, and it is ours."""
        self._transition(session, "recording")

    def _resumable(self, now: datetime) -> Any | None:
        try:
            return self.spool.resumable(now=now)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not look for a resumable session error=%s", exc)
            return None

    def _finish(
        self, *, auto: bool, reason: str = "", note: str | None = None
    ) -> dict[str, Any]:
        session = self.session
        capturer = self.capturer
        assert session is not None
        now = self.clock()

        result: CaptureResult | None = None
        failure: str | None = None
        if capturer is not None:
            try:
                result = capturer.stop()
            except (CaptureError, NotImplementedError) as exc:
                failure = str(exc)
                log.error("capture produced nothing usable session=%s error=%s",
                          getattr(session, "id", None), exc)
        self.capturer = None

        segments = list(getattr(session, "segments", []) or [])
        if result is not None and segments:
            segment = segments[-1]
            segment.stopped_at = result.stopped_at
            segment.duration_seconds = result.duration_seconds
            # What the app track actually holds. A capturer that had to fall
            # back to the desktop mix ("sink-monitor") recorded more than this
            # meeting, and session.json has to say so -- it is the record of
            # what was captured, and a reader cannot infer it from the audio.
            segment.app_source = _describe_app_source(capturer)
        session.stopped_at = now
        session.duration_seconds = sum(
            float(getattr(seg, "duration_seconds", 0.0) or 0.0) for seg in segments
        )

        self.inhibit_idle(False)

        if failure is not None and not self._has_audio(session):
            self._fail(session, "capture_failed", failure)
            if auto:
                self.notifier(
                    notify.auto_stopped(
                        int(session.duration_seconds // 60),
                        failed=True,
                        session_id=getattr(session, "id", None),
                    )
                )
            raise IpcError("capture_failed", failure)

        session.checksums = self._checksums(session)
        self._transition(session, "captured", note=note)
        try:
            self.spool.link_inbox(session)
        except Exception as exc:  # noqa: BLE001 - the inbox is an index, not the record
            log.warning("could not link the inbox session=%s error=%s",
                        getattr(session, "id", None), exc)

        duration = float(session.duration_seconds or 0.0)
        payload = {
            "session": str(getattr(session, "directory", "")),
            "session_id": getattr(session, "id", None),
            "state": "captured",
            "duration_seconds": duration,
        }

        self.state.state = "captured"
        self.state.since = now
        self.state.started_at = None
        self.state.grace_deadline = None
        self.state.segment = self._segment_index
        self.state.last_error = failure
        self._ending_since = None
        self._warned = False
        self.session = None
        if auto:
            # The streams are gone; a stale "detected" app would make the bar lie.
            self._forget_detected()
        self._refresh_queue_depth()
        self._write_state()

        log.info(
            "recording captured session=%s duration=%.1fs auto=%s reason=%s",
            payload["session_id"],
            duration,
            auto,
            reason or "-",
        )
        if auto:
            # D14: an auto-stop always notifies. A recording that stops silently
            # is indistinguishable from a crash.
            self.notifier(
                notify.auto_stopped(
                    int(duration // 60),
                    failed=failure is not None,
                    session_id=payload["session_id"],
                )
            )
        return payload

    def _warn_if_capture_widened(self, capturer: Capturer, app: CaptureTarget | None) -> None:
        """Say so, now, when the app track is not the application's own stream.

        An application was asked for and something else is being recorded -- in
        practice the desktop-sink monitor, which is every other application's
        audio as well. ``segments[].app_source`` puts it on the record, but only
        afterwards; the subjects of that audio saw no prompt, so the user has to
        be able to stop while it is still happening (spec 12).
        """
        if app is None:
            return
        if app.handle == SYSTEM_OUTPUT_HANDLE:
            # Asked for, not fallen back to -- but the same people-outside-the-
            # meeting point applies, so it is still said out loud.
            log.info("the app track is the whole output mix session=%s", self.state.session_id)
            self.notifier(notify.recording_system_output(session_id=self.state.session_id))
            return
        source = _describe_app_source(capturer)
        if source is None or source in ISOLATED_APP_SOURCES:
            return
        log.warning(
            "the app track is %s, not %s's own stream session=%s",
            source,
            app.label,
            self.state.session_id,
        )
        self.state.last_error = f"the meeting track is {source}, not an isolated stream"
        self._write_state()
        self.notifier(
            notify.capture_widened(app.label, session_id=self.state.session_id)
        )

    def _has_audio(self, session: Any) -> bool:
        directory = Path(getattr(session, "directory", ""))
        for segment in getattr(session, "segments", []) or []:
            for name in (getattr(segment, "mic", None), getattr(segment, "app", None)):
                if name and (directory / name).exists() and (directory / name).stat().st_size > 0:
                    return True
        return False

    def _checksums(self, session: Any) -> dict[str, str]:
        directory = Path(getattr(session, "directory", ""))
        checksums = dict(getattr(session, "checksums", {}) or {})
        for segment in getattr(session, "segments", []) or []:
            for name in (getattr(segment, "mic", None), getattr(segment, "app", None)):
                if not name:
                    continue
                path = directory / name
                if not path.exists():
                    continue
                try:
                    checksums[name] = self.spool.checksum(path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not checksum %s error=%s", path, exc)
        return checksums

    def _fail(self, session: Any, code: str, message: str) -> None:
        session.error = {"code": code, "message": message, "at": _iso(self.clock())}
        try:
            self._transition(session, "failed")
        except Exception:  # noqa: BLE001
            log.exception("could not record the failure on disk")
        self.inhibit_idle(False)
        self.session = None
        self.capturer = None
        self.state.state = "failed"
        self.state.since = self.clock()
        self.state.started_at = None
        self.state.grace_deadline = None
        self.state.last_error = message
        self._write_state()

    def _transition(self, session: Any, to: str, *, note: str | None = None) -> None:
        session.transition(to, by="munin-rec", note=note)

    def _save(self, session: Any) -> None:
        try:
            session.save()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save session.json error=%s", exc)

    def _assert_space(self) -> None:
        try:
            free = self.spool.free_mb()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not measure free space error=%s", exc)
            return
        needed = self.config.capture.min_free_mb
        if free is not None and free < needed:
            raise IpcError(
                "no_space",
                f"only {free} MB free on the recordings filesystem, {needed} MB required",
            )

    def _refresh_queue_depth(self) -> None:
        try:
            self.state.queue_depth = int(self.spool.queue_depth())
        except Exception:  # noqa: BLE001
            pass

    def _refresh_idle_state(self) -> None:
        """On start, mirror the newest session so the bar is right after a restart."""
        self._refresh_queue_depth()
        if self.state.state in ("recording", "ending") and self.session is not None:
            self._write_state()
            return
        try:
            latest = self.spool.latest()
        except Exception:  # noqa: BLE001
            latest = None
        if latest is None:
            self.state.state = "idle"
        else:
            # ``pending`` is not one of the bar's states: it means captured and
            # safe, with the worker not started, which is what ``captured`` says.
            mapped = {"pending": "captured"}.get(
                getattr(latest, "state", ""), getattr(latest, "state", "idle")
            )
            if mapped in ("recording", "ending"):
                # No capturer is running in this process, so this is the debris
                # of a crash that recovery could not reach, never a live
                # recording. Publishing it verbatim would put a pulsing red dot
                # on the bar that no `munin stop` can turn off (D10: the bar is
                # the only reliable stop control).
                mapped = "captured" if self._has_audio(latest) else "failed"
            self.state.state = mapped if mapped in RUNTIME_STATES else "idle"
            self.state.session = str(getattr(latest, "directory", ""))
            self.state.session_id = getattr(latest, "id", None)
            self.state.title = getattr(latest, "title", None)
            self.state.since = getattr(latest, "stopped_at", None) or self.clock()
        self._write_state()

    def _start_payload(self, *, resumed: bool) -> dict[str, Any]:
        return {
            "session": self.state.session,
            "session_id": self.state.session_id,
            "state": "recording",
            "segment": self.state.segment,
            "resumed": resumed,
        }

    def _write_state(self) -> None:
        self.state.daemon_pid = os.getpid()
        try:
            self.state.write(self._state_path)
        except Exception as exc:  # noqa: BLE001 - the bar going stale is not fatal
            log.warning("could not write state.json error=%s", exc)
            return
        self._last_state_write = self.clock()


# --- defaults and entry point -------------------------------------------
def _describe_app_source(capturer: Capturer | None) -> str | None:
    """The capturer's ``app_source``, via the frozen ``describe()`` seam.

    ``describe()`` returns flat strings by contract (section 5), so this asks
    for a documented key rather than reaching for a platform attribute. A
    capturer that does not report one leaves the field null, which reads as
    "unknown", never as "isolated".
    """
    if capturer is None:
        return None
    try:
        described = capturer.describe()
    except Exception as exc:  # noqa: BLE001 - never fail a stop over metadata
        log.warning("could not describe the capturer error=%s", exc)
        return None
    value = described.get("app_source")
    return str(value) if value else None


def _rule_rows(config: Config) -> list[dict]:
    """``[[detection.apps]]`` flattened for ``state.json``, empty fields dropped."""
    rows: list[dict] = []
    for rule in getattr(config, "app_rules", ()) or ():
        row: dict[str, str] = {
            "app_id": str(getattr(rule, "app_id", "")),
            "label": str(getattr(rule, "label", "")),
        }
        for name in ("client_name", "binary", "window_class", "window_title_contains"):
            value = getattr(rule, name, None)
            if value:
                row[name] = str(value)
        rows.append(row)
    return rows


def _run_quiet(argv: Sequence[str]) -> int:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode


def _default_spool(config: Config) -> Any:
    from munin.spool import Spool

    return Spool(config)


def _default_capturer_factory(config: Config) -> CapturerFactory:
    def factory(mic: CaptureTarget, app: CaptureTarget | None) -> Capturer:
        if os.environ.get("MUNIN_FAKE_CAPTURE") == "1":
            log.warning("MUNIN_FAKE_CAPTURE=1: recording silence, not audio")
            cls: type[Capturer] = SilentCapturer
        else:
            from munin.capture import get_capturer

            cls = get_capturer()
        return cls(
            mic,
            app,
            bitrate_kbps=config.capture.bitrate_kbps,
            channels=config.capture.channels,
            sample_rate=config.capture.sample_rate,
        )

    return factory


def load_config() -> Config:
    """The config. A missing file is not an error; every key has a default."""
    from munin import config as config_module

    return config_module.load()


def configure_logging(config: Config, *, level: int = logging.INFO) -> None:
    """Structured lines to ``~/munin/munin.log`` and to stderr for systemd."""
    root = logging.getLogger("munin")
    root.setLevel(level)
    root.handlers.clear()
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    try:
        path = Path(config.home).expanduser() / config.paths.log
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except OSError as exc:  # pragma: no cover
        root.warning("no log file: %s", exc)


def main(argv: list[str] | None = None) -> int:
    """``munin-rec`` entry point. Also reached as ``munin daemon``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="munin-rec", description="Munin's recorder daemon."
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        default=True,
        help="run in the foreground (the default, and what systemd wants)",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    config = load_config()
    configure_logging(config, level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        daemon = Daemon(config)
        return daemon.run()
    except IpcError as exc:
        log.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        log.exception("munin-rec failed")
        print(f"munin-rec failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
