"""Cutting a session that holds two meetings into two sessions (D26).

One recording, two meetings: the user walked from one call into the next and
never stopped the recording. The audio is right and the record is wrong, and
every consequence of that is downstream -- diarization clusters speakers across
both meetings, the two sets of participants end up in one transcript under one
retention clock, and the second meeting's title is the first one's.

Two routes exist, and this module is the second of them:

- **Live** (``munin split --now``, :meth:`munin.daemon.Daemon.handle_split`):
  the daemon notices a *different* call going live while it is recording, the
  user confirms, and the boundary costs nothing -- one session is finished and
  the next is started. No audio is touched.
- **Offline** (``munin split <session> <at>``, this module): the meeting is
  already captured, so the cut has to be made in the file.

**The parent is never modified in place.** Its audio and its ``checksums`` are
what the machine recorded, and a session store that rewrites those has no
integrity story left to tell an auditor (spec 12). So the two halves are new
sessions derived from it, and the parent moves to the terminal state ``split``:
it keeps every byte it had, and it stops being work the worker will pick up.
``split_into`` on the parent and ``split_from`` on each half are the provenance
chain, and all three records carry a ``history[]`` row for the cut.

**The cut is a stream copy, not a re-encode.** ``ffmpeg -c copy`` lands on Opus
packet boundaries (20 ms), which costs nothing audible and no quality at all,
and a two-hour session cuts in about a second. The consequence to know about is
that a half's ``duration_seconds`` is the *intended* cut length; the file may
differ by up to one packet either side. At the resolution the transcript renders
(one second) that is invisible, and no timestamp downstream is derived from
anything finer.

Refusing is the default wherever the state is not obviously safe: a session
still being written to, one already split, one the worker is inside, and a cut
point outside the audio are all errors that say what to do instead.

Neither half exists as a *session* until it is complete: its directory is filled
first and its ``session.json`` written last, after the parent has been claimed.
``munin-work`` finds sessions by scanning for ``session.json`` and claims every
``captured`` one it meets, so a record written beside audio still being copied
would be claimed mid-cut -- two writers on one session, and a rollback deleting
a directory the worker is inside.

Owner: worker workstream. Spec 7.5 and D26; contracts section 16.7.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from munin.capture.base import segment_filenames
from munin.spool import Segment, Session, Spool, StateError, read_session

__all__ = [
    "SplitError",
    "SplitFailed",
    "SplitUsage",
    "SPLITTABLE_STATES",
    "Piece",
    "Part",
    "parse_offset",
    "format_offset",
    "clock_to_offset",
    "measure_missing_durations",
    "plan",
    "cut_argv",
    "default_titles",
    "split",
]

log = logging.getLogger("munin.split")

#: Where a split is allowed to start from. ``recording``/``ending`` are still
#: being written to; ``transcribing`` belongs to the worker at that moment;
#: ``done`` already has a transcript, and cutting the audio underneath one would
#: leave a transcript describing files that no longer say that; ``split`` has
#: already been cut. Each refusal names the alternative.
SPLITTABLE_STATES = frozenset({"captured", "pending"})

#: A piece shorter than this is not a meeting, it is a rounding error. A cut
#: this close to a segment's edge is snapped to the edge instead, which is what
#: the user meant and what keeps ``ffmpeg -t 0.000`` -- a zero-byte file, and a
#: failed split -- out of reach. Two axes meet at every segment boundary (wall
#: clock in, captured audio out), and they disagree by a fraction of a second.
MIN_PIECE_SECONDS = 0.25


class SplitError(RuntimeError):
    """The split cannot be made. Carries a sentence, not a traceback.

    A precondition: the state is wrong, the cut point is outside the audio, the
    session has already been split. The CLI exits 4 on these -- the same code
    "already recording" uses, and for the same reason: nothing was attempted.
    """


class SplitFailed(SplitError):
    """The cut was attempted and did not finish. The CLI exits 1."""


class SplitUsage(SplitError):
    """What was typed is not a time. The CLI exits 2, like any bad argument."""


@dataclass(frozen=True)
class Piece:
    """One stretch of one parent segment that becomes part of a new session."""

    #: The parent segment this comes from.
    source_index: int
    mic: str
    app: str
    #: Offset into the parent segment, in seconds. ``0.0`` for a whole segment.
    start: float
    #: Length in seconds. ``None`` means "to the end of the segment".
    duration: float | None
    #: How much *audio* this piece holds. Carried rather than derived from the
    #: two timestamps, because those are not the same quantity: a segment's
    #: ``started_at`` is stamped before the encoders are up, so its wall-clock
    #: span runs a fraction of a second longer than its ``duration_seconds``.
    #: The halves have to sum to the parent on the axis the transcript uses.
    seconds: float
    started_at: datetime
    stopped_at: datetime
    app_source: str | None

    @property
    def whole(self) -> bool:
        """A piece that is the parent segment entire: a copy, not a cut."""
        return self.start == 0.0 and self.duration is None


@dataclass(frozen=True)
class Part:
    """One half of the split, before anything is written."""

    number: int  # 1 or 2
    pieces: tuple[Piece, ...]

    @property
    def started_at(self) -> datetime:
        return self.pieces[0].started_at

    @property
    def stopped_at(self) -> datetime:
        return self.pieces[-1].stopped_at

    @property
    def duration_seconds(self) -> float:
        return sum(piece.seconds for piece in self.pieces)


def parse_offset(text: str) -> float:
    """``HH:MM:SS``, ``MM:SS`` or plain seconds into a float. Locale-neutral.

    >>> parse_offset("27:32")
    1652.0
    >>> parse_offset("1:02:11")
    3731.0
    """
    raw = (text or "").strip()
    if not raw:
        raise SplitUsage("give a cut point, as HH:MM:SS, MM:SS or seconds")
    fields = raw.split(":")
    if len(fields) > 3:
        raise SplitUsage(f"{raw!r} is not a time; use HH:MM:SS, MM:SS or seconds")
    try:
        numbers = [float(field) for field in fields]
    except ValueError:
        raise SplitUsage(
            f"{raw!r} is not a time; use HH:MM:SS, MM:SS or seconds"
        ) from None
    if any(number < 0 for number in numbers):
        raise SplitUsage(f"{raw!r} is negative")
    if len(numbers) > 1 and any(number >= 60 for number in numbers[1:]):
        raise SplitUsage(f"{raw!r} has a minutes or seconds field above 59")
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


def format_offset(seconds: float) -> str:
    """``HH:MM:SS``, the same shape ``munin list`` prints durations in."""
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def clock_to_offset(session: Session, clock: str) -> float:
    """Turn a wall-clock ``HH:MM[:SS]`` into an offset on the audio timeline.

    The two are not the same axis, and the difference is the whole reason this
    exists: a resumed session (D15) has a gap between segments that is wall-clock
    time but not audio time, and every offset downstream -- the transcript, the
    mix, this module -- counts captured audio only. A user reading a clock has no
    way to do that arithmetic; the record does.

    The clock is read on the local date each segment actually ran on, so a
    meeting that crosses midnight resolves against the right day.
    """
    fields = clock.strip().split(":")
    if len(fields) not in (2, 3) or not all(field.isdigit() for field in fields):
        raise SplitUsage(f"--clock takes HH:MM or HH:MM:SS, got {clock!r}")
    hour, minute = int(fields[0]), int(fields[1])
    second = int(fields[2]) if len(fields) == 3 else 0
    if hour > 23 or minute > 59 or second > 59:
        raise SplitUsage(f"{clock!r} is not a time of day")

    elapsed = 0.0
    for segment in _ordered_segments(session):
        started = segment.started_at
        stopped = _segment_stopped(segment)
        wanted = started.replace(
            hour=hour, minute=minute, second=second, microsecond=0
        )
        if wanted < started - timedelta(hours=12):
            # The segment runs past midnight and the clock belongs to the next
            # day: 00:10 asked for during a segment that began at 23:50.
            wanted += timedelta(days=1)
        if started <= wanted <= stopped:
            # Into the segment by the clock, but *out* of it in audio: the two
            # differ by the encoder start-up the segment's timestamps include
            # and its duration does not, so a clock past the last sample maps to
            # the end of the audio rather than past it.
            within = min((wanted - started).total_seconds(), _segment_seconds(segment))
            return elapsed + within
        elapsed += _segment_seconds(segment)
    raise SplitError(
        f"{session.id}: nothing was being recorded at {clock}; "
        f"this session recorded {_wall_clock_span(session)}"
    )


def _ordered_segments(session: Session) -> list[Segment]:
    segments = sorted(session.segments, key=lambda segment: segment.index)
    if not segments:
        raise SplitError(f"{session.id}: no segments to split")
    return segments


def _segment_stopped(segment: Segment) -> datetime:
    if segment.stopped_at is not None:
        return segment.stopped_at
    if segment.duration_seconds is not None:
        return segment.started_at + timedelta(seconds=segment.duration_seconds)
    raise SplitError(f"segment {segment.index} never finished; it cannot be cut")


def _segment_seconds(segment: Segment) -> float:
    """How much audio a segment holds. Never guessed from the clock.

    A session recovered after a daemon crash has ``stopped_at`` set to the
    moment of recovery and no ``duration_seconds`` at all (spec 11), so its
    wall-clock span can be hours wider than its audio.
    :func:`measure_missing_durations` is what fills that in, by measuring the
    file; falling back to the clock here would put the cut in the wrong place
    and write a duration that is simply false.
    """
    if segment.duration_seconds is None:
        raise SplitError(
            f"segment {segment.index} has no measured duration; it was "
            "recovered after a crash and has to be measured before it is cut"
        )
    return float(segment.duration_seconds)


def _probe_seconds(path: Path, ffprobe: str) -> float:
    """The true length of one track, from ``ffprobe``."""
    if shutil.which(ffprobe) is None:
        raise SplitFailed(
            f"{ffprobe} is not installed, and this session was recovered after "
            "a crash: measuring the audio is the only way to know its length"
        )
    try:
        completed = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path),
            ],
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # pragma: no cover - which() already passed
        raise SplitFailed(f"could not run {ffprobe}: {exc}") from exc
    try:
        return float((completed.stdout or "").strip())
    except ValueError:
        detail = (completed.stderr or "").strip().splitlines()
        raise SplitFailed(
            f"{ffprobe} could not measure {path.name}"
            + (f": {detail[-1]}" if detail else "")
        ) from None


def measure_missing_durations(
    session: Session, *, ffprobe: str = "ffprobe"
) -> bool:
    """Fill in segment lengths a crash recovery never measured. In memory only.

    Returns whether anything had to be measured. The parent is never saved by
    this: what is written down stays what the daemon wrote, and the measurement
    exists so the *cut* lands in the right place. ``stopped_at`` is corrected
    alongside, because a recovered segment's is the moment the next daemon
    started, which is not when the meeting ended.
    """
    measured = False
    for segment in _ordered_segments(session):
        if segment.duration_seconds is not None:
            continue
        seconds = _probe_seconds(session.directory / segment.mic, ffprobe)
        segment.duration_seconds = seconds
        segment.stopped_at = segment.started_at + timedelta(seconds=seconds)
        measured = True
    if measured:
        log.info("measured %s: the daemon never recorded its segment lengths",
                 session.id)
    return measured


def _wall_clock_span(session: Session) -> str:
    segments = _ordered_segments(session)
    first = segments[0].started_at
    last = _segment_stopped(segments[-1])
    return f"{first:%H:%M:%S} to {last:%H:%M:%S}"


def plan(session: Session, at_seconds: float) -> tuple[Part, Part]:
    """Map a cut point on the audio timeline onto the two halves' pieces.

    Pure: nothing is read or written. ``at_seconds`` counts captured audio, so a
    gap between segments takes no time -- the same axis the transcript and the
    mix use (contracts section 10).
    """
    segments = _ordered_segments(session)
    total = sum(_segment_seconds(segment) for segment in segments)
    if at_seconds <= 0 or at_seconds >= total:
        raise SplitError(
            f"{session.id}: the cut has to fall inside the recording "
            f"(00:00:00 to {format_offset(total)}), got {format_offset(at_seconds)}"
        )
    at_seconds = _snap_to_boundary(segments, at_seconds, total)

    first: list[Piece] = []
    second: list[Piece] = []
    elapsed = 0.0
    for segment in segments:
        seconds = _segment_seconds(segment)
        started = segment.started_at
        stopped = _segment_stopped(segment)
        ends_at = elapsed + seconds
        if ends_at <= at_seconds:
            first.append(_whole(segment, started, stopped))
        elif elapsed >= at_seconds:
            second.append(_whole(segment, started, stopped))
        else:
            within = at_seconds - elapsed
            boundary = started + timedelta(seconds=within)
            first.append(
                Piece(
                    source_index=segment.index,
                    mic=segment.mic,
                    app=segment.app,
                    start=0.0,
                    duration=within,
                    seconds=within,
                    started_at=started,
                    stopped_at=boundary,
                    app_source=segment.app_source,
                )
            )
            second.append(
                Piece(
                    source_index=segment.index,
                    mic=segment.mic,
                    app=segment.app,
                    start=within,
                    duration=None,
                    seconds=seconds - within,
                    started_at=boundary,
                    stopped_at=stopped,
                    app_source=segment.app_source,
                )
            )
        elapsed = ends_at
    return Part(1, tuple(first)), Part(2, tuple(second))


def _whole(segment: Segment, started: datetime, stopped: datetime) -> Piece:
    return Piece(
        source_index=segment.index,
        mic=segment.mic,
        app=segment.app,
        start=0.0,
        duration=None,
        seconds=_segment_seconds(segment),
        started_at=started,
        stopped_at=stopped,
        app_source=segment.app_source,
    )


def _snap_to_boundary(
    segments: list[Segment], at_seconds: float, total: float
) -> float:
    """Pull a cut that lands a fraction from a segment's edge onto the edge.

    Without this, a cut a millisecond into a segment asks ffmpeg for a piece of
    a millisecond, which writes a zero-byte file and fails the whole split. It
    is reachable from ordinary input: ``--clock`` on a segment's start minute,
    or an offset typed from the durations ``munin list`` rounds to the second.
    A cut that snaps onto the recording's own start or end is refused, because
    a half with no audio in it is not a meeting.
    """
    elapsed = 0.0
    for segment in segments:
        seconds = _segment_seconds(segment)
        if elapsed < at_seconds < elapsed + seconds:
            if at_seconds - elapsed < MIN_PIECE_SECONDS:
                at_seconds = elapsed
            elif elapsed + seconds - at_seconds < MIN_PIECE_SECONDS:
                at_seconds = elapsed + seconds
            break
        elapsed += seconds
    if at_seconds <= 0 or at_seconds >= total:
        raise SplitError(
            "the cut falls within a fraction of a second of the start or the "
            "end of the recording; there would be nothing in one of the halves"
        )
    return at_seconds


def cut_argv(
    source: Path,
    output: Path,
    *,
    start: float,
    duration: float | None,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """The ffmpeg argv copying one stretch of one track out to its own file.

    ``-ss`` before ``-i`` so the seek is on the input and costs nothing on a long
    file, ``-c copy`` so no sample is re-encoded, and ``-f opus`` because the
    output is written to a ``.part`` name ffmpeg cannot guess a muxer from.
    """
    argv = [ffmpeg, "-nostdin", "-loglevel", "error", "-y"]
    if start > 0:
        argv += ["-ss", f"{start:.3f}"]
    argv += ["-i", str(source)]
    if duration is not None:
        argv += ["-t", f"{duration:.3f}"]
    argv += ["-c", "copy", "-f", "opus", str(output)]
    return argv


def _run_cut(argv: list[str], output: Path, ffmpeg: str) -> None:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:  # pragma: no cover - which() already passed
        output.unlink(missing_ok=True)
        raise SplitFailed(f"could not run {ffmpeg}: {exc}") from exc
    if completed.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        detail = (completed.stderr or "").strip().splitlines()
        output.unlink(missing_ok=True)
        raise SplitFailed(
            f"ffmpeg could not cut {output.name}"
            + (f": {detail[-1]}" if detail else f" (exit {completed.returncode})")
        )


def _write_pieces(
    part: Part, source_dir: Path, target_dir: Path, ffmpeg: str
) -> list[Segment]:
    """Write one half's audio and return its ``segments[]``, renumbered from 1."""
    segments: list[Segment] = []
    for index, piece in enumerate(part.pieces, start=1):
        mic_name, app_name = segment_filenames(index)
        for source_name, target_name in ((piece.mic, mic_name), (piece.app, app_name)):
            source = source_dir / source_name
            if not source.is_file():
                raise SplitError(f"{source.name} is missing; nothing to cut")
            target = target_dir / target_name
            if piece.whole:
                shutil.copy2(source, target)
                continue
            partial = target.with_name(target.name + ".part")
            _run_cut(
                cut_argv(
                    source,
                    partial,
                    start=piece.start,
                    duration=piece.duration,
                    ffmpeg=ffmpeg,
                ),
                partial,
                ffmpeg,
            )
            os.replace(partial, target)
        segments.append(
            Segment(
                index=index,
                mic=mic_name,
                app=app_name,
                started_at=piece.started_at,
                stopped_at=piece.stopped_at,
                duration_seconds=piece.seconds,
                # Inherited, never inferred: what the app track holds is a fact
                # about the capture, and a cut does not change it (spec 12).
                app_source=piece.app_source,
            )
        )
    return segments


def default_titles(session: Session, parts: tuple[Part, Part]) -> tuple[str, str]:
    """Part 1 keeps the session's title; part 2 is named after its clock time.

    The first meeting is the one the user named when they started recording, so
    it keeps that name. The second was never named -- it is the one nobody
    stopped for -- and ``Meeting HH:MM`` is what ``Spool.create`` calls an
    untitled recording, so a split leaves the same shape of name behind.
    """
    return session.title, f"Meeting {parts[1].started_at:%H:%M}"


def split(
    session: Session,
    *,
    spool: Spool,
    at_seconds: float,
    titles: tuple[str | None, str | None] = (None, None),
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> tuple[Session, Session]:
    """Cut ``session`` in two. Returns the two new sessions, part 1 first.

    The parent is left byte-for-byte as it was and moves to ``split``; the two
    halves are new sessions in ``captured``, linked into the inbox so the worker
    picks them up as the separate meetings they are.

    Order matters, and it is: fill both halves' directories, claim the parent,
    then write the halves' ``session.json`` and link them into the inbox. A
    directory without a ``session.json`` is not a session to anything that reads
    this store, so until the parent is claimed there is nothing for the worker
    to pick up and nothing a rollback can delete out from under it. An
    interrupted split therefore leaves a parent still queued and at worst a
    directory nothing points at -- never a parent marked ``split`` with nothing
    to show for it, and never two halves queued beside a parent that was resumed
    while ffmpeg ran.
    """
    state = getattr(session, "state", None)
    if state == "split":
        into = ", ".join(session.split_into or []) or "two sessions"
        raise SplitError(f"{session.id}: already split into {into}")
    if state not in SPLITTABLE_STATES:
        hint = ""
        if state in ("recording", "ending"):
            hint = "; stop the recording first"
        elif state == "done":
            hint = "; the transcript would no longer describe the audio"
        elif state == "transcribing":
            hint = "; the worker is inside it, so wait for it to finish"
        raise SplitError(
            f"{session.id}: is {state}, and only a captured or pending session "
            f"can be split{hint}"
        )
    if shutil.which(ffmpeg) is None:
        raise SplitFailed(f"{ffmpeg} is not installed; the cut needs it")

    # A session recovered after a daemon crash carries no segment lengths, and
    # its timestamps are the recovery's, not the meeting's (spec 11). Measure
    # before planning, or the cut lands somewhere else entirely.
    measure_missing_durations(session, ffprobe=ffprobe)

    parts = plan(session, at_seconds)
    first_title, second_title = default_titles(session, parts)
    if titles[0]:
        first_title = titles[0]
    if titles[1]:
        second_title = titles[1]

    log.info(
        "splitting session=%s at=%s into %d + %d pieces",
        session.id,
        format_offset(at_seconds),
        len(parts[0].pieces),
        len(parts[1].pieces),
    )

    halves: list[Session] = []
    for part, title in zip(parts, (first_title, second_title)):
        half = spool.derive(
            title=title,
            parent=session,
            created=part.started_at,
            note=(
                f"part {part.number} of {session.id}, split at "
                f"{format_offset(at_seconds)} of the audio"
            ),
            split_from={
                "session": session.id,
                "kind": "offline",
                "part": part.number,
                "offset_seconds": round(at_seconds, 3),
            },
        )
        try:
            half.segments = _write_pieces(
                part, session.directory, half.directory, ffmpeg
            )
            half.started_at = part.started_at
            half.stopped_at = part.stopped_at
            half.duration_seconds = part.duration_seconds
            half.checksums = spool.checksum_segments(half)
        except (SplitError, OSError):
            # The half is incomplete and nothing points at it yet. Take the
            # directories back out so a retry is not blocked by a -2 suffix.
            shutil.rmtree(half.directory, ignore_errors=True)
            for done in halves:
                shutil.rmtree(done.directory, ignore_errors=True)
            raise
        halves.append(half)

    # The parent is claimed before either half is advertised, and it is claimed
    # against the state on disk rather than the one this process read minutes
    # ago: ``munin-work`` sweeps every 5 s and ``munin start --resume`` can take
    # a captured session back to ``recording`` (D15), and both can happen while
    # ffmpeg is cutting. Losing that race has to cost the halves, never the
    # parent -- two halves queued *and* a resumed parent would be the original
    # problem twice over.
    parent = read_session(session.directory)
    try:
        if parent.state not in SPLITTABLE_STATES:
            raise SplitError(
                f"{session.id}: became {parent.state} while it was being cut "
                "(resumed, or claimed by the worker); nothing was split"
            )
        parent.transition(
            "split",
            by="munin",
            note=(
                f"split at {format_offset(at_seconds)} into "
                f"{halves[0].id} and {halves[1].id}; audio and checksums unchanged"
            ),
            split_into=[half.id for half in halves],
            # Contracts section 3: ``pending_reason`` is non-null only in
            # ``pending``. A parent cut while it waited for a backend would
            # otherwise keep "no transcription backend configured" for good, on
            # a session nothing will ever transcribe.
            pending_reason=None,
        )
    except (SplitError, StateError) as exc:
        for half in halves:
            shutil.rmtree(half.directory, ignore_errors=True)
        if isinstance(exc, SplitError):
            raise
        raise SplitError(
            f"{session.id}: changed under the split ({exc}); nothing was cut"
        ) from exc

    # The parent is committed, so the halves become sessions now: the record
    # first, then the inbox link that makes the worker look at it.
    for half in halves:
        half.save()
        spool.link_inbox(half)
    spool.unlink_inbox(session)
    return halves[0], halves[1]
