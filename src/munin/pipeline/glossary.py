"""The domain glossary: built, not hand-written, from pensieve and M365 sources.
See spec section 7.3.

Not implemented in the PoC -- there is no ASR output for a glossary to
correct, and no M365 enrichment.
"""

from __future__ import annotations

__all__ = ["apply_glossary"]


def apply_glossary(*args: object, **kwargs: object) -> object:
    raise NotImplementedError("glossary application is not implemented in the PoC; see spec 7.3")
