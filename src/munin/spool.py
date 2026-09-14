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

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Literal

from munin.config import Config

__all__ = [
    "SCHEMA_VERSION",
    "STATES",
    "TRANSITIONS",
    "Segment",
    "Session",
    "SessionState",
    "Spool",
    "StateError",
]

SCHEMA_VERSION = 1

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


class StateError(RuntimeError):
    """An illegal transition, or one attempted by the wrong process."""


@dataclass
class Segment:
    """One capture segment. Filenames come from ``capture.base.segment_filenames``."""

    index: int
    mic: str
    app: str
    started_at: datetime
    stopped_at: datetime | None = None
    duration_seconds: float | None = None


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

    @property
    def json_path(self) -> Path:
        """``<directory>/session.json``."""
        raise NotImplementedError

    def to_dict(self) -> dict:
        """The on-disk shape, schema version 1."""
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data: dict, directory: Path) -> "Session":
        """Parse ``session.json``. Refuses a schema version above this build's."""
        raise NotImplementedError

    def save(self) -> None:
        """Write ``session.json`` atomically (tmp + rename in the same directory)."""
        raise NotImplementedError

    def transition(self, to: str, *, by: str, **fields) -> None:
        """Move to ``to``, appending to ``history[]`` and saving.

        Raises :class:`StateError` if the transition is not in :data:`TRANSITIONS`
        or if ``by`` is not the process that owns it.
        """
        raise NotImplementedError


class Spool:
    """The session store rooted at ``config.home``."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def create(
        self,
        *,
        title: str | None,
        source: str,
        app: dict | None = None,
        now: datetime | None = None,
    ) -> Session:
        """Make the directory, write the first ``session.json``, update ``latest``.

        Raises :class:`StateError` if free space is below ``capture.min_free_mb``
        (spec 11: refuse rather than truncate).
        """
        raise NotImplementedError

    def load(self, session_id: str) -> Session:
        """Load one session by id."""
        raise NotImplementedError

    def iter_sessions(self, *, limit: int | None = None) -> Iterator[Session]:
        """Newest first, by session id (which sorts chronologically)."""
        raise NotImplementedError

    def latest(self) -> Session | None:
        """The newest session, or ``None`` on an empty spool."""
        raise NotImplementedError

    def resumable(self, *, now: datetime | None = None) -> Session | None:
        """The newest session still inside ``resume_window_seconds`` (D15).

        Only a session in ``captured`` or ``pending`` qualifies; once the worker
        has started, a resume must open a fresh session instead.
        """
        raise NotImplementedError

    def add_segment(self, session: Session, index: int) -> Segment:
        """Append a segment record, with filenames from the capture layer."""
        raise NotImplementedError

    def link_inbox(self, session: Session) -> Path:
        """Create ``inbox/<id>`` as a relative symlink to the session directory."""
        raise NotImplementedError

    def unlink_inbox(self, session: Session) -> None:
        """Remove the inbox symlink. Tolerates it being absent or broken."""
        raise NotImplementedError

    def queue_depth(self) -> int:
        """How many sessions sit in ``inbox/``. Shown in the bar."""
        raise NotImplementedError

    def checksum(self, path: Path) -> str:
        """``sha256:<hex>`` of one file."""
        raise NotImplementedError

    def free_mb(self) -> int:
        """Free space on the recordings filesystem, in MB."""
        raise NotImplementedError
