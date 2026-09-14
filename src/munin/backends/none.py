"""The no-op backend: the PoC's configured backend.

Everything except capture is deferred, so a captured session must come to rest
somewhere visible rather than disappearing. This backend refuses, with a reason
the bar and ``munin list`` both show.

Owner: worker workstream. Signature fixed by PoC contracts section 10.
"""

from __future__ import annotations

from munin.backends.base import BackendUnavailable, Transcript
from munin.capture.base import SessionRef

__all__ = ["NoneBackend", "REASON"]

#: The PoC's visible end state. Part of the contract; do not reword.
REASON = "no transcription backend configured"


class NoneBackend:
    """Never transcribes, always explains why."""

    name = "none"

    def available(self) -> bool:
        return False

    def transcribe(self, session: SessionRef) -> Transcript:
        raise BackendUnavailable(REASON)
