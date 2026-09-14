"""Test doubles for the daemon: a spool, a capturer, a clock and a notifier.

The daemon is the one component that cannot be unit tested against the real
thing -- the spool belongs to another workstream and capture needs a meeting --
so every collaborator is injected. These fakes are deliberately strict: the fake
session validates every transition against the real
:data:`munin.spool.TRANSITIONS` table, so a daemon that writes a state it does
not own fails here rather than on hardware.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from munin.capture.base import (
    CaptureError,
    CaptureResult,
    CaptureTarget,
    Capturer,
    SessionRef,
    segment_filenames,
)
from munin.spool import TRANSITIONS, StateError

__all__ = [
    "FakeClock",
    "FakeCapturer",
    "FakeSegment",
    "FakeSession",
    "FakeSpool",
    "FakeTrackHealth",
    "recording_capturer_factory",
]


class FakeClock:
    """A clock the test advances by hand, so no test ever sleeps."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 14, 13, 25, 7).astimezone()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


@dataclass(frozen=True)
class FakeTrackHealth:
    """The shape of :class:`munin.capture.linux.TrackHealth` the daemon reads."""

    kind: str
    detail: str
    alive: bool = False


@dataclass
class FakeSegment:
    index: int
    mic: str
    app: str
    started_at: datetime
    stopped_at: datetime | None = None
    duration_seconds: float | None = None


@dataclass
class FakeSession:
    id: str
    directory: Path
    state: str
    title: str
    slug: str = "meeting"
    source: str = "adhoc"
    platform: str = "linux"
    capture_method: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    duration_seconds: float | None = None
    app: dict | None = None
    calendar_event_id: None = None
    segments: list[FakeSegment] = field(default_factory=list)
    checksums: dict[str, str] = field(default_factory=dict)
    pending_reason: str | None = None
    error: dict | None = None
    transcript: dict = field(default_factory=lambda: {"txt": None, "json": None, "pensieve_copy": None})
    history: list[dict] = field(default_factory=list)
    clock: Callable[[], datetime] | None = None

    @property
    def json_path(self) -> Path:
        return self.directory / "session.json"

    def _now(self) -> datetime:
        return (self.clock or (lambda: datetime.now().astimezone()))()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "id": self.id,
            "state": self.state,
            "title": self.title,
            "slug": self.slug,
            "source": self.source,
            "platform": self.platform,
            "capture_method": self.capture_method,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "stopped_at": _iso(self.stopped_at),
            "duration_seconds": self.duration_seconds,
            "app": self.app,
            "calendar_event_id": None,
            "segments": [
                {
                    "index": seg.index,
                    "mic": seg.mic,
                    "app": seg.app,
                    "started_at": _iso(seg.started_at),
                    "stopped_at": _iso(seg.stopped_at),
                    "duration_seconds": seg.duration_seconds,
                }
                for seg in self.segments
            ],
            "checksums": self.checksums,
            "pending_reason": self.pending_reason,
            "error": self.error,
            "transcript": self.transcript,
            "history": self.history,
        }

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.json_path)

    def transition(self, to: str, *, by: str, **fields: Any) -> None:
        key = (self.state, to)
        writer = TRANSITIONS.get(key)
        if writer is None:
            raise StateError(f"illegal transition {self.state} -> {to}")
        if writer != by:
            raise StateError(f"{by} may not write {self.state} -> {to}; {writer} owns it")
        for name, value in fields.items():
            setattr(self, name, value)
        self.history.append(
            {"at": _iso(self._now()), "from": self.state, "to": to, "by": by}
        )
        self.state = to
        self.save()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="seconds")


class FakeSpool:
    """Enough of :class:`munin.spool.Spool` for the daemon, on a real filesystem."""

    def __init__(self, home: Path, *, clock: Callable[[], datetime] | None = None,
                 free_mb: int = 100_000) -> None:
        self.home = Path(home)
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._free_mb = free_mb
        self.sessions: list[FakeSession] = []
        self.inbox = self.home / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.resume_window_seconds = 600

    # --- creation ----------------------------------------------------
    def create(self, *, title: str | None, source: str, app: dict | None = None,
               now: datetime | None = None) -> FakeSession:
        created = now or self.clock()
        slug = (title or "adhoc").lower().replace(" ", "-")
        sid = f"{created:%Y-%m-%dT%H%M}-{slug}"
        suffix = 2
        while any(s.id == sid for s in self.sessions):
            sid = f"{created:%Y-%m-%dT%H%M}-{slug}-{suffix}"
            suffix += 1
        directory = self.home / "recordings" / f"{created:%Y}" / f"{created:%m}" / sid
        directory.mkdir(parents=True, exist_ok=True)
        session = FakeSession(
            id=sid,
            directory=directory,
            state="recording",
            title=title or "Meeting",
            slug=slug,
            source=source,
            app=app,
            created_at=created,
            started_at=created,
            clock=self.clock,
        )
        session.history.append(
            {"at": _iso(created), "from": None, "to": "recording", "by": "munin-rec"}
        )
        session.save()
        self.sessions.append(session)
        return session

    # --- reading -----------------------------------------------------
    def load(self, session_id: str) -> FakeSession:
        for session in self.sessions:
            if session.id == session_id:
                return session
        raise KeyError(session_id)

    def iter_sessions(self, *, limit: int | None = None) -> Iterator[FakeSession]:
        ordered = sorted(self.sessions, key=lambda s: s.id, reverse=True)
        return iter(ordered[:limit] if limit else ordered)

    def latest(self) -> FakeSession | None:
        ordered = sorted(self.sessions, key=lambda s: s.id, reverse=True)
        return ordered[0] if ordered else None

    def sessions_in_state(self, *states: str) -> list[FakeSession]:
        return [s for s in self.sessions if s.state in states]

    def recover_for_daemon(self) -> list[FakeSession]:
        """Mirrors :meth:`munin.spool.Spool.recover_for_daemon`.

        A session still ``recording`` or ``ending`` is what a SIGKILL'd daemon
        leaves behind: playable audio becomes ``captured`` with an inbox link,
        an empty directory becomes ``failed``.
        """
        recovered: list[FakeSession] = []
        at = self.clock()
        for session in self.sessions_in_state("recording", "ending"):
            has_audio = any(
                (session.directory / name).is_file()
                for segment in session.segments
                for name in (segment.mic, segment.app)
            )
            if has_audio:
                for segment in session.segments:
                    if segment.stopped_at is None:
                        segment.stopped_at = session.stopped_at or at
                session.transition(
                    "captured", by="munin-rec", stopped_at=session.stopped_at or at
                )
                self.link_inbox(session)
            else:
                session.transition(
                    "failed",
                    by="munin-rec",
                    error={
                        "code": "interrupted",
                        "message": "munin-rec stopped before any audio was written",
                        "at": _iso(at),
                    },
                )
            recovered.append(session)
        return recovered

    def resumable(self, *, now: datetime | None = None) -> FakeSession | None:
        current = now or self.clock()
        latest = self.latest()
        if latest is None or latest.state not in ("captured", "pending"):
            return None
        if latest.stopped_at is None:
            return None
        if (current - latest.stopped_at).total_seconds() > self.resume_window_seconds:
            return None
        return latest

    # --- mutation ----------------------------------------------------
    def add_segment(self, session: FakeSession, index: int) -> FakeSegment:
        mic, app = segment_filenames(index)
        segment = FakeSegment(index=index, mic=mic, app=app, started_at=self.clock())
        session.segments.append(segment)
        session.save()
        return segment

    def link_inbox(self, session: FakeSession) -> Path:
        link = self.inbox / session.id
        if not link.is_symlink():
            link.symlink_to(Path("..") / session.directory.relative_to(self.home))
        return link

    def unlink_inbox(self, session: FakeSession) -> None:
        (self.inbox / session.id).unlink(missing_ok=True)

    def queue_depth(self) -> int:
        return len(list(self.inbox.iterdir()))

    def checksum(self, path: Path) -> str:
        import hashlib

        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def free_mb(self) -> int:
        return self._free_mb


class FakeCapturer(Capturer):
    """Writes two tiny files and reports the clock's elapsed time as duration."""

    method = "fake"

    def __init__(self, mic: CaptureTarget, app: CaptureTarget | None, *,
                 clock: Callable[[], datetime], fail_on_start: bool = False,
                 fail_on_stop: bool = False, write_files: bool = True,
                 **kwargs: Any) -> None:
        super().__init__(mic, app, **kwargs)
        self.clock = clock
        self.fail_on_start = fail_on_start
        self.fail_on_stop = fail_on_stop
        self.write_files = write_files
        self.started_at: datetime | None = None
        self._paths: tuple[Path, Path] | None = None
        self._index = 0
        #: What the app track holds, as contracts section 3's ``app_source``.
        self.app_source = "silent" if app is None else "stream"
        #: Track kinds the test has killed mid-capture.
        self.dead_tracks: list[str] = []

    @property
    def is_running(self) -> bool:
        return self.started_at is not None

    def start(self, session: SessionRef, segment_index: int) -> None:
        if self.is_running:
            raise CaptureError("capture is already running")
        if self.fail_on_start:
            raise CaptureError("no PipeWire here")
        mic_name, app_name = segment_filenames(segment_index)
        directory = Path(session.directory)
        directory.mkdir(parents=True, exist_ok=True)
        self._paths = (directory / mic_name, directory / app_name)
        self._index = segment_index
        self.started_at = self.clock()

    def stop(self) -> CaptureResult:
        if not self.is_running or self._paths is None or self.started_at is None:
            raise CaptureError("capture is not running")
        if self.fail_on_stop:
            self.started_at = None
            raise CaptureError("neither track is playable")
        stopped_at = self.clock()
        if self.write_files:
            for path in self._paths:
                path.write_bytes(b"OpusHead-fake")
        result = CaptureResult(
            segment_index=self._index,
            mic_path=self._paths[0],
            app_path=self._paths[1],
            started_at=self.started_at,
            stopped_at=stopped_at,
            duration_seconds=(stopped_at - self.started_at).total_seconds(),
        )
        self.started_at = None
        self._paths = None
        return result

    def failed_tracks(self) -> list[FakeTrackHealth]:
        """The tracks the test has declared dead, while capture is running."""
        if not self.is_running:
            return []
        return [
            FakeTrackHealth(kind=kind, detail=f"{kind} encoder exited 137")
            for kind in self.dead_tracks
        ]

    def describe(self) -> dict[str, str]:
        return {"method": self.method, "mic": self.mic.label,
                "app": self.app.label if self.app else "none",
                "app_source": self.app_source}


def recording_capturer_factory(clock: Callable[[], datetime], **kwargs: Any):
    """A factory the daemon can call, keeping every capturer it made."""
    made: list[FakeCapturer] = []

    def factory(mic: CaptureTarget, app: CaptureTarget | None) -> FakeCapturer:
        capturer = FakeCapturer(mic, app, clock=clock, **kwargs)
        made.append(capturer)
        return capturer

    factory.made = made  # type: ignore[attr-defined]
    return factory
