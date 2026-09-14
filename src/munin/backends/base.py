"""The transcription backend seam (D3, spec 8).

The recorder never knows who transcribes. One protocol covers a GPU desktop
(``local``), another machine on the LAN (``ssh``) and a shared gateway
(``api``); the PoC ships only ``none``, which exists so that the seam is real
rather than promised.

Diarization and voice matching are always local, whatever produced the words
(D9, spec 8.1). That split lives above this interface, in ``pipeline/``.

FROZEN: see docs/superpowers/specs/2026-09-14-poc-contracts.md, section 10.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from munin.capture.base import SessionRef

__all__ = ["Backend", "BackendUnavailable", "Transcript", "TranscriptSegment"]


class BackendUnavailable(RuntimeError):
    """This backend cannot run. ``reason`` becomes the session's pending_reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TranscriptSegment:
    """One utterance on the session's captured-audio timeline, in seconds."""

    start: float
    end: float
    speaker: str
    text: str


@dataclass(frozen=True)
class Transcript:
    """What a backend returns. ``words`` is the sidecar payload, opaque here."""

    segments: list[TranscriptSegment]
    language: str | None
    backend: str
    model: str | None = None
    words: list[dict] | None = field(default=None)


@runtime_checkable
class Backend(Protocol):
    """Every backend satisfies exactly this."""

    name: str

    def available(self) -> bool:
        """Cheap reachability check. Never raises."""
        ...

    def transcribe(self, session: SessionRef) -> Transcript:
        """Transcribe the session's audio, or raise :class:`BackendUnavailable`."""
        ...
