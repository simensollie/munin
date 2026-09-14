"""The capture interface. Written before any platform implementation (spec 16.5).

Two independent streams per segment: track 1 is the microphone (you), track 2 is
the meeting application's audio (everyone else). They are separate processes
writing separate files so that one failing cannot truncate the other.

Nothing in this module may import a platform module, and nothing above
``munin.capture`` may import one either. ``munin.capture.get_capturer`` is the
only place ``sys.platform`` is consulted.

FROZEN: see docs/superpowers/specs/2026-09-14-poc-contracts.md, section 5.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal, Protocol, runtime_checkable

__all__ = [
    "CaptureError",
    "CaptureResult",
    "CaptureTarget",
    "Capturer",
    "SessionRef",
    "segment_filenames",
]


class CaptureError(RuntimeError):
    """Capture could not start, or produced nothing usable."""


@dataclass(frozen=True)
class CaptureTarget:
    """One end of a capture: a microphone, or one application's audio stream.

    ``handle`` is an opaque platform token. On Linux it is a PipeWire
    ``object.serial`` rendered as a string, or the literal ``"default"`` for the
    default source. No code above ``capture/`` may parse it.
    """

    kind: Literal["mic", "app"]
    handle: str
    label: str
    pid: int | None = None
    app_id: str | None = None


@dataclass(frozen=True)
class CaptureResult:
    """What one segment produced. Both paths are always set (see ``Capturer``)."""

    segment_index: int
    mic_path: Path
    app_path: Path
    started_at: datetime
    stopped_at: datetime
    duration_seconds: float


@runtime_checkable
class SessionRef(Protocol):
    """The slice of a session a capturer is allowed to see.

    Structural on purpose: ``munin.spool.Session`` satisfies it without
    ``capture/`` importing the spool.
    """

    @property
    def id(self) -> str: ...

    @property
    def directory(self) -> Path: ...


def segment_filenames(index: int) -> tuple[str, str]:
    """Return ``(mic_filename, app_filename)`` for a 1-based segment index.

    Segment 1 is ``mic.opus`` / ``app.opus`` (spec 7.5); later segments are
    zero-padded to three digits, ``mic.002.opus`` / ``app.002.opus`` (spec 6.3,
    D15). This is the single implementation of that rule -- no other module may
    construct these names.

    >>> segment_filenames(1)
    ('mic.opus', 'app.opus')
    >>> segment_filenames(2)
    ('mic.002.opus', 'app.002.opus')
    """
    if index < 1:
        raise ValueError(f"segment index is 1-based, got {index}")
    if index == 1:
        return ("mic.opus", "app.opus")
    return (f"mic.{index:03d}.opus", f"app.{index:03d}.opus")


class Capturer(ABC):
    """Captures one session segment as two independent Opus tracks.

    Contract:

    - ``start()`` while :attr:`is_running` raises :class:`CaptureError`.
    - ``stop()`` while not running raises :class:`CaptureError`.
    - ``app`` may be ``None`` (ad-hoc recording with no identified application).
      The app track is then a valid silent file of the same duration, so
      ``segments[]`` is always a pair and the worker never special-cases it.
    - ``stop()`` returns a :class:`CaptureResult` if *either* track is playable,
      and raises :class:`CaptureError` only when neither is.
    """

    #: Frozen identifier written to ``session.json`` as ``capture_method``.
    method: ClassVar[str] = "abstract"

    def __init__(
        self,
        mic: CaptureTarget,
        app: CaptureTarget | None,
        *,
        bitrate_kbps: int = 24,
        channels: int = 1,
        sample_rate: int = 48000,
    ) -> None:
        self.mic = mic
        self.app = app
        self.bitrate_kbps = bitrate_kbps
        self.channels = channels
        self.sample_rate = sample_rate

    @abstractmethod
    def start(self, session: SessionRef, segment_index: int) -> None:
        """Begin writing segment ``segment_index`` into ``session.directory``."""

    @abstractmethod
    def stop(self) -> CaptureResult:
        """Stop both streams, flush the encoders, and describe what was written."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """True between a successful :meth:`start` and :meth:`stop`."""

    @abstractmethod
    def describe(self) -> dict[str, str]:
        """Flat, printable facts for ``munin doctor`` and ``munin status --json``."""
