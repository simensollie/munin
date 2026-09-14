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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from munin import notify
from munin.capture.base import (
    CaptureError,
    CaptureResult,
    CaptureTarget,
    Capturer,
    SessionRef,
    segment_filenames,
)
from munin.config import Config
from munin.ipc import AlreadyRunning, IpcError, Server, socket_path
from munin.notify import Notification

__all__ = ["Daemon", "RuntimeState", "main"]

log = logging.getLogger("munin.daemon")

#: How often the loop wakes to run the timers.
POLL_INTERVAL = 1.0

#: How often ``state.json`` is refreshed when nothing has changed, so a reader
#: can tell a live daemon from a stale file. Elapsed time is still computed by
#: the plugin from ``started_at``; no timer writes a *changing* field here.
HEARTBEAT_SECONDS = 30.0

#: Omarchy's idle service gates on this file (contracts section 12).
STAY_AWAKE_STATE = "omarchy/indicators/stay-awake"

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

    Resolved through :mod:`munin.paths` where that is implemented, with the same
    local fallback :func:`munin.ipc.socket_path` uses, so the daemon runs before
    the spool workstream lands.
    """
    if explicit is not None:
        return Path(explicit)
    try:
        from munin import paths

        return paths.state_path()
    except (NotImplementedError, ImportError):
        pass
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime:
        raise IpcError("internal", "XDG_RUNTIME_DIR is not set")
    return Path(runtime) / "munin" / "state.json"


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
        socket_path_override: Path | None = None,
        state_path_override: Path | None = None,
    ) -> None:
        self.config = config
        self.state = RuntimeState(daemon_pid=os.getpid())
        self.clock = clock or _now
        self.spool = spool if spool is not None else _default_spool(config)
        self.capturer_factory = capturer_factory or _default_capturer_factory(config)
        self.notifier = notifier or self._default_notifier
        self.idle_runner = idle_runner or _run_quiet
        self._detector = detector
        self._socket_path = socket_path_override
        self._state_path = state_path_override

        self.session: Any | None = None
        self.capturer: Capturer | None = None
        self._segment_index = 0
        self._detected: dict[str, Any] | None = None
        self._playback_handle: str | None = None
        self._ending_since: datetime | None = None
        self._warned = False
        self._last_poll: datetime | None = None
        self._last_state_write: datetime | None = None
        self._stopping = False

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
            resumed = True
            self._reopen(session)
        else:
            session = self._create(title=title, from_detection=from_detection, now=now)

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
        self._write_state()
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
            # The stream came back, or the user chose "Keep recording".
            self._cancel_grace()
            return True
        if self.is_recording:
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
            self.notifier(
                notify.detected(
                    detected["label"], self.clock().strftime("%H:%M")
                )
            )
        log.info("call detected pid=%s app=%s", pid, detected["label"])
        return True

    def on_call_ended(self, *, pid: int | None = None) -> bool:
        """The app's streams disappeared. Starts the grace period if recording."""
        if self.is_recording and self.state.state == "recording":
            session_pid = (self.state.detected_app or {}).get("pid")
            if pid is not None and session_pid is not None and pid != session_pid:
                return False
            self._begin_grace()
            return True
        if self.state.state == "detected":
            self._detected = None
            self.state.detected_app = None
            self._refresh_idle_state()
            return True
        return False

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

        self._poll_detection(current)
        self._heartbeat(current)

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
            call = identified[0]
            self.on_call_started(
                pid=call.evidence.pid,
                app=call.identity.label,
                app_id=call.identity.app_id,
                title=call.window.title if call.window else None,
                handle=call.evidence.playback_handle,
            )
        else:
            watched = (self.state.detected_app or {}).get("pid")
            if watched is not None or self.is_recording:
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
        """Hold ``omarchy-toggle-idle stay-awake`` for the recording's duration.

        Records whether stay-awake was already set before touching it and
        restores that prior state on stop, so a user who keeps the machine awake
        permanently does not lose it when a recording ends.
        """
        if not self.config.idle.inhibit:
            return
        if on:
            self.state.idle_was_inhibited = _stay_awake_is_set()
            if self.state.idle_was_inhibited:
                log.info("stay-awake was already held; leaving it alone")
                return
            self._toggle_idle("stay-awake")
        else:
            if self.state.idle_was_inhibited:
                log.info("stay-awake was held before this recording; leaving it held")
            else:
                self._toggle_idle("allow-idle")
            self.state.idle_was_inhibited = False

    def _toggle_idle(self, mode: str) -> None:
        try:
            code = self.idle_runner(["omarchy-toggle-idle", mode])
        except Exception as exc:  # noqa: BLE001 - never take a recording down
            log.warning("idle toggle failed mode=%s error=%s", mode, exc)
            return
        if code != 0:
            log.warning("idle toggle exited %d mode=%s", code, mode)

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
        if self._playback_handle:
            detected = self._detected or {}
            app = CaptureTarget(
                kind="app",
                handle=self._playback_handle,
                label=str(detected.get("label", "meeting audio")),
                pid=detected.get("pid"),
                app_id=detected.get("app_id"),
            )
        return mic, app

    def _create(self, *, title: str | None, from_detection: bool, now: datetime) -> Any:
        detected = self._detected if from_detection else None
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
        )
        if getattr(session, "state", None) != "recording":
            self._transition(session, "recording")
        return session

    def _reopen(self, session: Any) -> None:
        """D15: a resume is the one state that moves backwards, and it is ours."""
        self._transition(session, "recording")

    def _resumable(self, now: datetime) -> Any | None:
        try:
            return self.spool.resumable(now=now)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not look for a resumable session error=%s", exc)
            return None

    def _finish(self, *, auto: bool, reason: str = "") -> dict[str, Any]:
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
        self._transition(session, "captured")
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
            self._detected = None
            self._playback_handle = None
            self.state.detected_app = None
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

    def _transition(self, session: Any, to: str) -> None:
        session.transition(to, by="munin-rec")

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
        if self.state.state in ("recording", "ending"):
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
def _run_quiet(argv: Sequence[str]) -> int:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode


def _stay_awake_is_set() -> bool:
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return (Path(state_home) / STAY_AWAKE_STATE).exists()


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
    """The config, or defaults while ``munin.config.load`` is still a stub."""
    from munin import config as config_module

    try:
        return config_module.load()
    except NotImplementedError:
        home = Path(os.environ.get("MUNIN_HOME") or (Path.home() / "munin"))
        log.warning("munin.config.load is not implemented yet; using defaults home=%s", home)
        return Config(home=home)


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
