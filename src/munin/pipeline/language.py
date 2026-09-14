"""Language routing: majority vote over three 30-second samples of track 2.

Not implemented in the PoC -- no ASR model is downloaded or run. See spec
section 7.1.
"""

from __future__ import annotations

__all__ = ["detect_language"]


def detect_language(*args: object, **kwargs: object) -> str:
    raise NotImplementedError("language routing is not implemented in the PoC; see spec 7.1")
