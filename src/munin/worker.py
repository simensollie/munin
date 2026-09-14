"""``munin-work``: drain the spool, run the pipeline, write the transcript.

Transcripts arrive late, never missing (spec 11). The worker never deletes a
session; it only moves its state. On start it resets anything left
``transcribing`` back to ``pending``, because that state can only mean a previous
worker died mid-file.

In the PoC the configured backend is ``none``, so every session stops at
``pending`` carrying the reason. That is the intended end state, not a failure.

Owner: worker workstream. Contracts: sections 4 and 10.
"""

from __future__ import annotations

from munin.config import Config
from munin.spool import Session, Spool

__all__ = ["Worker", "main"]


class Worker:
    """Drains one spool."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.spool = Spool(config)

    def recover(self) -> int:
        """Reset every ``transcribing`` session to ``pending``. Returns the count."""
        raise NotImplementedError

    def claim(self, session: Session) -> None:
        """``captured`` -> ``pending``: the worker has seen it and queued it."""
        raise NotImplementedError

    def process(self, session: Session) -> None:
        """Run the backend chain for one session.

        ``BackendUnavailable`` leaves the session ``pending`` with
        ``pending_reason`` set and the inbox symlink in place -- visible, not lost.
        """
        raise NotImplementedError

    def drain(self, *, once: bool = True) -> int:
        """Process everything claimable. Returns how many sessions were touched."""
        raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    """``munin-work`` entry point. Also reached as ``munin worker``."""
    raise NotImplementedError
