"""The voice register: a name and an embedding per person, under
``~/munin/voices/``. See spec section 7.4.1.

Not implemented in the PoC -- there is no embedding model, and naming a
speaker in the PoC's transcript is not possible because no transcript is ever
produced (the configured backend is ``none``).
"""

from __future__ import annotations

__all__ = ["match_voice"]


def match_voice(*args: object, **kwargs: object) -> object:
    raise NotImplementedError("voice matching is not implemented in the PoC; see spec 7.4.1")
