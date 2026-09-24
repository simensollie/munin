"""The one-file copy of a session, for manual upload (spec 10).

Munin records two tracks because everything downstream wants them apart: tier 1
speaker attribution is "energy on track 1 and not on track 2", and that is exact
and free only while the tracks are separate (spec 7.4). Every *other* tool wants
one file. Plaud's importer takes MP3 or OPUS, one file per meeting, five hours
maximum, so until a transcription backend runs on this machine the mix is how a
recording becomes text at all.

Produced by ``munin mix``, and by ``munin-work`` on every sweep when
``[export] enabled`` is set (contracts amendment 2026-09-18). It stays a derived
file that regenerates from the tracks in a few seconds and is absent from the
session record, which is what makes producing it twice harmless and a missed
export self-healing.

**This module is temporary (D25).** It exists because the PoC ships
``backend = "none"``. Once M4 transcribes on this machine, delete it, the ``mix``
command, its tests and the upload folder rather than maintaining them; the two
tracks are the record and everything downstream wants them apart. Spec §10 says
what deleting it costs (the summary), which is a decision to make before the
deletion, not after.

**Opus by default, not the MP3 of spec 10.** Measured on a 17-minute meeting:
Opus at 24 kbps is 3.0 MB against 8.2 MB for MP3 at 64 kbps, while sounding
*better* on speech -- MP3 is a poor codec at low rates and 64 kbps was picked as
"the safe choice", not as the good one. Appendix A's captured upload API takes
``file_type: "MP3"|"OPUS"``, so Opus is accepted; that is an API probe rather
than a completed upload, which is why ``--format mp3`` stays one flag away if
the web importer ever refuses an ``.opus``.

Three deliberate departures from the literal command in spec 10
(``ffmpeg -i mic.opus -i app.opus -filter_complex amix=inputs=2 mixed.mp3``):

- ``normalize=0``. ``amix`` divides every input by the number of inputs, so the
  spec's command lands both tracks 6 dB down. On a real recording the microphone
  already sits ~8 dB under the meeting track (headset mic, muted between turns),
  and halving it again puts your own speech where an ASR model starts dropping
  words.
- ``alimiter`` after the sum, because two tracks that each peak near 0 dBFS add
  up to clipping. It catches peaks only: ``level=false`` keeps it from
  auto-levelling, so quiet passages are left where they were.
- ``latency=true`` on the limiter, so its lookahead does not shift the mix off
  the timeline the tracks and the transcript share (spec 7.5).

A resumed session (D15) has several segments. Each track is concatenated in
segment order *before* the sum, which puts the mix on the same captured-audio
time axis the renderer uses -- summed durations, wall-clock breaks closed up --
so a timestamp in a Plaud transcript still points at the same moment as a
timestamp in a Munin one.

The mix is not part of the session record: no ``session.json`` field, no state
transition, no history entry. It is a re-encode of audio that already exists,
and the audit trail is about what was captured (contracts section 3). What
little bookkeeping it has stays on the filesystem: the file in the session
directory, and -- for the upload folder only -- a marker under
``<upload folder>/.exported/`` saying the folder has already carried this
session, so a file deleted after it was uploaded is not silently put back.

Owner: worker workstream. Spec 10; not part of the frozen PoC contract surface.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from munin.paths import slugify

__all__ = [
    "MIXED_STEM",
    "DEFAULT_FORMAT",
    "FORMATS",
    "MixFormat",
    "PLAUD_MAX_SECONDS",
    "BUSY_STATES",
    "SKIP_STATES",
    "MixdownError",
    "mix_format",
    "mixed_filename",
    "mixed_path",
    "existing_mixes",
    "track_pairs",
    "mix_argv",
    "mixdown",
    "export_filename",
    "export_name",
    "LEDGER_DIRNAME",
    "ledger_dir",
    "ledger_entry",
    "is_exported",
    "mark_exported",
    "forget_export",
    "exported_ids",
    "ledger_owner",
]

log = logging.getLogger("munin.mixdown")

#: Spec 7.5's name for the file, next to the tracks it is made from. The
#: extension follows the format; the stem never changes.
MIXED_STEM = "mixed"


@dataclass(frozen=True)
class MixFormat:
    """One upload format Plaud accepts (Appendix A: ``MP3`` or ``OPUS``)."""

    name: str
    extension: str
    #: Named explicitly, because the encode writes to ``mixed.<ext>.part``
    #: first and ffmpeg cannot guess a muxer from that.
    muxer: str
    codec: str
    bitrate_kbps: int
    #: Encoder options after the bitrate.
    options: tuple[str, ...] = ()


FORMATS: dict[str, MixFormat] = {
    # 24 kbps matches what the tracks were captured at, so the mix carries no
    # less than its sources. ``voip`` is Opus's speech mode: the mix exists to be
    # read by an ASR model, not to be listened to as music.
    "opus": MixFormat(
        name="opus",
        extension="opus",
        muxer="opus",
        codec="libopus",
        bitrate_kbps=24,
        options=("-application", "voip"),
    ),
    # Spec 10's original choice, kept as the fallback: MP3 is the format every
    # importer has always taken, and 64 kbps is where it stops hurting speech.
    "mp3": MixFormat(
        name="mp3",
        extension="mp3",
        muxer="mp3",
        codec="libmp3lame",
        bitrate_kbps=64,
    ),
}

#: What ``munin mix`` writes unless told otherwise.
DEFAULT_FORMAT = "opus"

SAMPLE_RATE = 48000

#: Peak ceiling for the limiter, linear. 0.95 is ~-0.4 dBFS.
LIMIT = 0.95

#: Plaud's per-file ceiling (spec 10). Munin warns; it does not split the file,
#: because where to cut a five-hour meeting is the user's call, not a default.
PLAUD_MAX_SECONDS = 5 * 3600

#: The upload folder's own bookkeeping: one marker per session it has already
#: carried, so deleting an uploaded file does not summon it back.
LEDGER_DIRNAME = ".exported"

#: States where the audio files are still being written to.
BUSY_STATES = frozenset({"recording", "ending"})

#: Sessions an unattended sweep must leave alone. ``BUSY_STATES`` because half a
#: recording mixed down looks exactly like a whole one afterwards; ``split``
#: because a split parent holds *both* meetings and its halves hold one each
#: (D26) -- mixing all three would put the merged recording in the upload folder
#: beside the two meetings it was cut into. Shared with ``munin mix --all``
#: rather than restated there: the two lists drifting apart is what let the
#: worker export a parent the CLI knew to skip.
SKIP_STATES = BUSY_STATES | frozenset({"split"})


class MixdownError(RuntimeError):
    """The mix could not be produced. Carries a sentence, not a traceback."""


def mix_format(name: str = DEFAULT_FORMAT) -> MixFormat:
    """Look up a format by name, naming the alternatives when it is unknown."""
    try:
        return FORMATS[name]
    except KeyError:
        known = ", ".join(sorted(FORMATS))
        raise MixdownError(f"unknown mix format {name}; known formats: {known}") from None


def mixed_filename(fmt: str | MixFormat = DEFAULT_FORMAT) -> str:
    """``mixed.opus`` or ``mixed.mp3``."""
    resolved = fmt if isinstance(fmt, MixFormat) else mix_format(fmt)
    return f"{MIXED_STEM}.{resolved.extension}"


def mixed_path(session, fmt: str | MixFormat = DEFAULT_FORMAT) -> Path:
    """Where the mix for this session lives, in the given format."""
    return Path(session.directory) / mixed_filename(fmt)


def existing_mixes(session) -> list[Path]:
    """Every mix already on disk for this session, whatever the format.

    A session mixed to MP3 last month and to Opus today holds both; nothing here
    deletes one for the other, because the older file may be the one already
    uploaded.
    """
    directory = Path(session.directory)
    return [
        path
        for path in (directory / mixed_filename(fmt) for fmt in FORMATS.values())
        if path.exists()
    ]


def export_filename(session_id: str, fmt: str | MixFormat = DEFAULT_FORMAT) -> str:
    """``<session directory name>.<ext>`` -- spec 10's export convention.

    The session id is already the date, the time and a slug of the title
    (``2026-09-17T0913-weekly-quality-sync``), so an exported file pairs
    unambiguously with the session that produced it and the upload folder sorts
    chronologically. It needs no sanitising: ``Spool.create`` builds the id from
    a slug, so it is filesystem-safe by construction.

    Carrying the time matters more than it looks. An uploaded file arrives at a
    third-party importer with no session record attached: the importer stamps it
    with the moment of upload, hours or days after the meeting, so the filename
    is the only thing that still knows when the meeting was, and ``mixed.opus``
    is the same name for every meeting.

    The name is also final. An imported file keeps whatever it was uploaded as,
    however good a summary the importer then generates from it (spec section 10,
    corrected 2026-09-24) -- which is why the session title is worth deriving
    properly at capture time rather than left to a clock.

    >>> export_filename("2026-09-17T0913-weekly-quality-sync")
    '2026-09-17T0913-weekly-quality-sync.opus'
    """
    resolved = fmt if isinstance(fmt, MixFormat) else mix_format(fmt)
    return f"{session_id}.{resolved.extension}"


#: Characters a filename cannot carry on at least one of the systems the file
#: may pass through on its way to the importer.
_UNSAFE = set('/\\:*?"<>|')

#: Longest title kept in an export name, in characters. Well under any
#: filesystem limit, and about what an importer's list view shows.
EXPORT_TITLE_MAX = 90


def export_name(session: Any, fmt: str | MixFormat = DEFAULT_FORMAT) -> str:
    """``<YYYY-MM-DDTHHMM> <title>.<ext>`` -- the export name since M9.

    The importer keeps the filename as the recording's title for good (spec 10,
    corrected 2026-09-24), so the file carries the real title rather than its
    slug: ``2026-09-23T0901 QMS risk review.opus`` reads as a meeting, where
    ``2026-09-23T0901-qms-risk-review`` reads as a path. The stamp stays first,
    because it is still the only thing that knows when the meeting was, and it
    keeps the folder in chronological order.

    A clock-fallback title (``Microsoft Teams 09:01``) loses its clock, which
    the stamp already says. Anything a filesystem refuses is dropped, a
    ``: `` separator becomes `` - ``, and a long subject is cut at a word.
    The ledger is keyed by session id, never by this name (contracts
    amendment 2026-09-21), so a title that changes after export does not
    export the session twice.

    >>> class S: id = "2026-09-17T0913-weekly-quality-sync"; title = "Weekly quality sync"
    >>> export_name(S)
    '2026-09-17T0913 Weekly quality sync.opus'
    """
    resolved = fmt if isinstance(fmt, MixFormat) else mix_format(fmt)
    session_id = str(session.id)
    stamp = session_id[:15]
    title = str(getattr(session, "title", "") or "").strip()
    clock = f" {stamp[11:13]}:{stamp[13:15]}"
    if title.endswith(clock):
        title = title[: -len(clock)].rstrip()
    title = title.replace(": ", " - ")
    title = "".join(" " if ch in _UNSAFE or ord(ch) < 32 else ch for ch in title)
    title = " ".join(title.split()).strip(" .-")
    if len(title) > EXPORT_TITLE_MAX:
        cut = title[:EXPORT_TITLE_MAX]
        title = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" .-")
    if not title:
        return export_filename(session_id, resolved)
    # Two sessions with one title in one minute (the halves of a live split,
    # D26) are told apart by the id's collision suffix; the name has to be too,
    # or the second export finds the first one's file and reads it as its own.
    suffix = ""
    captured_title = (getattr(session, "enrichment", None) or {}).get(
        "original_title"
    ) or getattr(session, "title", "")
    head, _, tail = session_id.rpartition("-")
    if tail.isdigit() and head.endswith("-" + slugify(str(captured_title))):
        suffix = f" ({tail})"
    return f"{stamp} {title}{suffix}.{resolved.extension}"


# --------------------------------------------------------------------------
# The ledger: what the upload folder has already carried
# --------------------------------------------------------------------------
#
# Until this existed, a destination file's presence was the whole of the
# bookkeeping, which made "exported" and "still here" the same fact. They are
# not: uploading a file to Plaud and then deleting or moving it is the normal
# end of its life, and the next sweep read the absence as "never exported" and
# put it back. The folder refilled itself behind the user.
#
# The fix keeps the bookkeeping in the folder rather than moving it onto the
# session record. Spec 10 sketched the other option -- `munin mark-uploaded`
# and upload state in session metadata -- and the 2026-09-18 amendment ruled it
# out: a `session.json` field makes a re-encode look like a capture event
# (contracts 7.2) and puts new schema on a surface D25 deletes. A marker beside
# the files it describes costs neither, goes when the folder goes, and is
# readable with `ls`.
#
# The marker is written *after* a successful copy, so a failed export still
# retries on the next sweep, and it is named for the session alone, not the
# format: a session exported as Opus and later configured for MP3 has still had
# its turn in the folder, and re-exporting it in a second format is a decision
# for `munin mix --force`, not a side effect of editing config.toml.


def ledger_dir(export_dir: Path) -> Path:
    """The marker directory inside the upload folder.

    Dotted, so the folder a human scans for "what still needs uploading" holds
    only the files they are there to upload.
    """
    return Path(export_dir) / LEDGER_DIRNAME


def ledger_entry(export_dir: Path, session_id: str) -> Path:
    """The marker path for one session. The session id is already a safe name."""
    return ledger_dir(export_dir) / session_id


def is_exported(export_dir: Path, session_id: str) -> bool:
    """Has this session already had its turn in the upload folder?"""
    return ledger_entry(export_dir, session_id).exists()


def mark_exported(export_dir: Path, session_id: str, *, filename: str | None = None) -> Path:
    """Record that the folder carried this session, and return the marker.

    The marker holds one line -- when it was written, and the filename it was
    written for -- because a zero-byte file answers "was it exported" and
    nothing else, and the first question after that is always "when".
    Rewriting an existing marker is allowed: a deliberate re-export through
    ``munin mix`` is a new turn in the folder, and its date is the useful one.
    """
    entry = ledger_entry(export_dir, session_id)
    entry.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    entry.write_text(f"{stamp}\t{filename or ''}\n", encoding="utf-8")
    return entry


def forget_export(export_dir: Path, session_id: str) -> bool:
    """Drop the marker so the session exports again. True if one was there."""
    entry = ledger_entry(export_dir, session_id)
    if not entry.exists():
        return False
    entry.unlink()
    return True


def ledger_owner(export_dir: Path, filename: str) -> str | None:
    """Which session the ledger says a file in the folder was written for.

    ``None`` for a file no marker names -- one copied in before the ledger
    existed, or by hand. Needed since export names carry the title rather than
    the session id: two sessions can now want the same name.
    """
    directory = ledger_dir(export_dir)
    if not directory.is_dir():
        return None
    for entry in directory.iterdir():
        try:
            _stamp, _, name = entry.read_text(encoding="utf-8").strip().partition("\t")
        except OSError:
            continue
        if name == filename:
            return entry.name
    return None


def exported_ids(export_dir: Path) -> list[str]:
    """Every session the folder has carried, sorted -- which is chronological,
    since a session id starts with its own date and time."""
    directory = ledger_dir(export_dir)
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir() if entry.is_file())


def track_pairs(session) -> list[tuple[Path, Path]]:
    """``(mic, app)`` paths per segment, in index order.

    Raises :class:`MixdownError` naming the first missing file rather than
    handing ffmpeg a path it will fail on less legibly.
    """
    directory = Path(session.directory)
    segments = sorted(session.segments, key=lambda segment: segment.index)
    if not segments:
        raise MixdownError(f"{session.id}: no segments to mix")
    pairs: list[tuple[Path, Path]] = []
    for segment in segments:
        mic = directory / segment.mic
        app = directory / segment.app
        for path in (mic, app):
            if not path.exists():
                raise MixdownError(f"{session.id}: {path.name} is missing")
        pairs.append((mic, app))
    return pairs


def mix_argv(
    pairs: Sequence[tuple[Path, Path]],
    output: Path,
    *,
    fmt: str | MixFormat = DEFAULT_FORMAT,
    ffmpeg: str = "ffmpeg",
    bitrate_kbps: int | None = None,
) -> list[str]:
    """The ffmpeg argv summing every segment pair into one mono file.

    Inputs are interleaved ``mic, app, mic, app, ...`` in segment order. Each is
    normalised to mono 48 kHz first, so one oddly-encoded segment cannot make
    ``concat`` refuse the whole session.
    """
    if not pairs:
        raise MixdownError("nothing to mix: no segment pairs")
    resolved = fmt if isinstance(fmt, MixFormat) else mix_format(fmt)
    bitrate = bitrate_kbps or resolved.bitrate_kbps

    argv = [ffmpeg, "-nostdin", "-loglevel", "error", "-y"]
    for mic, app in pairs:
        argv += ["-i", str(mic), "-i", str(app)]

    count = len(pairs)
    chains = [
        f"[{index}:a]aresample={SAMPLE_RATE},"
        f"aformat=sample_fmts=fltp:channel_layouts=mono[t{index}]"
        for index in range(count * 2)
    ]
    if count == 1:
        mic_label, app_label = "[t0]", "[t1]"
    else:
        mic_inputs = "".join(f"[t{2 * k}]" for k in range(count))
        app_inputs = "".join(f"[t{2 * k + 1}]" for k in range(count))
        chains.append(f"{mic_inputs}concat=n={count}:v=0:a=1[mic]")
        chains.append(f"{app_inputs}concat=n={count}:v=0:a=1[app]")
        mic_label, app_label = "[mic]", "[app]"
    chains.append(f"{mic_label}{app_label}amix=inputs=2:normalize=0:duration=longest[sum]")
    chains.append(f"[sum]alimiter=limit={LIMIT}:level=false:latency=true[out]")

    argv += [
        "-filter_complex", ";".join(chains),
        "-map", "[out]",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-c:a", resolved.codec,
        "-b:a", f"{bitrate}k",
        *resolved.options,
        "-f", resolved.muxer,
        str(output),
    ]
    return argv


def mixdown(
    session,
    *,
    fmt: str | MixFormat = DEFAULT_FORMAT,
    force: bool = False,
    ffmpeg: str = "ffmpeg",
    bitrate_kbps: int | None = None,
) -> Path:
    """Write the mix into the session directory and return its path.

    An existing mix *in the requested format* is kept unless ``force``; a mix in
    another format is left alone either way. Refuses a session that is still
    being written to, because half a recording mixed down looks exactly like a
    whole one afterwards.

    Encoding goes to ``mixed.<ext>.part`` and is renamed on success, so an
    interrupted run leaves no truncated file that a later run would skip as
    already done.
    """
    resolved = fmt if isinstance(fmt, MixFormat) else mix_format(fmt)
    state = getattr(session, "state", None)
    if state in BUSY_STATES:
        raise MixdownError(f"{session.id}: still {state}; stop the recording first")

    output = mixed_path(session, resolved)
    if output.exists() and not force:
        return output
    if shutil.which(ffmpeg) is None:
        raise MixdownError(f"{ffmpeg} is not installed; the mix needs it")

    pairs = track_pairs(session)
    partial = output.with_name(output.name + ".part")
    argv = mix_argv(
        pairs, partial, fmt=resolved, ffmpeg=ffmpeg, bitrate_kbps=bitrate_kbps
    )
    log.info(
        "mixing session=%s segments=%d format=%s", session.id, len(pairs), resolved.name
    )
    try:
        completed = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:  # pragma: no cover - which() already passed
        partial.unlink(missing_ok=True)
        raise MixdownError(f"{session.id}: could not run {ffmpeg}: {exc}") from exc
    if completed.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        detail = (completed.stderr or "").strip().splitlines()
        partial.unlink(missing_ok=True)
        raise MixdownError(
            f"{session.id}: ffmpeg failed"
            + (f": {detail[-1]}" if detail else f" (exit {completed.returncode})")
        )
    os.replace(partial, output)
    return output
