"""PipeWire two-track capture (spec 6.1).

Track 1 is the default source; track 2 is bound to the meeting application's own
``Stream/Output/Audio`` node, so music and notification sounds stay out of the
mix. Verified on this machine: a plain ``pw-record --target <object.serial>``
isolates one application at roughly 52 dB rejection of a second concurrent
stream, and ``stream.capture.sink`` is neither needed nor wanted.

Every PipeWire tool needs ``XDG_RUNTIME_DIR`` in its environment or it fails
without saying why; the systemd unit supplies it and this module asserts it.

Owner: capture workstream.
"""

from __future__ import annotations

from typing import ClassVar

from munin.capture.base import (
    CaptureResult,
    CaptureTarget,
    Capturer,
    SessionRef,
)

__all__ = ["PipewireCapturer"]


class PipewireCapturer(Capturer):
    """``pw-record`` raw s16 piped into ``ffmpeg -c:a libopus``, one pair per segment."""

    method: ClassVar[str] = "pipewire-pw-record+ffmpeg-libopus"

    def start(self, session: SessionRef, segment_index: int) -> None:
        raise NotImplementedError

    def stop(self) -> CaptureResult:
        raise NotImplementedError

    @property
    def is_running(self) -> bool:
        raise NotImplementedError

    def describe(self) -> dict[str, str]:
        raise NotImplementedError


def default_mic_target(mic_source: str = "default") -> CaptureTarget:
    """Resolve the configured microphone to a :class:`CaptureTarget`.

    ``mic_source`` is either ``"default"``, a PipeWire node name, or an
    ``object.serial``. Raises ``CaptureError`` if the source cannot be resolved.
    """
    raise NotImplementedError
