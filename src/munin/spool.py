"""The session store: layout, ``session.json``, the state machine, the inbox.

``session.json`` is the only file state is written into, so a half-finished
session is always identifiable after a crash (spec 7.5). Writes are atomic
(tmp + rename) and every transition appends to ``history[]``, which is the audit
trail this tool needs in a regulated context (spec 12).

Ownership of transitions is the contract that keeps two processes out of each
other's way (spec 11): the daemon owns ``recording``, ``ending``, ``captured``
and capture-side ``failed``; after ``captured``, only the worker writes state.

Owner: spool workstream. Schema: PoC contracts sections 3 and 4.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket as _socket
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Literal

import munin
from munin.capture.base import segment_filenames
from munin.config import Config
from munin.paths import (
    inbox_dir,
    latest_link,
    recordings_root,
    session_dir,
    session_id as make_session_id,
    slugify,
    voices_dir,
)

try:  # POSIX only; the PoC is Linux, but importing must not break a port.
    import fcntl
except ImportError:  # pragma: no cover - not reachable on this machine
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "SCHEMA_VERSION",
    "SESSION_FILENAME",
    "STATES",
    "TRANSITIONS",
    "NoSpaceError",
    "SchemaError",
    "Segment",
    "Session",
    "SessionState",
    "Spool",
    "StateError",
    "now_local",
    "to_iso",
    "from_iso",
]

SCHEMA_VERSION = 1

SESSION_FILENAME = "session.json"

#: One writer at a time per session directory, so the daemon appending a segment
#: and the worker queueing the same session cannot interleave a read-modify-write.
LOCK_FILENAME = ".lock"

SessionState = Literal[
    "recording", "ending", "captured", "pending", "transcribing", "done", "failed"
]

STATES: tuple[str, ...] = (
    "recording",
    "ending",
    "captured",
    "pending",
    "transcribing",
    "done",
    "failed",
)

#: ``(from, to) -> writer``. The only legal transitions, and who may write them.
TRANSITIONS: dict[tuple[str | None, str], str] = {
    (None, "recording"): "munin-rec",
    ("recording", "ending"): "munin-rec",
    ("ending", "recording"): "munin-rec",
    ("recording", "captured"): "munin-rec",
    ("ending", "captured"): "munin-rec",
    ("recording", "failed"): "munin-rec",
    ("ending", "failed"): "munin-rec",
    # D15: resume reopens a session the worker has not started on.
    ("captured", "recording"): "munin-rec",
    ("pending", "recording"): "munin-rec",
    ("captured", "pending"): "munin-work",
    ("pending", "transcribing"): "munin-work",
    ("transcribing", "pending"): "munin-work",
    ("transcribing", "done"): "munin-work",
    ("pending", "failed"): "munin-work",
    ("transcribing", "failed"): "munin-work",
}

#: States the daemon may find after its own crash, and what they become.
DAEMON_STATES: frozenset[str] = frozenset({"recording", "ending"})


class StateError(RuntimeError):
    """An illegal transition, or one attempted by the wrong process."""


class NoSpaceError(StateError):
    """Free space is below ``capture.min_free_mb``; refuse rather than truncate."""


class SchemaError(RuntimeError):
    """A ``session.json`` this build must not guess at."""


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def now_local() -> datetime:
    """Local wall clock, timezone-aware. Never naive (contracts section 2)."""
    return datetime.now().astimezone()


def to_iso(value: datetime | None) -> str | None:
    """ISO 8601 with a local offset, second precision. ``None`` passes through.

    Never ``Z``-normalised: the directory name is local time and the two have to
    agree on which day a late meeting belongs to.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.replace(microsecond=0).isoformat()


def from_iso(value: str | None) -> datetime | None:
    """Parse what :func:`to_iso` wrote. A naive string gets the local offset."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _write_atomic(path: Path, text: str) -> None:
    """tmp + fsync + rename, in the same directory so rename stays atomic.

    A crash leaves either the previous file or the new one, plus possibly a
    ``.tmp`` nobody reads -- never a half-written ``session.json``.
    """
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _replace_symlink(link: Path, target: str) -> None:
    """Point ``link`` at ``target`` atomically, creating the parent if needed."""
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(link.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, link)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass
class Segment:
    """One capture segment. Filenames come from ``capture.base.segment_filenames``."""

    index: int
    mic: str
    app: str
    started_at: datetime
    stopped_at: datetime | None = None
    duration_seconds: float | None = None
    #: What the app track actually recorded. ``"stream"`` is the meeting
    #: application's own audio and nothing else. ``"sink-monitor"`` is the whole
    #: desktop mix -- every other application, every notification sound -- taken
    #: because the application's stream could not be bound. ``"silent"`` is a
    #: generated silent track (an ad-hoc recording with no application).
    #:
    #: This is on the record because the three are not interchangeable: a
    #: sink-monitor track may contain audio from people and applications that
    #: were never part of the meeting, which is a GDPR and ISO 27001 question
    #: about what was captured, not a capture-quality detail (spec 12).
    app_source: str | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "mic": self.mic,
            "app": self.app,
            "started_at": to_iso(self.started_at),
            "stopped_at": to_iso(self.stopped_at),
            "duration_seconds": self.duration_seconds,
            "app_source": self.app_source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Segment":
        started = from_iso(data.get("started_at"))
        if started is None:
            raise SchemaError(f"segment {data.get('index')!r} has no started_at")
        return cls(
            index=int(data["index"]),
            mic=str(data["mic"]),
            app=str(data["app"]),
            started_at=started,
            stopped_at=from_iso(data.get("stopped_at")),
            duration_seconds=data.get("duration_seconds"),
            app_source=data.get("app_source"),
        )


@dataclass
class Session:
    """One meeting on disk. Satisfies ``munin.capture.base.SessionRef``."""

    id: str
    directory: Path
    state: str
    title: str
    slug: str
    source: str  # adhoc | detected
    platform: str
    capture_method: str | None = None
    host: str | None = None
    munin_version: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    duration_seconds: float | None = None
    app: dict | None = None
    calendar_event_id: None = None
    segments: list[Segment] = field(default_factory=list)
    checksums: dict[str, str] = field(default_factory=dict)
    pending_reason: str | None = None
    error: dict | None = None
    transcript: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    #: Keys written by a newer build, kept so a round trip does not lose them.
    extra: dict = field(default_factory=dict)

    @property
    def json_path(self) -> Path:
        """``<directory>/session.json``."""
        return self.directory / SESSION_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.directory / LOCK_FILENAME

    @property
    def audio_duration_seconds(self) -> float:
        """Captured-audio time: the segments summed, not wall clock.

        A break between segments is a gap line in the transcript, never silence
        in the timeline (contracts section 10).
        """
        return sum(s.duration_seconds or 0.0 for s in self.segments)

    @property
    def next_segment_index(self) -> int:
        return len(self.segments) + 1

    def to_dict(self) -> dict:
        """The on-disk shape, schema version 1."""
        data = {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "state": self.state,
            "title": self.title,
            "slug": self.slug,
            "source": self.source,
            "platform": self.platform,
            "capture_method": self.capture_method,
            "host": self.host,
            "munin_version": self.munin_version,
            "created_at": to_iso(self.created_at),
            "started_at": to_iso(self.started_at),
            "stopped_at": to_iso(self.stopped_at),
            "duration_seconds": self.duration_seconds,
            "app": self.app,
            "calendar_event_id": self.calendar_event_id,
            "segments": [s.to_dict() for s in self.segments],
            "checksums": dict(self.checksums),
            "pending_reason": self.pending_reason,
            "error": self.error,
            "transcript": dict(self.transcript) or {
                "txt": None,
                "json": None,
                "pensieve_copy": None,
            },
            "history": [dict(entry) for entry in self.history],
        }
        for key, value in self.extra.items():
            data.setdefault(key, value)
        return data

    @classmethod
    def from_dict(cls, data: dict, directory: Path) -> "Session":
        """Parse ``session.json``. Refuses a schema version above this build's."""
        version = data.get("schema_version")
        if not isinstance(version, int):
            raise SchemaError(f"{directory}: session.json has no schema_version")
        if version > SCHEMA_VERSION:
            raise SchemaError(
                f"{directory}: session.json is schema version {version}, this build "
                f"understands {SCHEMA_VERSION}; refusing to guess"
            )
        state = data.get("state")
        if state not in STATES:
            raise SchemaError(f"{directory}: unknown state {state!r}")
        known = {
            "schema_version",
            "id",
            "state",
            "title",
            "slug",
            "source",
            "platform",
            "capture_method",
            "host",
            "munin_version",
            "created_at",
            "started_at",
            "stopped_at",
            "duration_seconds",
            "app",
            "calendar_event_id",
            "segments",
            "checksums",
            "pending_reason",
            "error",
            "transcript",
            "history",
        }
        return cls(
            id=str(data["id"]),
            directory=directory,
            state=str(state),
            title=str(data.get("title") or data["id"]),
            slug=str(data.get("slug") or ""),
            source=str(data.get("source") or "adhoc"),
            platform=str(data.get("platform") or sys.platform),
            capture_method=data.get("capture_method"),
            host=data.get("host"),
            munin_version=data.get("munin_version"),
            created_at=from_iso(data.get("created_at")),
            started_at=from_iso(data.get("started_at")),
            stopped_at=from_iso(data.get("stopped_at")),
            duration_seconds=data.get("duration_seconds"),
            app=data.get("app"),
            calendar_event_id=data.get("calendar_event_id"),
            segments=[Segment.from_dict(row) for row in data.get("segments", [])],
            checksums=dict(data.get("checksums") or {}),
            pending_reason=data.get("pending_reason"),
            error=data.get("error"),
            transcript=dict(data.get("transcript") or {}),
            history=[dict(entry) for entry in data.get("history", [])],
            extra={k: v for k, v in data.items() if k not in known},
        )

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        """Write ``session.json`` atomically (tmp + rename in the same directory)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        _write_atomic(
            self.json_path,
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )

    def reload(self) -> "Session":
        """Re-read this session from disk. The file, not memory, is the record."""
        return read_session(self.directory)

    @contextmanager
    def lock(self, *, blocking: bool = True) -> Iterator[None]:
        """Hold the per-session write lock.

        An advisory ``flock`` on ``<directory>/.lock``. Both processes take it
        around a read-modify-write of ``session.json``; the ownership rule keeps
        them apart by state, and this keeps them apart in the one place the rule
        does not reach (a resume racing the worker's first look at the session).
        """
        if fcntl is None:  # pragma: no cover - Linux only in the PoC
            yield
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def update(self, **fields) -> None:
        """Set fields and save, without changing state. Used inside a segment."""
        with self.lock():
            self._apply(fields)
            self.save()

    def _apply(self, fields: dict) -> None:
        for key, value in fields.items():
            if not hasattr(self, key):
                raise TypeError(f"session has no field {key!r}")
            if key in {"state", "history", "id", "directory"}:
                raise TypeError(f"{key!r} is not settable this way")
            setattr(self, key, value)

    def transition(self, to: str, *, by: str, note: str | None = None, **fields) -> None:
        """Move to ``to``, appending to ``history[]`` and saving.

        Raises :class:`StateError` if the transition is not in :data:`TRANSITIONS`
        or if ``by`` is not the process that owns it.
        """
        if to not in STATES:
            raise StateError(f"{to!r} is not a session state")
        key = (self.state, to)
        writer = TRANSITIONS.get(key)
        if writer is None:
            raise StateError(
                f"{self.id}: {self.state} -> {to} is not a legal transition"
            )
        if writer != by:
            raise StateError(
                f"{self.id}: {self.state} -> {to} is written by {writer}, not {by!r} "
                "(spec 11: after captured, only the worker writes state)"
            )
        with self.lock():
            if self.json_path.exists():
                on_disk = read_session(self.directory)
                if on_disk.state != self.state:
                    raise StateError(
                        f"{self.id}: session.json says {on_disk.state!r} but this "
                        f"process holds {self.state!r}; refusing to overwrite"
                    )
                self.history = on_disk.history
            at = now_local()
            self._apply(fields)
            entry: dict = {
                "at": to_iso(at),
                "from": self.state,
                "to": to,
                "by": by,
            }
            if note:
                entry["note"] = note
            self.state = to
            self.history.append(entry)
            self.save()


def read_session(directory: Path) -> Session:
    """Load one ``session.json`` from a directory."""
    path = directory / SESSION_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"{path} does not exist") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{path} is not valid JSON: {exc}") from exc
    return Session.from_dict(data, directory)


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


class Spool:
    """The session store rooted at ``config.home``."""

    def __init__(self, config: Config) -> None:
        self.config = config

    # -- layout ------------------------------------------------------------

    @property
    def home(self) -> Path:
        return self.config.home

    @property
    def recordings(self) -> Path:
        return recordings_root(self.home, self.config.paths.recordings)

    @property
    def inbox(self) -> Path:
        return inbox_dir(self.home, self.config.paths.inbox)

    @property
    def voices(self) -> Path:
        return voices_dir(self.home, self.config.paths.voices)

    @property
    def latest_path(self) -> Path:
        return latest_link(self.home)

    def ensure_dirs(self) -> None:
        """Create the data root's directories. Idempotent; ``munin setup`` calls it."""
        for path in (self.recordings, self.inbox, self.voices):
            path.mkdir(parents=True, exist_ok=True)

    # -- creating ----------------------------------------------------------

    def create(
        self,
        *,
        title: str | None,
        source: str,
        app: dict | None = None,
        now: datetime | None = None,
        capture_method: str | None = None,
    ) -> Session:
        """Make the directory, write the first ``session.json``, update ``latest``.

        Raises :class:`StateError` if free space is below ``capture.min_free_mb``
        (spec 11: refuse rather than truncate).
        """
        if source not in ("adhoc", "detected"):
            raise ValueError(f"source must be adhoc or detected, got {source!r}")
        created = now or now_local()
        if created.tzinfo is None:
            created = created.astimezone()

        self.ensure_dirs()
        free = self.free_mb()
        minimum = self.config.capture.min_free_mb
        if free < minimum:
            raise NoSpaceError(
                f"{free} MB free below {self.recordings}, {minimum} MB required; "
                "refusing to start a recording that would truncate"
            )

        resolved_title = title.strip() if title and title.strip() else None
        if resolved_title is None:
            label = (app or {}).get("label")
            clock = f"{created:%H:%M}"
            resolved_title = f"{label} {clock}" if label else f"Meeting {clock}"

        slug = slugify(resolved_title)
        sid = make_session_id(created, resolved_title)
        directory = session_dir(
            self.home, created, sid, self.config.paths.recordings
        )
        suffix = 1
        while directory.exists():
            suffix += 1
            slug = f"{slugify(resolved_title)}-{suffix}"
            sid = f"{created:%Y-%m-%dT%H%M}-{slug}"
            directory = session_dir(
                self.home, created, sid, self.config.paths.recordings
            )
        directory.mkdir(parents=True)

        session = Session(
            id=sid,
            directory=directory,
            state="recording",
            title=resolved_title,
            slug=slug,
            source=source,
            platform=sys.platform,
            capture_method=capture_method,
            host=_socket.gethostname(),
            munin_version=munin.__version__,
            created_at=created,
            app=app,
            transcript={"txt": None, "json": None, "pensieve_copy": None},
            history=[
                {
                    "at": to_iso(created),
                    "from": None,
                    "to": "recording",
                    "by": TRANSITIONS[(None, "recording")],
                }
            ],
        )
        session.save()
        self.update_latest(session)
        return session

    # -- reading -----------------------------------------------------------

    def load(self, session_id: str) -> Session:
        """Load one session by id."""
        session = self.find(session_id)
        if session is None:
            raise SchemaError(f"no session {session_id!r} under {self.recordings}")
        return session

    def find(self, session_id: str) -> Session | None:
        """One session by exact id, or ``None``. Cheap: the id names the path."""
        # "2026-09-14T1325-<slug>": the first 15 characters place the directory.
        try:
            created = datetime.strptime(session_id[:15], "%Y-%m-%dT%H%M")
        except ValueError:
            return None
        directory = session_dir(
            self.home, created, session_id, self.config.paths.recordings
        )
        if not (directory / SESSION_FILENAME).exists():
            return None
        return read_session(directory)

    def iter_sessions(self, *, limit: int | None = None) -> Iterator[Session]:
        """Newest first, by session id (which sorts chronologically).

        A directory whose ``session.json`` cannot be parsed is skipped rather
        than allowed to break ``munin list`` -- it stays on disk and ``munin
        doctor`` is where it should surface.
        """
        yielded = 0
        for directory in self._session_dirs():
            try:
                session = read_session(directory)
            except SchemaError:
                continue
            yield session
            yielded += 1
            if limit is not None and yielded >= limit:
                return

    def _session_dirs(self) -> list[Path]:
        """Every ``recordings/YYYY/MM/<id>``, newest id first."""
        if not self.recordings.is_dir():
            return []
        found: list[Path] = []
        for year in sorted(self.recordings.iterdir(), reverse=True):
            if not year.is_dir():
                continue
            for month in sorted(year.iterdir(), reverse=True):
                if not month.is_dir():
                    continue
                for entry in month.iterdir():
                    if entry.is_dir() and (entry / SESSION_FILENAME).exists():
                        found.append(entry)
        found.sort(key=lambda p: p.name, reverse=True)
        return found

    def latest(self) -> Session | None:
        """The newest session, or ``None`` on an empty spool."""
        return next(self.iter_sessions(limit=1), None)

    def active(self) -> Session | None:
        """The session currently being captured, if any.

        The daemon asks this before honouring a second ``start``: a duplicate is
        a no-op that reports the running session, not a second directory.
        """
        for session in self.iter_sessions():
            if session.state in DAEMON_STATES:
                return session
        return None

    def sessions_in_state(self, *states: str) -> list[Session]:
        """Every session in any of ``states``, newest first."""
        wanted = set(states)
        return [s for s in self.iter_sessions() if s.state in wanted]

    def resumable(self, *, now: datetime | None = None) -> Session | None:
        """The newest session still inside ``resume_window_seconds`` (D15).

        Only a session in ``captured`` or ``pending`` qualifies; once the worker
        has started, a resume must open a fresh session instead.
        """
        moment = now or now_local()
        if moment.tzinfo is None:
            moment = moment.astimezone()
        window = timedelta(seconds=self.config.detection.resume_window_seconds)
        for session in self.iter_sessions():
            if session.state not in ("captured", "pending"):
                continue
            if session.stopped_at is None:
                continue
            if moment - session.stopped_at <= window:
                return session
            return None
        return None

    # -- segments ----------------------------------------------------------

    def add_segment(
        self, session: Session, index: int, *, started_at: datetime | None = None
    ) -> Segment:
        """Append a segment record, with filenames from the capture layer."""
        expected = session.next_segment_index
        if index != expected:
            raise StateError(
                f"{session.id}: segments are 1-based and contiguous; next is "
                f"{expected}, got {index}"
            )
        mic, app = segment_filenames(index)
        segment = Segment(
            index=index,
            mic=mic,
            app=app,
            started_at=started_at or now_local(),
        )
        with session.lock():
            session.segments.append(segment)
            if session.started_at is None:
                # The session started when its first track did, not when the
                # directory was made: `created_at` is the name, this is the clock
                # the bar counts up from.
                session.started_at = segment.started_at
            session.save()
        return segment

    def complete_segment(
        self,
        session: Session,
        index: int,
        *,
        stopped_at: datetime,
        duration_seconds: float,
    ) -> Segment:
        """Close the open segment and roll the session's duration forward."""
        for segment in session.segments:
            if segment.index == index:
                break
        else:
            raise StateError(f"{session.id}: no segment {index}")
        with session.lock():
            segment.stopped_at = stopped_at
            segment.duration_seconds = duration_seconds
            session.stopped_at = stopped_at
            session.duration_seconds = session.audio_duration_seconds
            session.save()
        return segment

    # -- the inbox ---------------------------------------------------------

    def link_inbox(self, session: Session) -> Path:
        """Create ``inbox/<id>`` as a relative symlink to the session directory."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        link = self.inbox / session.id
        target = os.path.relpath(session.directory, self.inbox)
        _replace_symlink(link, target)
        return link

    def unlink_inbox(self, session: Session) -> None:
        """Remove the inbox symlink. Tolerates it being absent or broken."""
        link = self.inbox / session.id
        try:
            link.unlink()
        except FileNotFoundError:
            pass

    def inbox_ids(self) -> list[str]:
        """What the inbox indexes, sorted. Broken links count -- they are the point."""
        if not self.inbox.is_dir():
            return []
        return sorted(entry.name for entry in self.inbox.iterdir())

    def queue_depth(self) -> int:
        """How many sessions sit in ``inbox/``. Shown in the bar."""
        return len(self.inbox_ids())

    def pending_sessions(self) -> list[Session]:
        """What the worker should look at: the inbox, falling back to a scan.

        The symlink is an index, not the record (contracts section 2), so a
        missing or broken ``inbox/`` costs speed and nothing else.
        """
        found: dict[str, Session] = {}
        for sid in self.inbox_ids():
            session = self.find(sid)
            if session is not None:
                found[session.id] = session
        for session in self.iter_sessions():
            if session.state in ("captured", "pending", "transcribing"):
                found.setdefault(session.id, session)
        return sorted(found.values(), key=lambda s: s.id)

    def update_latest(self, session: Session) -> Path:
        """Point ``latest`` at this session, atomically and relatively."""
        target = os.path.relpath(session.directory, self.home)
        _replace_symlink(self.latest_path, target)
        return self.latest_path

    # -- facts about files -------------------------------------------------

    def checksum(self, path: Path) -> str:
        """``sha256:<hex>`` of one file."""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return f"sha256:{digest.hexdigest()}"

    def checksum_segments(self, session: Session) -> dict[str, str]:
        """Checksums for every segment file that exists, keyed by filename."""
        sums: dict[str, str] = {}
        for segment in session.segments:
            for name in (segment.mic, segment.app):
                path = session.directory / name
                if path.is_file():
                    sums[name] = self.checksum(path)
        return sums

    def free_mb(self) -> int:
        """Free space on the recordings filesystem, in MiB."""
        probe = self.recordings
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free // (1024 * 1024)

    def has_space(self) -> bool:
        return self.free_mb() >= self.config.capture.min_free_mb

    # -- crash recovery (spec 11) ------------------------------------------

    def recover_for_daemon(self) -> list[Session]:
        """What munin-rec does at start with sessions its last life left open.

        A session still marked ``recording`` or ``ending`` means the daemon died
        mid-capture. Opus is a streaming format, so whatever reached the disk is
        playable: the session becomes ``captured`` with the reason in
        ``history[]`` and an inbox link, so the worker picks it up rather than
        the audio being stranded. A session with no segment files at all has
        nothing to keep and goes to ``failed`` instead.
        """
        recovered: list[Session] = []
        for session in self.sessions_in_state(*DAEMON_STATES):
            note = "recovered at daemon start; the previous munin-rec did not stop it"
            has_audio = any(
                (session.directory / name).is_file()
                for segment in session.segments
                for name in (segment.mic, segment.app)
            )
            at = now_local()
            if has_audio:
                for segment in session.segments:
                    if segment.stopped_at is None:
                        segment.stopped_at = session.stopped_at or at
                session.checksums = self.checksum_segments(session)
                session.transition(
                    "captured",
                    by="munin-rec",
                    note=note,
                    stopped_at=session.stopped_at or at,
                    duration_seconds=session.audio_duration_seconds,
                )
                self.link_inbox(session)
            else:
                session.transition(
                    "failed",
                    by="munin-rec",
                    note=note,
                    error={
                        "code": "interrupted",
                        "message": "munin-rec stopped before any audio was written",
                        "at": to_iso(at),
                    },
                )
            recovered.append(session)
        return recovered

    def recover_for_worker(self) -> list[Session]:
        """Spec 11: a session left ``transcribing`` is reset to ``pending``.

        The worker died mid-file. Nothing downstream trusts a partial transcript,
        so the session goes back in the queue with the reason recorded.
        """
        recovered: list[Session] = []
        for session in self.sessions_in_state("transcribing"):
            session.transition(
                "pending",
                by="munin-work",
                note="reset at worker start after an interrupted transcription",
                pending_reason="requeued after the worker was interrupted",
            )
            self.link_inbox(session)
            recovered.append(session)
        return recovered
