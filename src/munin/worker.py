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
import logging
import os
import shutil
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from munin import mixdown
from munin.backends import get_backend
from munin.backends.base import BackendUnavailable
from munin.config import Config, load as load_config
from munin.pipeline import run as run_pipeline
from munin.pipeline.render import adjust_segments, render_transcript
from munin.spool import Segment, Session, Spool, StateError

__all__ = ["Worker", "main"]

log = logging.getLogger("munin.worker")

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

    def export(self, session: Session) -> Path | None:
        """Mix the session and copy it to the upload folder. Returns the copy.

        Off unless ``[export] enabled`` is set (D11 calls the Plaud route
        opt-in, and this folder is staged for a third party). The mix itself is
        still on-demand work; what this adds is a standing demand, because a
        folder nobody refills is a folder that silently goes stale -- the PoC's
        observed failure mode was three meetings sitting unexported for a day
        with nothing to say so.

        Exported once, ever. A session is skipped when its file is still in the
        folder *or* when the folder's ledger says it has already carried it
        (``mixdown.LEDGER_DIRNAME``). Absence used to be the only test, which
        made deleting a file you had just uploaded indistinguishable from never
        having exported it: the next sweep put it straight back, and the folder
        refilled itself behind you. Deleting or moving the file is how an upload
        ends, so that has to stick.

        Nothing is written to ``session.json`` -- no field, no state, no history
        row -- because a re-encode is not a capture event (contracts section
        7.2). The ledger lives in the upload folder instead, and goes when the
        folder goes (D25).

        A split parent is skipped with the still-being-written states
        (``mixdown.SKIP_STATES``): it holds both meetings, its halves hold one
        each, and the halves are what an upload wants (D26).

        What is given up is self-healing: a file deleted by accident no longer
        comes back on the next sweep. ``munin mix <session> --force`` puts it
        back deliberately, which is the right way round -- a folder that
        recreates files you removed is the louder failure.

        Never raises. A failure here must not take munin-work down or stop a
        transcript being written; it logs and the next sweep tries again. The
        marker is written only after the copy lands, so a failed export still
        retries.
        """
        if not self.config.export.enabled:
            return None
        if session.state in mixdown.SKIP_STATES:
            return None
        fmt = self.config.export.format
        export_dir = self.config.export_dir
        destination = export_dir / mixdown.export_filename(session.id, fmt)
        if destination.exists():
            # Backfill: the folder is carrying the file, so it has been
            # exported, whether or not a marker was written at the time. This is
            # what carries a folder filled before the ledger existed across the
            # change without re-exporting everything in it once more.
            self._mark_exported(session, destination)
            return None
        if mixdown.is_exported(export_dir, session.id):
            return None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            mixed = mixdown.mixdown(session, fmt=fmt)
            # copy2 for the mtime: the upload folder is read by a human deciding
            # what still needs uploading, and a file stamped "now" on every
            # sweep tells them nothing.
            shutil.copy2(mixed, destination)
        except (mixdown.MixdownError, OSError) as exc:
            log.warning("export failed for %s: %s", session.id, exc)
            return None
        self._mark_exported(session, destination)
        log.info(
            "exported session=%s -> %s (%.1f MB)",
            session.id,
            destination,
            destination.stat().st_size / (1024 * 1024),
        )
        if (session.duration_seconds or 0) > mixdown.PLAUD_MAX_SECONDS:
            log.warning(
                "%s is longer than the 5-hour upload limit; split it before uploading",
                session.id,
            )
        return destination

    def _mark_exported(self, session: Session, destination: Path) -> None:
        """Write the ledger marker, treating a failure as cosmetic.

        Separate from the copy's own ``try``: by the time this runs the file is
        already in the folder, and the worst a missing marker costs is one
        re-export after the user deletes it. That must not read as a failed
        export, and must not stop the sweep.
        """
        try:
            mixdown.mark_exported(
                self.config.export_dir, session.id, filename=destination.name
            )
        except OSError as exc:  # pragma: no cover - the copy just succeeded here
            log.warning("could not mark %s as exported: %s", session.id, exc)

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
            # transcript out (disk full, unwritable session directory, ...):
            # either way the session is unrecoverable without intervention, and its
            # audio is retained rather than deleted (spec 11).
            session.transition(
                "failed",
                by="munin-work",
                error={"code": "internal", "message": str(exc), "at": _now_iso()},
            )

    def _write_transcript(self, session: Session, transcript) -> None:
        """``transcribing`` -> ``done``: render, write the sidecar, then drop
        the inbox symlink.

        The transcript is written once, into the session directory. There is no
        second copy anywhere: the session directory is the authoritative
        location (D24, spec 7.5).
        """
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

        session.transition(
            "done",
            by="munin-work",
            transcript={
                "txt": str(txt_path),
                "json": str(json_path),
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
        backend = get_backend(self.config.transcribe.backend)
        available = backend.available()
        # Distinct sessions, not transitions: a session that is claimed and then
        # processed in the same sweep was one session's worth of work, and the
        # docstring promises sessions.
        touched: set[str] = set()
        for session in list(self.spool.iter_sessions()):
            # Before the claim below, so a session captured during this sweep is
            # exported in the same sweep rather than one interval later.
            if self.export(session) is not None:
                touched.add(session.id)
            if session.state == "captured":
                # A session read in this sweep may have moved since: `munin
                # start --resume` takes captured/pending back to recording, and
                # that is the daemon's transition to make (D15). Losing the race
                # is ordinary, so the sweep skips the session and re-reads it
                # next time round -- it must never take munin-work down, which
                # has no unit to restart it.
                try:
                    self.claim(session)
                except StateError as exc:
                    log.info("skipping %s: %s", session.id, exc)
                    continue
                touched.add(session.id)
        for session in list(self.spool.iter_sessions()):
            if session.state != "pending":
                continue
            if session.pending_reason and not available:
                # Already queued, with the reason on the record, and nothing has
                # changed since. Re-running the backend would re-walk
                # pending -> transcribing -> pending and append two rows to
                # history[] every sweep -- and history[] is the audit trail, is
                # append-only and is never trimmed (spec 12). munin-work runs on
                # a timer, and the PoC's *designed* end state is exactly this
                # one, so without this guard every proof-of-concept session
                # grows session.json without bound. The session is retried the
                # moment a backend reports itself available.
                continue
            try:
                self.process(session)
            except StateError as exc:  # the daemon reopened it; see above
                log.info("skipping %s: %s", session.id, exc)
                continue
            touched.add(session.id)
        return len(touched)


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


def configure_logging(config: Config, *, level: int = logging.INFO) -> None:
    """Structured lines to ``~/munin/munin.log`` and to stderr for systemd.

    Deliberately the same shape as ``daemon.configure_logging``, and writing to
    the same file: one log is how "the recorder captured it at 12:15, the worker
    exported it at 12:15" reads as one story. Duplicated rather than imported,
    because munin-work importing the recorder's module to borrow a formatter
    would pull the capture and desktop stacks into a process that holds no audio
    device (contracts section 3, workstream ownership).

    Until this existed the worker logged into a root logger with no handlers, so
    every ``log.info`` was discarded -- including the export lines added for the
    2026-09-18 amendment, whose entire purpose is to make a silent failure
    visible.
    """
    root = logging.getLogger("munin")
    root.setLevel(level)
    root.handlers.clear()
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    try:
        path = Path(config.home).expanduser() / config.paths.log
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except OSError as exc:  # pragma: no cover - a read-only home is the user's
        root.warning("no log file: %s", exc)


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
    configure_logging(config)
    log.info("munin-work ready pid=%s interval=%.1fs", os.getpid(), args.interval)
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
