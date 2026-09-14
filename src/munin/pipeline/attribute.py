"""Speaker attribution: the four-tier fallback (you / enrolled / invited /
unknown). See spec section 7.4.

Not implemented in the PoC -- no diarization or voice matching runs, so there
is nothing to attribute yet.
"""

from __future__ import annotations

__all__ = ["attribute_speakers"]


def attribute_speakers(*args: object, **kwargs: object) -> object:
    raise NotImplementedError("speaker attribution is not implemented in the PoC; see spec 7.4")
