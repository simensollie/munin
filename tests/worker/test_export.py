"""Auto-export: the worker refilling the upload folder (contracts 2026-09-18).

The feature exists because the manual version silently stopped happening --
three captured meetings sat unexported for a day, and nothing could report it,
since the mix is deliberately absent from the session record. So the tests that
matter here are not "does it copy a file" but the three properties that make an
unattended copy safe to run every five seconds: it does nothing unless asked, it
never writes to ``session.json``, and a failure is a logged line rather than a
dead worker.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from munin import config as config_module
from munin import mixdown
from munin.mixdown import export_name
from munin.capture.base import segment_filenames
from munin.spool import Session, Spool
from munin.worker import Worker

FFMPEG = shutil.which("ffmpeg")

OSLO = timezone(timedelta(hours=2))
WHEN = datetime(2026, 9, 14, 13, 25, 7, tzinfo=OSLO)

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="the mix needs ffmpeg")


def _config(munin_home: Path, body: str = "") -> config_module.Config:
    (munin_home / "config.toml").write_text(body, encoding="utf-8")
    return config_module.load()


def _enabled(munin_home: Path, destination: Path, fmt: str = "opus"):
    return _config(
        munin_home,
        f'[export]\nenabled = true\ndirectory = "{destination}"\nformat = "{fmt}"\n',
    )


def _captured_session(
    spool: Spool,
    two_track: Callable[..., tuple[Path, Path]],
    *,
    seconds: float = 2.0,
) -> Session:
    """A finished session with real audio on disk."""
    session = spool.create(title="Weekly quality sync", source="adhoc", now=WHEN)
    mic_name, app_name = segment_filenames(1)
    spool.add_segment(session, 1, started_at=WHEN)
    two_track(session.directory, seconds=seconds, mic_name=mic_name, app_name=app_name)
    stopped = WHEN + timedelta(seconds=seconds)
    spool.complete_segment(session, 1, stopped_at=stopped, duration_seconds=seconds)
    session.transition("ending", by="munin-rec")
    session.transition("captured", by="munin-rec", stopped_at=stopped)
    return session


@pytest.fixture()
def ample_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    gib = 1024**3
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: type("U", (), {"total": 500 * gib, "used": 0, "free": 400 * gib})(),
    )


# -- off by default ---------------------------------------------------------


def test_nothing_is_copied_unless_export_is_enabled(
    munin_home: Path, two_track: Callable[..., tuple[Path, Path]], ample_disk: None
) -> None:
    """The shipped default stages nothing for a third party (D11, spec 12)."""
    config = _config(munin_home)
    spool = Spool(config)
    session = _captured_session(spool, two_track)

    assert Worker(config).export(session) is None
    assert not mixdown.mixed_path(session).exists()


# -- the copy ---------------------------------------------------------------


def test_an_enabled_export_mixes_and_copies_under_the_session_name(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)

    copy = Worker(config).export(session)

    assert copy == destination / export_name(session)
    assert copy.exists() and copy.stat().st_size > 0
    # Stamped with the meeting's own time, because the importer stamps its own
    # date at upload and the meeting's exists nowhere else (spec 10), then the
    # title, because the importer keeps the filename as the title for good.
    assert copy.name == "2026-09-14T1325 Weekly quality sync.opus"
    assert mixdown.mixed_path(session).exists()


def test_the_destination_directory_is_created(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    destination = tmp_path / "not" / "there" / "yet"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)

    assert Worker(config).export(session) is not None
    assert destination.is_dir()


def test_the_configured_format_is_the_one_written(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination, fmt="mp3")
    spool = Spool(config)
    session = _captured_session(spool, two_track)

    copy = Worker(config).export(session)

    assert copy is not None and copy.suffix == ".mp3"


# -- safe to run every five seconds -----------------------------------------


def test_a_session_is_exported_once_and_not_again(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """The destination file existing is the whole of the bookkeeping, so the
    second sweep must be a no-op rather than a re-encode.
    """
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)
    worker = Worker(config)

    first = worker.export(session)
    assert first is not None
    stamped = first.stat().st_mtime_ns

    assert worker.export(session) is None
    assert first.stat().st_mtime_ns == stamped


def test_exporting_writes_nothing_to_the_session_record(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """Contracts 7.2: no field, no state transition, no history entry. A
    re-encode of audio that already exists is not a capture event.
    """
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)
    record = session.directory / "session.json"
    before = json.loads(record.read_text(encoding="utf-8"))

    Worker(config).export(session)

    after = json.loads(record.read_text(encoding="utf-8"))
    assert after == before
    assert after["state"] == "captured"
    assert len(after["history"]) == len(before["history"])


def test_a_recording_in_progress_is_left_alone(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """Its audio files are still being written to (mixdown.BUSY_STATES)."""
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = spool.create(title="Live", source="adhoc", now=WHEN)
    spool.add_segment(session, 1, started_at=WHEN)
    assert session.state == "recording"

    assert Worker(config).export(session) is None
    assert not destination.exists()


def test_a_failed_export_is_a_logged_line_not_an_exception(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript arriving late beats a worker that died exporting (spec 11)."""
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)

    def _boom(*args: object, **kwargs: object) -> Path:
        raise mixdown.MixdownError("ffmpeg is not installed; the mix needs it")

    monkeypatch.setattr(mixdown, "mixdown", _boom)

    with caplog.at_level("WARNING"):
        assert Worker(config).export(session) is None

    assert "export failed" in caplog.text
    assert not (destination / export_name(session)).exists()


# -- the sweep --------------------------------------------------------------


def test_a_sweep_exports_everything_the_folder_is_missing(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """The failure this feature exists for: sessions captured while nothing was
    exporting must be picked up, not only the newest one.
    """
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    sessions = [_captured_session(spool, two_track) for _ in range(3)]

    Worker(config).drain()

    exported = sorted(path.name for path in destination.iterdir() if path.is_file())
    assert exported == sorted(export_name(session) for session in sessions)


def test_a_sweep_still_exports_a_session_already_queued_as_pending(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """With backend = "none" every session parks at pending with a reason, and
    the sweep skips those for transcription. The export must not be skipped with
    them, or enabling it would only ever catch a session's first sweep.

    Export is turned on between the two sweeps rather than deleting the copy
    after the first: a deleted copy is now a finished upload and stays gone.
    """
    destination = tmp_path / "uploads"
    spool = Spool(_config(munin_home))
    session = _captured_session(spool, two_track)

    Worker(_config(munin_home)).drain()  # captured -> pending, export off

    reloaded = Spool(_enabled(munin_home, destination)).find(session.id)
    assert reloaded is not None and reloaded.state == "pending"
    Worker(_enabled(munin_home, destination)).drain()

    assert (destination / export_name(session)).exists()


# -- exported once, ever ----------------------------------------------------


def test_a_file_deleted_after_upload_is_not_put_back(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """The point of the ledger. Uploading a meeting and clearing the file is how
    an upload ends; before this the next sweep read the absence as "never
    exported" and refilled the folder behind the user.
    """
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)
    worker = Worker(config)

    worker.drain()
    copy = destination / export_name(session)
    assert copy.exists()
    assert mixdown.is_exported(destination, session.id)

    copy.unlink()  # uploaded, then cleared
    worker.drain()
    worker.drain()

    assert not copy.exists()


def test_a_file_moved_out_of_the_folder_is_not_put_back(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """Moving is the other half of the same habit -- an archive folder instead
    of the bin -- and it has to behave the same way.
    """
    destination = tmp_path / "uploads"
    archive = tmp_path / "archive"
    archive.mkdir()
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)
    worker = Worker(config)

    worker.drain()
    copy = destination / export_name(session)
    copy.rename(archive / copy.name)

    worker.drain()

    assert not copy.exists()


def test_a_folder_filled_before_the_ledger_existed_is_backfilled(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """Upgrade path. A file the folder is already carrying was exported, marker
    or no marker; without this every file exported before the change would have
    come back once more after its upload.
    """
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)
    destination.mkdir(parents=True, exist_ok=True)
    copy = destination / export_name(session)
    copy.write_bytes(b"exported before the ledger existed")

    Worker(config).drain()

    assert mixdown.is_exported(destination, session.id)
    assert copy.read_bytes() == b"exported before the ledger existed"

    copy.unlink()
    Worker(config).drain()
    assert not copy.exists()


def test_a_failed_export_leaves_no_marker(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker records a copy that landed. Writing it before the copy would
    turn one transient ffmpeg failure into a meeting that never exports.
    """
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)

    def _boom(*args, **kwargs):
        raise mixdown.MixdownError("ffmpeg fell over")

    monkeypatch.setattr(mixdown, "mixdown", _boom)
    assert Worker(config).export(session) is None
    assert not mixdown.is_exported(destination, session.id)

    monkeypatch.undo()
    assert Worker(config).export(session) is not None
    assert mixdown.is_exported(destination, session.id)


def test_the_ledger_is_not_per_format(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """Switching [export] format is not a reason to re-export the archive. A
    session that has had its turn in the folder has had it, whatever the
    extension; a second format is `munin mix --force`, not a config edit.
    """
    destination = tmp_path / "uploads"
    spool = Spool(_enabled(munin_home, destination))
    session = _captured_session(spool, two_track)
    Worker(_enabled(munin_home, destination)).drain()
    (destination / export_name(session)).unlink()

    Worker(_enabled(munin_home, destination, fmt="mp3")).drain()

    assert not (destination / export_name(session, "mp3")).exists()


def test_the_marker_says_when_and_which_file(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """A zero-byte marker answers "was it exported" and nothing else; the first
    question after that is always "when".
    """
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)

    Worker(config).drain()

    body = mixdown.ledger_entry(destination, session.id).read_text(encoding="utf-8")
    stamp, _, filename = body.strip().partition("\t")
    assert datetime.fromisoformat(stamp).tzinfo is not None
    assert filename == export_name(session)


def test_the_ledger_stays_out_of_the_session_record(
    munin_home: Path,
    tmp_path: Path,
    two_track: Callable[..., tuple[Path, Path]],
    ample_disk: None,
) -> None:
    """Contracts 7.2: a re-encode is not a capture event. The ledger moved the
    bookkeeping into the upload folder precisely so session.json did not have to
    grow a field.
    """
    destination = tmp_path / "uploads"
    config = _enabled(munin_home, destination)
    spool = Spool(config)
    session = _captured_session(spool, two_track)
    before = json.loads((Path(session.directory) / "session.json").read_text("utf-8"))

    Worker(config).drain()

    after = json.loads((Path(session.directory) / "session.json").read_text("utf-8"))
    assert set(after) == set(before)
    assert "export" not in json.dumps(after).lower()
    # The rows the sweep did add are ordinary state transitions, not export rows.
    added = after["history"][len(before["history"]) :]
    assert {row["to"] for row in added} <= {"pending", "transcribing"}


# -- the worker's own log ---------------------------------------------------


def test_the_worker_logs_where_the_recorder_does(munin_home: Path) -> None:
    """One file, one story: the capture line and the export line for the same
    meeting have to be readable together. Before this the worker configured no
    logging at all, so every info line it wrote was discarded.
    """
    from munin.worker import configure_logging

    config = _config(munin_home)
    configure_logging(config)
    try:
        logging.getLogger("munin.worker").info("exported session=test")
        for handler in logging.getLogger("munin").handlers:
            handler.flush()
        assert "exported session=test" in config.log_path.read_text(encoding="utf-8")
    finally:
        logging.getLogger("munin").handlers.clear()
