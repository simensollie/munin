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

import argparse
import json
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from munin.backends import get_backend
from munin.backends.base import BackendUnavailable
from munin.config import Config, load as load_config
from munin.pipeline import run as run_pipeline
from munin.pipeline.render import adjust_segments, pensieve_filename, render_transcript
from munin.spool import Segment, Session, Spool, StateError

__all__ = ["Worker", "main"]

#: Written to pending_reason when a crash-recovered session is reset (spec 11).
STALE_TRANSCRIBING_REASON = "worker restarted while this session was transcribing"

#: Default poll interval for the ``munin worker`` loop, seconds. Overridable
#: with ``--interval``; not part of the frozen contract, so free to pick.
DEFAULT_INTERVAL_SECONDS = 5.0


def _now_iso() -> str:
    """ISO 8601, local offset, second precision -- contracts section 2."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _compute_gaps(segments: Sequence[Segment]) -> list[tuple[float, float]]:
    """Wall-clock breaks between capture segments, as ``(position, gap_seconds)``.

    ``position`` is the point on the captured-audio timeline -- the summed
    duration of every earlier segment -- where the break belongs (contracts
    section 10: "segment k's times are offset by the summed duration of
    segments 1...k-1"). A session with one segment (the common PoC case, no
    resume) has no gaps and this returns ``[]``. Segments with an unknown
    ``duration_seconds`` or timestamp are skipped rather than raising, since a
    partially-written ``session.json`` should degrade the transcript, not crash
    the worker.
    """
    gaps: list[tuple[float, float]] = []
    cumulative = 0.0
    ordered = sorted(segments, key=lambda s: s.index)
    for previous, current in zip(ordered, ordered[1:]):
        if previous.duration_seconds is not None:
            cumulative += previous.duration_seconds
        if previous.stopped_at is not None and current.started_at is not None:
            gap_seconds = (current.started_at - previous.stopped_at).total_seconds()
            if gap_seconds > 0:
                gaps.append((cumulative, gap_seconds))
    return gaps


class Worker:
    """Drains one spool."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.spool = Spool(config)

    def recover(self) -> int:
        """Reset every ``transcribing`` session to ``pending``. Returns the count."""
        count = 0
        for session in self.spool.iter_sessions():
            if session.state == "transcribing":
                session.transition(
                    "pending",
                    by="munin-work",
                    pending_reason=STALE_TRANSCRIBING_REASON,
                )
                count += 1
        return count

    def claim(self, session: Session) -> None:
        """``captured`` -> ``pending``: the worker has seen it and queued it."""
        if session.state != "captured":
            raise StateError(f"cannot claim a session in state {session.state!r}")
        session.transition("pending", by="munin-work")

    def process(self, session: Session) -> None:
        """Run the backend chain for one session.

        ``BackendUnavailable`` leaves the session ``pending`` with
        ``pending_reason`` set and the inbox symlink in place -- visible, not lost.
        Any other exception is treated as unrecoverable: the session goes to
        ``failed`` with the error recorded, and its audio is retained (nobody
        deletes anything in the PoC).
        """
        if session.state != "pending":
            raise StateError(f"cannot process a session in state {session.state!r}")

        backend = get_backend(self.config.transcribe.backend)
        session.transition("transcribing", by="munin-work")

        try:
            transcript = run_pipeline(session, backend)
            self._write_transcript(session, transcript)
        except BackendUnavailable as exc:
            session.transition("pending", by="munin-work", pending_reason=exc.reason)
        except Exception as exc:
            # Covers both a failing backend and a failure while writing the
            # transcript out (disk full, unwritable pensieve dir, ...): either
            # way the session is unrecoverable without intervention, and its
            # audio is retained rather than deleted (spec 11).
            session.transition(
                "failed",
                by="munin-work",
                error={"code": "internal", "message": str(exc), "at": _now_iso()},
            )

    def _write_transcript(self, session: Session, transcript) -> None:
        """``transcribing`` -> ``done``: render, write the sidecar, copy to
        pensieve, then drop the inbox symlink."""
        gaps = _compute_gaps(session.segments)
        adjusted, adjustments = adjust_segments(transcript.segments)
        text = render_transcript(transcript, gaps=gaps)

        txt_path = Path(session.directory) / "transcript.txt"
        txt_path.write_text(text, encoding="utf-8")

        json_path = Path(session.directory) / "transcript.json"
        payload = {
            "schema_version": 1,
            "backend": transcript.backend,
            "language": transcript.language,
            "model": transcript.model,
            "segments": [
                {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text}
                for s in adjusted
            ],
            "words": transcript.words,
            "gaps": [{"at_seconds": at, "gap_seconds": gap} for at, gap in gaps],
            "adjustments": adjustments,
        }
        json_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        pensieve_path: Path | None = None
        if self.config.pensieve_raw_dir is not None:
            pensieve_dir = Path(self.config.pensieve_raw_dir).expanduser()
            pensieve_dir.mkdir(parents=True, exist_ok=True)
            pensieve_path = pensieve_dir / pensieve_filename(session.title)
            pensieve_path.write_text(text, encoding="utf-8")

        session.transition(
            "done",
            by="munin-work",
            transcript={
                "txt": str(txt_path),
                "json": str(json_path),
                "pensieve_copy": str(pensieve_path) if pensieve_path else None,
            },
        )
        self.spool.unlink_inbox(session)

    def drain(self, *, once: bool = True) -> int:
        """Process everything claimable. Returns how many sessions were touched.

        A single call does one full sweep: every ``captured`` session is
        claimed, then every ``pending`` session (including ones just claimed)
        is processed. ``once`` is accepted for symmetry with the CLI's
        ``--once`` flag and is not otherwise used here -- looping across
        sweeps, on an interval, is :func:`main`'s job, not this method's. See
        this workstream's deviation notes for why that split was chosen over a
        sleep loop inside ``drain`` itself.
        """
        del once  # see docstring
        touched = 0
        for session in list(self.spool.iter_sessions()):
            if session.state == "captured":
                self.claim(session)
                touched += 1
        for session in list(self.spool.iter_sessions()):
            if session.state == "pending":
                self.process(session)
                touched += 1
        return touched


def _install_stop_handler() -> "list[bool]":
    """Returns a one-element mutable flag, set True on SIGTERM/SIGINT.

    A list rather than a plain bool so the closure below can mutate it without
    a ``nonlocal`` across the signal boundary.
    """
    flag = [False]

    def _handle(signum: int, frame: object) -> None:
        flag[0] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    return flag


def main(argv: list[str] | None = None) -> int:
    """``munin-work`` entry point. Also reached as ``munin worker``."""
    parser = argparse.ArgumentParser(prog="munin-work")
    parser.add_argument("--once", action="store_true", help="drain once and exit")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="seconds between sweeps when not --once",
    )
    args = parser.parse_args(argv)

    config = load_config()
    worker = Worker(config)
    worker.recover()  # spec 11: crash recovery happens once, before anything else

    if args.once:
        worker.drain(once=True)
        return 0

    stop = _install_stop_handler()
    # Sleep in short slices so a SIGTERM lands within a fraction of a second
    # rather than at the end of a possibly long --interval.
    slice_seconds = 0.5
    while not stop[0]:
        worker.drain(once=False)
        remaining = args.interval
        while remaining > 0 and not stop[0]:
            time.sleep(min(slice_seconds, remaining))
            remaining -= slice_seconds
    return 0
