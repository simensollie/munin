"""``mixed.mp3``: the one-file copy a manual upload needs (spec 10).

The argv tests pin the filter graph, because its three departures from the
literal command in spec 10 (no ``amix`` normalisation, a limiter, latency
compensation) are the whole reason the mix is usable by an ASR model. The rest
run real ffmpeg over the synthetic two-track fixture and measure the result:
both tones have to survive into the mix, or the file is one track with extra
steps -- and the Opus default has to actually be the smaller file, or there was
no reason to leave spec 10's MP3 behind.
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
from munin.capture.base import segment_filenames
from munin.mixdown import (
    DEFAULT_FORMAT,
    FORMATS,
    MixdownError,
    exported_ids,
    existing_mixes,
    export_filename,
    export_name,
    forget_export,
    is_exported,
    ledger_entry,
    mark_exported,
    mix_argv,
    mix_format,
    mixdown,
    mixed_filename,
    mixed_path,
)
from munin.spool import Session, Spool

FFMPEG = shutil.which("ffmpeg")

OSLO = timezone(timedelta(hours=2))
WHEN = datetime(2026, 9, 14, 13, 25, 7, tzinfo=OSLO)

#: The fixture's two tones (tests/conftest.py), an octave apart.
MIC_HZ = 440
APP_HZ = 880

#: Any band this far down in a 64 kbps mono mix holds nothing that was recorded.
SILENT_DB = -60.0


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
    seconds: float = 2.0,
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
        started = stopped + timedelta(minutes=5)  # a resume gap (D15)
    session.transition("ending", by="munin-rec")
    session.transition("captured", by="munin-rec", stopped_at=stopped)
    return session


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


def _duration(path: Path) -> float:
    assert FFMPEG
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


# --- the filter graph -------------------------------------------------


def test_one_segment_takes_two_inputs_and_no_concat() -> None:
    argv = mix_argv([(Path("mic.opus"), Path("app.opus"))], Path("mixed.mp3"))

    assert argv.count("-i") == 2
    graph = argv[argv.index("-filter_complex") + 1]
    assert "concat" not in graph
    assert "amix=inputs=2" in graph


def test_every_segment_is_concatenated_before_the_sum() -> None:
    pairs = [
        (Path("mic.opus"), Path("app.opus")),
        (Path("mic.002.opus"), Path("app.002.opus")),
    ]
    argv = mix_argv(pairs, Path("mixed.mp3"))
    graph = argv[argv.index("-filter_complex") + 1]

    assert argv.count("-i") == 4
    # Each track is joined along its own chain, then the two are summed once:
    # concatenating after the sum would interleave the meeting with itself.
    assert graph.count("concat=n=2") == 2
    assert "[t0][t2]concat" in graph and "[t1][t3]concat" in graph
    assert "[mic][app]amix=inputs=2" in graph


def test_the_sum_does_not_halve_either_track() -> None:
    """``normalize=0`` is the departure from spec 10's literal command."""
    argv = mix_argv([(Path("mic.opus"), Path("app.opus"))], Path("mixed.mp3"))
    graph = argv[argv.index("-filter_complex") + 1]

    assert "normalize=0" in graph
    # A limiter, not a normaliser: it must not lift the quiet passages.
    assert "alimiter=" in graph and "level=false" in graph
    # ... and must not shift the mix off the tracks' time axis (spec 7.5).
    assert "latency=true" in graph


def test_the_default_output_is_mono_opus_at_the_track_bitrate() -> None:
    """24 kbps is what the tracks were captured at; the mix carries no less."""
    argv = mix_argv([(Path("mic.opus"), Path("app.opus"))], Path("out.opus"))

    assert argv[argv.index("-c:a") + 1] == "libopus"
    assert argv[argv.index("-b:a") + 1] == "24k"
    assert argv[argv.index("-ac") + 1] == "1"
    # Speech mode: the mix exists to be read by an ASR model.
    assert argv[argv.index("-application") + 1] == "voip"
    # The muxer is named because the encode goes to a ``.part`` file first.
    assert argv[argv.index("-f") + 1] == "opus"
    assert argv[-1] == "out.opus"


def test_mp3_stays_available_at_spec_tens_bitrate() -> None:
    """Appendix A takes MP3 or OPUS; MP3 is the fallback if an importer refuses."""
    argv = mix_argv([(Path("mic.opus"), Path("app.opus"))], Path("out.mp3"), fmt="mp3")

    assert argv[argv.index("-c:a") + 1] == "libmp3lame"
    assert argv[argv.index("-b:a") + 1] == "64k"
    assert argv[argv.index("-f") + 1] == "mp3"
    assert "-application" not in argv


def test_an_unknown_format_names_the_known_ones() -> None:
    with pytest.raises(MixdownError, match="mp3"):
        mix_format("flac")


def test_every_format_plaud_accepts_is_offered() -> None:
    """Appendix A: the upload API takes file_type MP3 or OPUS, nothing else."""
    assert set(FORMATS) == {"opus", "mp3"}
    assert DEFAULT_FORMAT == "opus"
    assert mixed_filename() == "mixed.opus"


def test_no_pairs_is_an_error() -> None:
    with pytest.raises(MixdownError):
        mix_argv([], Path("mixed.mp3"))


# --- the real thing ---------------------------------------------------


def test_the_mix_holds_both_tracks(spool: Spool, two_track) -> None:
    session = _captured_session(spool, two_track)

    path = mixdown(session)

    assert path == session.directory / "mixed.opus"
    assert path.exists() and path.stat().st_size > 0
    assert _duration(path) == pytest.approx(2.0, abs=0.15)
    # Both tones are present: the mic track alone would leave 880 Hz silent.
    assert _band_db(path, MIC_HZ) > SILENT_DB
    assert _band_db(path, APP_HZ) > SILENT_DB


def test_a_resumed_session_mixes_to_the_summed_duration(spool: Spool, two_track) -> None:
    """Segment audio is joined end to end; the wall-clock gap is not re-recorded."""
    session = _captured_session(spool, two_track, segments=2)

    path = mixdown(session)

    assert _duration(path) == pytest.approx(4.0, abs=0.25)


def test_an_existing_mix_is_kept_unless_forced(spool: Spool, two_track) -> None:
    session = _captured_session(spool, two_track)
    first = mixdown(session)
    stamp = first.stat().st_mtime_ns

    assert mixdown(session).stat().st_mtime_ns == stamp
    assert mixdown(session, force=True).stat().st_mtime_ns != stamp


def test_a_session_still_being_written_is_refused(spool: Spool, two_track) -> None:
    session = _captured_session(spool, two_track)
    session.state = "recording"

    with pytest.raises(MixdownError, match="recording"):
        mixdown(session)
    assert not mixed_path(session).exists()


def test_a_missing_track_is_named(spool: Spool, two_track) -> None:
    session = _captured_session(spool, two_track)
    (session.directory / "app.opus").unlink()

    with pytest.raises(MixdownError, match="app.opus"):
        mixdown(session)


def test_a_failed_encode_leaves_nothing_behind(
    spool: Spool, two_track, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated mix would look finished to the next run, so it must not survive."""
    session = _captured_session(spool, two_track)

    def fail(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"\x00" * 16)  # a part file, as ffmpeg would
        return subprocess.CompletedProcess(argv, 1, "", "Error: no such filter\n")

    monkeypatch.setattr("munin.mixdown.subprocess.run", fail)

    with pytest.raises(MixdownError, match="no such filter"):
        mixdown(session)
    assert not mixed_path(session).exists()
    assert list(session.directory.glob("*.part")) == []


def test_without_ffmpeg_the_error_says_so(
    spool: Spool, two_track, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _captured_session(spool, two_track)
    monkeypatch.setattr("munin.mixdown.shutil.which", lambda _tool: None)

    with pytest.raises(MixdownError, match="not installed"):
        mixdown(session)


# --- the export name --------------------------------------------------


def test_the_export_name_is_the_session_directory_name(spool: Spool) -> None:
    session_id = "2026-09-14T1325-weekly-quality-sync"

    assert export_filename(session_id) == "2026-09-14T1325-weekly-quality-sync.opus"
    assert export_filename(session_id, "mp3") == "2026-09-14T1325-weekly-quality-sync.mp3"


def test_the_export_name_sorts_chronologically() -> None:
    """ISO 8601 up front is why the upload folder needs no sorting by hand."""
    assert export_filename("2026-09-14T1325-review") < export_filename(
        "2026-09-14T1610-review"
    )


# --- the command ------------------------------------------------------


def test_mix_defaults_to_the_most_recent_session(spool: Spool, two_track, capsys) -> None:
    session = _captured_session(spool, two_track)

    assert cli.main(["mix"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert session.id in out and "mixed.opus" in out
    assert mixed_path(session).exists()


def test_mix_all_covers_every_finished_session(spool: Spool, two_track, capsys) -> None:
    first = _captured_session(spool, two_track, title="Weekly quality sync")
    second = _captured_session(spool, two_track, title="Supplier review")

    assert cli.main(["mix", "--all"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert first.id in out and second.id in out
    assert mixed_path(first).exists() and mixed_path(second).exists()


def test_mix_copies_to_an_upload_folder_under_the_session_name(
    spool: Spool, two_track, tmp_path: Path, capsys
) -> None:
    session = _captured_session(spool, two_track, title="Weekly quality sync")
    target = tmp_path / "plaud-upload"

    assert cli.main(["mix", session.id, "--to", str(target)]) == cli.EXIT_OK

    # Spec 10: the export carries the session directory name, so the file pairs
    # with the session that produced it. An importer stamps an upload with the
    # upload time and renames it to its own generated title only once a summary
    # finishes, so the filename is the only carrier of the meeting's own clock.
    assert (target / export_name(session)).exists()
    assert session.id.startswith("2026-09-14T1325")
    # The original stays in the session directory; the copy is for the browser.
    assert mixed_path(session).exists()


def test_mix_falls_back_to_the_configured_export_folder(
    munin_home: Path, spool: Spool, two_track, tmp_path: Path, capsys
) -> None:
    """A bare `munin mix` refills the same folder the worker does, so doing it
    by hand and letting it happen produce the same files (amendment 2026-09-18).
    """
    target = tmp_path / "uploads"
    (munin_home / "config.toml").write_text(
        f'[export]\nenabled = true\ndirectory = "{target}"\n', encoding="utf-8"
    )
    session = _captured_session(spool, two_track, title="Weekly quality sync")

    assert cli.main(["mix", session.id]) == cli.EXIT_OK

    assert (target / export_name(session)).exists()


def test_mix_without_an_enabled_export_copies_nowhere(
    munin_home: Path, spool: Spool, two_track, capsys
) -> None:
    session = _captured_session(spool, two_track, title="Weekly quality sync")

    assert cli.main(["mix", session.id]) == cli.EXIT_OK

    assert mixed_path(session).exists()
    assert "->" not in capsys.readouterr().out


def test_mix_format_follows_the_configured_export_format(
    munin_home: Path, spool: Spool, two_track, tmp_path: Path, capsys
) -> None:
    target = tmp_path / "uploads"
    (munin_home / "config.toml").write_text(
        f'[export]\nenabled = true\ndirectory = "{target}"\nformat = "mp3"\n',
        encoding="utf-8",
    )
    session = _captured_session(spool, two_track, title="Weekly quality sync")

    assert cli.main(["mix", session.id]) == cli.EXIT_OK

    assert (target / export_name(session, "mp3")).exists()
    # The flag still wins over the config.
    assert cli.main(["mix", session.id, "--format", "opus"]) == cli.EXIT_OK
    assert (target / export_name(session)).exists()


def test_mix_refuses_an_unknown_session(spool: Spool, capsys) -> None:
    assert cli.main(["mix", "2026-01-01T0000-no-such-meeting"]) == cli.EXIT_USAGE
    assert "no session" in capsys.readouterr().err


def test_mix_with_an_empty_spool_is_a_precondition(munin_home: Path, capsys) -> None:
    assert cli.main(["mix"]) == cli.EXIT_PRECONDITION
    assert "no sessions" in capsys.readouterr().err


def test_mix_reports_a_broken_session_without_abandoning_the_rest(
    spool: Spool, two_track, capsys
) -> None:
    broken = _captured_session(spool, two_track, title="Missing track")
    (broken.directory / "app.opus").unlink()
    intact = _captured_session(spool, two_track, title="Weekly quality sync")

    assert cli.main(["mix", "--all"]) == cli.EXIT_ERROR

    captured = capsys.readouterr()
    assert broken.id in captured.err
    assert intact.id in captured.out
    assert mixed_path(intact).exists()

# --- the two formats side by side -------------------------------------


def test_opus_is_the_smaller_file_and_holds_both_tracks(spool: Spool, two_track) -> None:
    """The whole reason for leaving spec 10's MP3 as the default."""
    session = _captured_session(spool, two_track, seconds=6.0)

    as_opus = mixdown(session)
    as_mp3 = mixdown(session, fmt="mp3")

    assert as_opus.stat().st_size < as_mp3.stat().st_size
    assert _band_db(as_opus, MIC_HZ) > SILENT_DB
    assert _band_db(as_opus, APP_HZ) > SILENT_DB
    assert _duration(as_opus) == pytest.approx(6.0, abs=0.15)


def test_the_two_formats_do_not_displace_each_other(spool: Spool, two_track) -> None:
    """A session mixed to MP3 last month may be the file already uploaded."""
    session = _captured_session(spool, two_track)

    mixdown(session, fmt="mp3")
    mixdown(session, fmt="opus")

    assert {path.name for path in existing_mixes(session)} == {"mixed.opus", "mixed.mp3"}


def test_mix_format_mp3_writes_the_fallback(spool: Spool, two_track, capsys) -> None:
    session = _captured_session(spool, two_track)

    assert cli.main(["mix", "--format", "mp3"]) == cli.EXIT_OK

    assert (session.directory / "mixed.mp3").exists()
    assert not (session.directory / "mixed.opus").exists()
    assert "mixed.mp3" in capsys.readouterr().out


def test_mix_rejects_a_format_plaud_does_not_take(spool: Spool) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["mix", "--format", "flac"])


# --- the upload folder's ledger ---------------------------------------


def test_the_ledger_is_empty_until_something_is_exported(tmp_path: Path) -> None:
    folder = tmp_path / "plaud-upload"
    assert exported_ids(folder) == []
    assert not is_exported(folder, "2026-09-14T1325-weekly-quality-sync")
    assert not forget_export(folder, "2026-09-14T1325-weekly-quality-sync")


def test_marking_creates_the_folder_and_forgetting_undoes_it(tmp_path: Path) -> None:
    folder = tmp_path / "plaud-upload"
    entry = mark_exported(folder, "2026-09-14T1325-weekly-quality-sync", filename="x.opus")

    assert entry == ledger_entry(folder, "2026-09-14T1325-weekly-quality-sync")
    assert is_exported(folder, "2026-09-14T1325-weekly-quality-sync")
    assert exported_ids(folder) == ["2026-09-14T1325-weekly-quality-sync"]
    assert forget_export(folder, "2026-09-14T1325-weekly-quality-sync")
    assert not is_exported(folder, "2026-09-14T1325-weekly-quality-sync")


def test_the_ledger_sorts_chronologically(tmp_path: Path) -> None:
    """Session ids start with their own date and time, so plain name order is
    meeting order -- worth pinning, since it is why nothing stores a timestamp
    to sort by."""
    folder = tmp_path / "plaud-upload"
    for session_id in ("2026-09-18T0900-b", "2026-09-14T1325-a", "2026-09-18T1200-c"):
        mark_exported(folder, session_id)

    assert exported_ids(folder) == [
        "2026-09-14T1325-a",
        "2026-09-18T0900-b",
        "2026-09-18T1200-c",
    ]


def test_mix_to_a_folder_marks_the_session_exported(
    spool: Spool, two_track, tmp_path: Path, capsys
) -> None:
    """By hand or by the worker, a copy into the folder is a turn in the folder;
    otherwise deleting a hand-made copy would summon a worker-made one.
    """
    session = _captured_session(spool, two_track, title="Weekly quality sync")
    target = tmp_path / "plaud-upload"

    assert cli.main(["mix", session.id, "--to", str(target)]) == cli.EXIT_OK

    assert is_exported(target, session.id)


def test_mix_all_skips_what_the_folder_already_carried(
    spool: Spool, two_track, tmp_path: Path, capsys
) -> None:
    """Without this, one `munin mix --all` undoes the ledger by refilling the
    folder with every meeting ever uploaded and cleared.
    """
    target = tmp_path / "plaud-upload"
    old = _captured_session(spool, two_track, title="Supplier review")
    assert cli.main(["mix", old.id, "--to", str(target)]) == cli.EXIT_OK
    (target / export_name(old)).unlink()
    fresh = _captured_session(spool, two_track, title="Weekly quality sync")
    capsys.readouterr()

    assert cli.main(["mix", "--all", "--to", str(target)]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert (target / export_name(fresh)).exists()
    assert not (target / export_name(old)).exists()
    assert "1 already exported" in out


def test_naming_a_session_puts_back_a_file_deleted_by_accident(
    spool: Spool, two_track, tmp_path: Path, capsys
) -> None:
    """The escape hatch the ledger needs: nothing self-heals any more, so a
    deliberate re-export has to be one command. A session named on the command
    line is that ask -- only `--all` consults the ledger.
    """
    target = tmp_path / "plaud-upload"
    session = _captured_session(spool, two_track, title="Weekly quality sync")
    assert cli.main(["mix", session.id, "--to", str(target)]) == cli.EXIT_OK
    (target / export_name(session)).unlink()

    assert cli.main(["mix", session.id, "--to", str(target)]) == cli.EXIT_OK

    assert (target / export_name(session)).exists()


def test_mix_all_force_refills_the_whole_folder(
    spool: Spool, two_track, tmp_path: Path, capsys
) -> None:
    target = tmp_path / "plaud-upload"
    session = _captured_session(spool, two_track, title="Weekly quality sync")
    assert cli.main(["mix", "--all", "--to", str(target)]) == cli.EXIT_OK
    (target / export_name(session)).unlink()

    assert cli.main(["mix", "--all", "--to", str(target), "--force"]) == cli.EXIT_OK

    assert (target / export_name(session)).exists()


# -- export_name: what the importer will call the meeting for good ---------------


class _Named:
    def __init__(self, session_id: str, title: str, enrichment: dict | None = None) -> None:
        self.id = session_id
        self.title = title
        self.enrichment = enrichment


@pytest.mark.parametrize(
    ("session_id", "title", "expected"),
    [
        # The stamp first, then the title as a person wrote it.
        ("2026-09-23T0901-qms-risk-review", "QMS risk review", "2026-09-23T0901 QMS risk review.opus"),
        # A clock fallback loses the clock the stamp already carries.
        ("2026-09-24T0906-microsoft-teams-09-06", "Microsoft Teams 09:06", "2026-09-24T0906 Microsoft Teams.opus"),
        # A ": " separator reads as a dash; other refused characters go.
        ("2026-09-24T0906-flutter", "Flutter sharing: mobile app", "2026-09-24T0906 Flutter sharing - mobile app.opus"),
        ("2026-09-24T0906-a-b", "Q3/Q4 plan? <draft>", "2026-09-24T0906 Q3 Q4 plan draft.opus"),
        # Nothing left of the title: the id, as before.
        ("2026-09-24T0906-adhoc", "???", "2026-09-24T0906-adhoc.opus"),
    ],
)
def test_export_name_is_stamp_and_title(session_id: str, title: str, expected: str) -> None:
    assert export_name(_Named(session_id, title)) == expected


def test_export_name_tells_same_minute_twins_apart() -> None:
    """The halves of a live split share a title and a minute (D26)."""
    first = _Named("2026-09-14T1325-weekly-quality-sync", "Weekly quality sync")
    second = _Named("2026-09-14T1325-weekly-quality-sync-2", "Weekly quality sync")
    assert export_name(first) == "2026-09-14T1325 Weekly quality sync.opus"
    assert export_name(second) == "2026-09-14T1325 Weekly quality sync (2).opus"


def test_a_title_ending_in_a_number_is_not_a_twin() -> None:
    assert export_name(_Named("2026-09-14T1325-sprint-2", "Sprint 2")) == "2026-09-14T1325 Sprint 2.opus"


def test_a_renamed_twin_keeps_its_suffix() -> None:
    twin = _Named(
        "2026-09-14T1325-microsoft-teams-13-25-2",
        "Ola Nordmann",
        {"title_from": "direct-call", "original_title": "Microsoft Teams 13:25"},
    )
    assert export_name(twin) == "2026-09-14T1325 Ola Nordmann (2).opus"


def test_a_long_subject_is_cut_at_a_word() -> None:
    name = export_name(_Named("2026-09-14T1325-x", "word " * 40))
    assert len(name) < 120 and name.endswith("word.opus")
