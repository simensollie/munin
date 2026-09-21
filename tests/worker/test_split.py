"""Cutting a captured session that holds two meetings (D26).

The planning half is pure arithmetic on the audio timeline and is tested as
such. The cutting half runs real ffmpeg over the synthetic two-track fixture,
because the claims worth making about it are claims about files: that the two
halves add up to the original, that the tone recorded on each track survives the
cut, and that the parent's audio is not touched -- which is the whole reason the
halves are new sessions rather than a rewrite in place.

Provenance is checked as carefully as the audio. A transcript that came out of
half a recording has to be traceable back to the capture it was cut from, or the
session store cannot answer the only question an auditor asks of it (spec 12).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

import pytest

from munin import cli
from munin import config as config_module
from munin import split as split_module
from munin.capture.base import segment_filenames
from munin.split import (
    SplitError,
    SplitFailed,
    clock_to_offset,
    cut_argv,
    format_offset,
    parse_offset,
    plan,
    split,
)
from munin.spool import Session, Spool, read_session

FFMPEG = shutil.which("ffmpeg")

OSLO = timezone(timedelta(hours=2))
WHEN = datetime(2026, 9, 14, 13, 25, 7, tzinfo=OSLO)

#: The fixture's two tones (tests/conftest.py), an octave apart.
MIC_HZ = 440
APP_HZ = 880

#: A cut is a stream copy, so it lands on an Opus packet boundary: 20 ms, and
#: ffmpeg may carry the page it is on. A tenth of a second is far inside that.
PACKET_SLACK = 0.1


class _Usage(NamedTuple):
    total: int
    used: int
    free: int


@pytest.fixture()
def spool(munin_home: Path, monkeypatch: pytest.MonkeyPatch) -> Spool:
    """A spool over the throwaway root, with plenty of pretend disk."""
    gib = 1024**3
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _Usage(500 * gib, 100 * gib, 400 * gib)
    )
    return Spool(config_module.load())


def _captured_session(
    spool: Spool,
    two_track: Callable[..., tuple[Path, Path]],
    *,
    segments: int = 1,
    seconds: float = 4.0,
    gap_minutes: int = 5,
    title: str = "Weekly quality sync",
) -> Session:
    """A finished session with real audio on disk, one pair per segment."""
    session = spool.create(title=title, source="adhoc", now=WHEN)
    started = WHEN
    for index in range(1, segments + 1):
        mic_name, app_name = segment_filenames(index)
        spool.add_segment(session, index, started_at=started)
        two_track(
            session.directory, seconds=seconds, mic_name=mic_name, app_name=app_name
        )
        stopped = started + timedelta(seconds=seconds)
        spool.complete_segment(
            session, index, stopped_at=stopped, duration_seconds=seconds
        )
        started = stopped + timedelta(minutes=gap_minutes)  # a resume gap (D15)
    session.checksums = spool.checksum_segments(session)
    session.transition("captured", by="munin-rec", stopped_at=stopped)
    spool.link_inbox(session)
    return session


def _duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def _band_db(path: Path, hz: int) -> float:
    """Mean volume of a narrow band around ``hz``, in dB. Silence reads very low."""
    assert FFMPEG
    result = subprocess.run(
        [
            FFMPEG, "-nostdin", "-hide_banner", "-i", str(path),
            "-af", f"bandpass=f={hz}:width_type=h:w=40,volumedetect",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", result.stderr)
    assert match, result.stderr
    return float(match.group(1))


# --- reading a cut point ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("27:32", 1652.0),
        ("1:02:11", 3731.0),
        ("00:01:30", 90.0),
        ("90", 90.0),
        ("12.5", 12.5),
    ],
)
def test_a_cut_point_reads_the_three_shapes(text: str, seconds: float) -> None:
    assert parse_offset(text) == seconds


@pytest.mark.parametrize("text", ["", "half past two", "-5", "1:2:3:4", "12:75"])
def test_a_cut_point_that_is_not_a_time_says_so(text: str) -> None:
    with pytest.raises(SplitError):
        parse_offset(text)


def test_offsets_print_the_way_durations_do() -> None:
    assert format_offset(1652.0) == "00:27:32"
    assert format_offset(0) == "00:00:00"


# --- planning ---------------------------------------------------------


def test_one_segment_is_cut_into_a_head_and_a_tail(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)

    first, second = plan(session, 1.5)

    assert [piece.source_index for piece in first.pieces] == [1]
    assert first.pieces[0].start == 0.0 and first.pieces[0].duration == 1.5
    assert second.pieces[0].start == 1.5 and second.pieces[0].duration is None
    assert first.duration_seconds == pytest.approx(1.5)
    assert second.duration_seconds == pytest.approx(2.5)


def test_the_cut_falls_on_the_audio_timeline_not_the_clock(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """Two 4 s segments five minutes apart: 5 s in is 1 s into segment 2, not
    somewhere in the gap. The transcript counts captured audio, and so does this."""
    session = _captured_session(spool, two_track, segments=2, seconds=4.0)

    first, second = plan(session, 5.0)

    assert [piece.source_index for piece in first.pieces] == [1, 2]
    assert first.pieces[1].duration == pytest.approx(1.0)
    assert [piece.source_index for piece in second.pieces] == [2]
    assert second.pieces[0].start == pytest.approx(1.0)


def test_a_cut_exactly_on_a_segment_boundary_copies_rather_than_cuts(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, segments=2, seconds=4.0)

    first, second = plan(session, 4.0)

    assert [piece.whole for piece in first.pieces] == [True]
    assert [piece.whole for piece in second.pieces] == [True]


@pytest.mark.parametrize("at", [0.0, -1.0, 4.0, 900.0])
def test_a_cut_outside_the_audio_is_refused(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]], at: float
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)

    with pytest.raises(SplitError, match="inside the recording"):
        plan(session, at)


def test_a_clock_time_resolves_through_the_gap(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """Segment 1 runs 13:25:07-13:25:11, segment 2 13:30:11-13:30:15. A user who
    says 13:30:13 means 6 s of audio in, not 306."""
    session = _captured_session(spool, two_track, segments=2, seconds=4.0)

    assert clock_to_offset(session, "13:30:13") == pytest.approx(6.0)
    assert clock_to_offset(session, "13:25:09") == pytest.approx(2.0)


def test_a_clock_time_nothing_was_recorded_at_says_what_was(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, segments=2, seconds=4.0)

    with pytest.raises(SplitError, match="13:25:07 to 13:30:15"):
        clock_to_offset(session, "13:27")


@pytest.mark.parametrize("clock", ["25:00", "noon", "13", "13:60"])
def test_a_clock_that_is_not_a_time_of_day_says_so(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]], clock: str
) -> None:
    session = _captured_session(spool, two_track)

    with pytest.raises(SplitError):
        clock_to_offset(session, clock)


# --- the ffmpeg command -----------------------------------------------


def test_the_cut_is_a_stream_copy_seeking_on_the_input() -> None:
    argv = cut_argv(Path("mic.opus"), Path("out.opus"), start=12.0, duration=3.0)

    assert argv.index("-ss") < argv.index("-i"), "seek the input, not the output"
    assert argv[argv.index("-c") + 1] == "copy", "never re-encode a recording"
    assert argv[argv.index("-t") + 1] == "3.000"
    assert argv[-3:] == ["-f", "opus", "out.opus"]


def test_a_tail_runs_to_the_end_with_no_duration() -> None:
    argv = cut_argv(Path("mic.opus"), Path("out.opus"), start=12.0, duration=None)

    assert "-t" not in argv


# --- the whole operation ----------------------------------------------


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_a_split_produces_two_sessions_whose_audio_adds_up(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)
    before = dict(session.checksums)

    first, second = split(session, spool=spool, at_seconds=1.5)

    assert first.state == "captured" and second.state == "captured"
    assert first.title == "Weekly quality sync"
    assert second.title == "Meeting 13:25"
    assert _duration(first.directory / "mic.opus") == pytest.approx(
        1.5, abs=PACKET_SLACK
    )
    assert _duration(second.directory / "mic.opus") == pytest.approx(
        2.5, abs=PACKET_SLACK
    )
    # Each half keeps both tracks, and each track keeps its own tone: a cut that
    # crossed the tracks over would still produce two playable files.
    for half in (first, second):
        assert _band_db(half.directory / "mic.opus", MIC_HZ) > -40
        assert _band_db(half.directory / "app.opus", APP_HZ) > -40
    # The parent is the record of what was captured, and it is untouched.
    assert spool.checksum_segments(read_session(session.directory)) == before


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_a_split_records_where_both_halves_came_from(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)

    first, second = split(session, spool=spool, at_seconds=1.5)

    parent = read_session(session.directory)
    assert parent.state == "split"
    assert parent.split_into == [first.id, second.id]
    assert parent.history[-1]["by"] == "munin"
    assert "audio and checksums unchanged" in parent.history[-1]["note"]

    for part, half in ((1, first), (2, second)):
        on_disk = read_session(half.directory)
        assert on_disk.split_from == {
            "session": session.id,
            "kind": "offline",
            "part": part,
            "offset_seconds": 1.5,
        }
        assert on_disk.history[0] == {
            "at": on_disk.history[0]["at"],
            "from": None,
            "to": "captured",
            "by": "munin",
            "note": on_disk.history[0]["note"],
        }
        assert session.id in on_disk.history[0]["note"]
        # Facts about the capture are inherited, never invented.
        assert on_disk.source == session.source
        assert on_disk.platform == session.platform
        assert on_disk.capture_method == session.capture_method
        assert on_disk.checksums, "a half carries its own checksums"


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_the_halves_are_queued_and_the_parent_is_not(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """The whole point: the worker must transcribe two meetings, not three."""
    session = _captured_session(spool, two_track, seconds=4.0)

    first, second = split(session, spool=spool, at_seconds=1.5)

    assert sorted(spool.inbox_ids()) == sorted([first.id, second.id])
    assert session.id not in [s.id for s in spool.pending_sessions()]
    assert sorted(s.id for s in spool.pending_sessions()) == sorted(
        [first.id, second.id]
    )


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_a_multi_segment_session_renumbers_each_half_from_one(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, segments=2, seconds=4.0)

    first, second = split(session, spool=spool, at_seconds=5.0)

    assert [segment.index for segment in first.segments] == [1, 2]
    assert [segment.mic for segment in first.segments] == ["mic.opus", "mic.002.opus"]
    assert [segment.index for segment in second.segments] == [1]
    assert [segment.mic for segment in second.segments] == ["mic.opus"]
    assert first.duration_seconds == pytest.approx(5.0)
    assert second.duration_seconds == pytest.approx(3.0)
    # The second half starts one second into the second segment's wall clock.
    assert second.started_at == session.segments[1].started_at + timedelta(seconds=1)


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_titles_can_be_given_for_both_halves(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)

    first, second = split(
        session,
        spool=spool,
        at_seconds=2.0,
        titles=("Weekly quality sync", "Supplier audit follow-up"),
    )

    # Part 1 keeps the parent's minute, so its id takes the collision suffix.
    assert first.slug.startswith("weekly-quality-sync")
    assert second.slug == "supplier-audit-follow-up"
    assert second.id.endswith("supplier-audit-follow-up")


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_a_half_that_collides_with_its_parents_id_takes_a_suffix(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """Part 1 starts in the same minute as the parent and keeps its title, so
    the id it would take is the one the parent already has."""
    session = _captured_session(spool, two_track, seconds=4.0)

    first, _second = split(session, spool=spool, at_seconds=2.0)

    assert first.id != session.id
    assert first.id.endswith("-2")
    assert session.directory.exists(), "the parent directory is never reused"


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_splitting_the_same_session_twice_is_refused(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)
    first, second = split(session, spool=spool, at_seconds=2.0)

    with pytest.raises(SplitError, match="already split"):
        split(read_session(session.directory), spool=spool, at_seconds=2.0)

    assert sorted(spool.inbox_ids()) == sorted([first.id, second.id])


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_a_pending_parent_leaves_its_pending_reason_behind(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """Contracts section 3: ``pending_reason`` is non-null only in ``pending``.

    In the PoC every captured session lands in ``pending`` with a reason, so
    this is the state a split is most often made from.
    """
    session = _captured_session(spool, two_track, seconds=4.0)
    session.transition(
        "pending",
        by="munin-work",
        pending_reason="no transcription backend configured",
    )

    split(session, spool=spool, at_seconds=2.0)

    parent = read_session(session.directory)
    assert parent.state == "split"
    assert parent.pending_reason is None


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("recording", "stop the recording first"),
        ("done", "no longer describe the audio"),
        ("transcribing", "wait for it to finish"),
    ],
)
def test_a_session_in_the_wrong_state_is_refused(
    spool: Spool,
    two_track: Callable[..., tuple[Path, Path]],
    state: str,
    message: str,
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)
    session.state = state

    with pytest.raises(SplitError, match=message):
        split(session, spool=spool, at_seconds=2.0)


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_a_parent_that_moves_under_the_cut_loses_the_halves_not_itself(
    spool: Spool,
    two_track: Callable[..., tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``munin start --resume`` (D15) and the worker's 5 s sweep both run while
    ffmpeg is cutting. The parent is claimed against the state on disk, so
    losing that race costs the halves and leaves the meeting where it was."""
    session = _captured_session(spool, two_track, seconds=4.0)
    real = split_module._write_pieces

    def resumed_midway(part, source_dir, target_dir, ffmpeg):
        segments = real(part, source_dir, target_dir, ffmpeg)
        fresh = read_session(session.directory)
        if fresh.state == "captured":  # the daemon reopens it (D15)
            fresh.transition("recording", by="munin-rec")
        return segments

    monkeypatch.setattr(split_module, "_write_pieces", resumed_midway)

    with pytest.raises(SplitError, match="nothing was split"):
        split_module.split(session, spool=spool, at_seconds=2.0)

    parent = read_session(session.directory)
    assert parent.state == "recording", "the resume stands; the split does not"
    assert parent.split_into is None
    assert [s.id for s in spool.iter_sessions()] == [session.id]
    assert spool.inbox_ids() == [session.id]


@pytest.mark.skipif(FFMPEG is None, reason="the measurement needs ffprobe")
def test_a_session_recovered_after_a_crash_is_measured_before_it_is_cut(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """Spec 11's crash recovery leaves no segment durations, and a ``stopped_at``
    of the moment the next daemon started. Reading that as audio would put the
    cut hours away from where the user asked for it."""
    session = _captured_session(spool, two_track, seconds=4.0)
    segment = session.segments[0]
    segment.duration_seconds = None
    segment.stopped_at = segment.started_at + timedelta(hours=8)  # recovery time
    session.save()

    measured = split_module.measure_missing_durations(session)

    assert measured is True
    assert session.segments[0].duration_seconds == pytest.approx(4.0, abs=0.1)
    assert session.segments[0].stopped_at == segment.started_at + timedelta(
        seconds=session.segments[0].duration_seconds
    )
    # And the cut now lands inside the audio rather than inside eight hours of
    # wall clock that was never recorded.
    first, second = split(session, spool=spool, at_seconds=2.0)
    assert first.duration_seconds == pytest.approx(2.0, abs=0.1)
    assert second.duration_seconds == pytest.approx(2.0, abs=0.1)


def test_an_unmeasured_segment_is_never_guessed_from_the_clock(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)
    session.segments[0].duration_seconds = None
    session.segments[0].stopped_at = session.segments[0].started_at + timedelta(hours=8)

    with pytest.raises(SplitError, match="no measured duration"):
        plan(session, 2.0)


def test_the_halves_sum_to_the_parents_audio_not_to_its_wall_clock(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """A segment's ``started_at`` is stamped before the encoders are up, so its
    wall-clock span is longer than its ``duration_seconds``. The halves have to
    add up on the axis the transcript and the mix use, which is the audio."""
    session = _captured_session(spool, two_track, seconds=4.0)
    segment = session.segments[0]
    segment.stopped_at = segment.started_at + timedelta(seconds=4.5)  # start-up
    segment.duration_seconds = 4.0

    first, second = plan(session, 2.0)

    assert first.duration_seconds == pytest.approx(2.0)
    assert second.duration_seconds == pytest.approx(2.0)
    assert first.duration_seconds + second.duration_seconds == pytest.approx(
        session.audio_duration_seconds
    )


def test_a_cut_a_fraction_from_a_segment_edge_snaps_to_the_edge(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """Reachable from ordinary input -- ``--clock`` on a segment's start minute,
    or an offset typed from a rounded duration. Unsnapped it asks ffmpeg for a
    piece of a millisecond, which writes a zero-byte file and fails the split."""
    session = _captured_session(spool, two_track, segments=2, seconds=4.0)

    first, second = plan(session, 4.01)

    assert [piece.whole for piece in first.pieces] == [True]
    assert [piece.whole for piece in second.pieces] == [True]
    assert first.duration_seconds == pytest.approx(4.0)


def test_a_cut_a_fraction_from_the_recording_is_refused(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)

    with pytest.raises(SplitError, match="nothing in one of the halves"):
        plan(session, 0.01)


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_a_half_is_not_a_session_until_it_is_a_whole_one(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """``munin-work`` claims every ``captured`` session it finds by scanning for
    ``session.json``, and it sweeps every 5 s. A record written beside audio
    still being copied would be claimed mid-cut."""
    session = _captured_session(spool, two_track, seconds=4.0)
    seen: list[list[str]] = []
    real = split_module._write_pieces

    def watch(part, source_dir, target_dir, ffmpeg):
        # What the worker would find if it swept right now, mid-copy.
        seen.append([s.id for s in spool.iter_sessions()])
        return real(part, source_dir, target_dir, ffmpeg)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(split_module, "_write_pieces", watch)
    try:
        first, second = split_module.split(session, spool=spool, at_seconds=2.0)
    finally:
        monkeypatch.undo()

    assert seen == [[session.id], [session.id]], "only the parent was visible"
    assert sorted(s.id for s in spool.iter_sessions()) == sorted(
        [session.id, first.id, second.id]
    )


# --- through the CLI, where the exit codes are the contract ------------


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_the_cli_cuts_and_reports_both_halves(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]], capsys
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)

    code = cli.main(["split", session.id, "00:00:02", "--title", "Supplier audit"])
    out = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert "part 1" in out and "part 2" in out
    assert "Supplier audit" in out
    assert read_session(session.directory).state == "split"


def test_the_cli_exits_two_when_the_cut_point_is_not_a_time(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]], capsys
) -> None:
    """Section 11: a bad argument is exit 2, the same as any other."""
    session = _captured_session(spool, two_track, seconds=4.0)

    code = cli.main(["split", session.id, "half past two"])

    assert code == cli.EXIT_USAGE
    assert "is not a time" in capsys.readouterr().err


def test_the_cli_exits_two_on_a_clock_that_is_not_a_time_of_day(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]], capsys
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)

    code = cli.main(["split", session.id, "--clock", "25:70"])

    assert code == cli.EXIT_USAGE


def test_the_cli_exits_four_when_the_session_cannot_be_split(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]], capsys
) -> None:
    """A precondition, not a usage error: nothing was attempted."""
    session = _captured_session(spool, two_track, seconds=4.0)
    session.transition("pending", by="munin-work")
    session.transition("transcribing", by="munin-work")

    code = cli.main(["split", session.id, "00:00:02"])

    assert code == cli.EXIT_PRECONDITION
    assert "wait for it to finish" in capsys.readouterr().err


def test_the_cli_exits_four_when_the_cut_is_outside_the_audio(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]], capsys
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)

    code = cli.main(["split", session.id, "01:00:00"])

    assert code == cli.EXIT_PRECONDITION
    assert "inside the recording" in capsys.readouterr().err


@pytest.mark.skipif(FFMPEG is None, reason="the cut and the mix need ffmpeg")
def test_mix_all_leaves_a_split_parent_alone(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]], capsys
) -> None:
    """The halves are the meetings; the parent holds both. Mixing all three
    would put the merged recording in the upload folder beside them (D26)."""
    session = _captured_session(spool, two_track, seconds=4.0)
    first, second = split(session, spool=spool, at_seconds=2.0)

    code = cli.main(["mix", "--all"])
    out = capsys.readouterr().out

    assert code == cli.EXIT_OK
    mixed = [line.split()[0] for line in out.splitlines() if line.strip()]
    # Part 1's id starts with the parent's, so compare whole ids, not substrings.
    assert sorted(mixed) == sorted([first.id, second.id])
    assert not (session.directory / "mixed.opus").exists()


@pytest.mark.skipif(FFMPEG is None, reason="the cut and the mix need ffmpeg")
def test_the_worker_leaves_a_split_parent_alone(
    munin_home: Path,
    tmp_path: Path,
    spool: Spool,
    two_track: Callable[..., tuple[Path, Path]],
) -> None:
    """The same rule as `munin mix --all`, and the one the sweep was missing:
    the parent holds both meetings, so exporting it puts the merged recording in
    the upload folder beside the two it was cut into (D26).
    """
    from munin.worker import Worker

    session = _captured_session(spool, two_track, seconds=4.0)
    first, second = split(session, spool=spool, at_seconds=2.0)
    destination = tmp_path / "uploads"
    (munin_home / "config.toml").write_text(
        f'[export]\nenabled = true\ndirectory = "{destination}"\n', encoding="utf-8"
    )
    config = config_module.load()

    Worker(config).drain()

    exported = sorted(path.stem for path in destination.glob("*.opus"))
    assert exported == sorted([first.id, second.id])
    assert not (destination / f"{session.id}.opus").exists()
    assert not (session.directory / "mixed.opus").exists()


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_a_full_disk_on_the_second_half_leaves_no_orphan(
    spool: Spool,
    two_track: Callable[..., tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disk guard lives in `derive`, and `NoSpaceError` is a `StateError`,
    not an `OSError`. Part 1's audio is on disk by the time part 2 asks for
    room, and a directory with no session.json is invisible to the spool and
    never retried -- so the rollback has to cover this path too.
    """
    from munin.spool import NoSpaceError

    session = _captured_session(spool, two_track, seconds=4.0)
    before = {path.name for path in spool.recordings.rglob("*") if path.is_dir()}
    real_derive = Spool.derive
    calls = {"n": 0}

    def _second_one_has_no_room(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise NoSpaceError("400 MB free below the minimum, refusing to write")
        return real_derive(self, **kwargs)

    monkeypatch.setattr(Spool, "derive", _second_one_has_no_room)

    with pytest.raises(NoSpaceError):
        split(session, spool=spool, at_seconds=2.0)

    after = {path.name for path in spool.recordings.rglob("*") if path.is_dir()}
    assert after == before, "part 1's directory was left behind"
    parent = read_session(session.directory)
    assert parent.state == "captured"
    assert parent.split_into is None
    assert spool.inbox_ids() == [session.id]
    assert [s.id for s in spool.iter_sessions()] == [session.id]


def test_a_missing_ffmpeg_is_a_run_failure_not_a_precondition(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    session = _captured_session(spool, two_track, seconds=4.0)

    with pytest.raises(SplitFailed, match="not installed"):
        split(session, spool=spool, at_seconds=2.0, ffmpeg="munin-no-such-ffmpeg")

    assert read_session(session.directory).state == "captured"


@pytest.mark.skipif(FFMPEG is None, reason="the cut needs ffmpeg")
def test_a_cut_that_fails_halfway_leaves_the_parent_queued(
    spool: Spool, two_track: Callable[..., tuple[Path, Path]]
) -> None:
    """Part 1 writes, part 2 cannot: the parent must still be the one queued,
    and no half-written session may be left pointing at a meeting."""
    session = _captured_session(spool, two_track, segments=2, seconds=4.0)
    (session.directory / "app.002.opus").unlink()

    with pytest.raises(SplitError, match="missing"):
        split(session, spool=spool, at_seconds=5.0)

    parent = read_session(session.directory)
    assert parent.state == "captured"
    assert parent.split_into is None
    assert spool.inbox_ids() == [session.id]
    assert [s.id for s in spool.iter_sessions()] == [session.id]
