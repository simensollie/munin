"""Speech-to-text: the resident Whisper models. See spec section 7.2.

Not implemented in the PoC -- no ASR model is downloaded or run.
"""

from __future__ import annotations

__all__ = ["transcribe_audio"]


def transcribe_audio(*args: object, **kwargs: object) -> object:
    raise NotImplementedError("ASR is not implemented in the PoC; see spec 7.2")
