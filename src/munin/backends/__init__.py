"""Backend registry.

FROZEN: see docs/superpowers/specs/2026-09-14-poc-contracts.md, section 10.
"""

from __future__ import annotations

from munin.backends.base import (
    Backend,
    BackendUnavailable,
    Transcript,
    TranscriptSegment,
)

__all__ = [
    "Backend",
    "BackendUnavailable",
    "Transcript",
    "TranscriptSegment",
    "get_backend",
]


def get_backend(name: str) -> Backend:
    """Return the named backend. The PoC knows only ``none``."""
    if name == "none":
        from munin.backends.none import NoneBackend

        return NoneBackend()
    raise NotImplementedError(f"backend {name!r} is not implemented in this build")
