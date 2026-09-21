"""The session store: creation, the state machine, segments, the inbox, recovery.

These are the rules two processes rely on to stay out of each other's way, so
every one of them is asserted rather than assumed: the legal transitions, the
illegal ones, who is allowed to write which, and what happens to a session whose
writer died halfway through.

Nothing here needs PipeWire, ffmpeg or a real microphone.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from munin.capture.base import segment_filenames
from munin.spool import (
    SCHEMA_VERSION,
    STATES,
    TRANSITIONS,
    NoSpaceError,
    SchemaError,
    Session,
    Spool,
    StateError,
    from_iso,
    read_session,
)

from .conftest import WHEN, with_resume_window

REC = "munin-rec"
WORK = "munin-work"
CLI = "munin"


def advance(session: Session, *states: str) -> Session:
    """Walk a session along a legal path, using each step's rightful writer."""
    for state in states:
        writer = TRANSITIONS[(session.state, state)]
        extra = {}
        if state == "captured":
            extra = {"stopped_at": session.stopped_at or WHEN}
        session.transition(state, by=writer, **extra)
    return session


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def test_create_writes_a_conformant_session(spool: Spool, munin_home: Path) -> None:
    session = spool.create(title="Weekly quality sync", source="adhoc", now=WHEN)

    assert session.id == "2026-09-14T1325-weekly-quality-sync"
    assert session.directory == (
        munin_home / "recordings" / "2026" / "09" / session.id
    )
    assert session.state == "recording"
    assert session.slug == "weekly-quality-sync"
    assert session.source == "adhoc"
    assert session.platform == "linux"
    assert session.host
    assert session.munin_version

    data = json.loads(session.json_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["calendar_event_id"] is None
    assert data["segments"] == []
    assert data["checksums"] == {}
    assert data["pending_reason"] is None
    assert data["error"] is None
    assert data["transcript"] == {"txt": None, "json": None}
    assert data["history"] == [
        {"at": data["created_at"], "from": None, "to": "recording", "by": REC}
    ]


def test_timestamps_carry_a_local_offset(spool: Spool) -> None:
    """Never naive, never Z: the directory name is local time and must agree."""
    session = spool.create(title="Weekly quality sync", source="adhoc", now=WHEN)
    created = json.loads(session.json_path.read_text(encoding="utf-8"))["created_at"]
    assert created == "2026-09-14T13:25:07+02:00"
    assert not created.endswith("Z")
    assert from_iso(created).tzinfo is not None


@pytest.mark.parametrize(
    ("title", "app", "expected_title", "expected_slug"),
    [
        (None, None, "Meeting 13:25", "meeting-13-25"),
        (
            None,
            {"app_id": "teams-tab", "label": "Microsoft Teams"},
            "Microsoft Teams 13:25",
            "microsoft-teams-13-25",
        ),
        ("   ", None, "Meeting 13:25", "meeting-13-25"),
    ],
)
def test_the_title_falls_back_to_the_app_then_the_clock(
    spool: Spool, title, app, expected_title: str, expected_slug: str
) -> None:
    session = spool.create(title=title, source="detected", app=app, now=WHEN)
    assert session.title == expected_title
    assert session.slug == expected_slug
    assert session.title is not None


def test_a_colliding_directory_gets_a_numeric_suffix(spool: Spool) -> None:
    first = spool.create(title="Weekly quality sync", source="adhoc", now=WHEN)
    second = spool.create(title="Weekly quality sync", source="adhoc", now=WHEN)
    third = spool.create(title="Weekly quality sync", source="adhoc", now=WHEN)

    assert first.id.endswith("weekly-quality-sync")
    assert second.id.endswith("weekly-quality-sync-2")
    assert third.id.endswith("weekly-quality-sync-3")
    assert second.slug == "weekly-quality-sync-2"
    assert len({first.directory, second.directory, third.directory}) == 3


def test_an_unknown_source_is_refused(spool: Spool) -> None:
    with pytest.raises(ValueError, match="adhoc or detected"):
        spool.create(title="x", source="telepathy", now=WHEN)


def test_latest_is_a_relative_symlink_to_the_newest_session(
    spool: Spool, munin_home: Path
) -> None:
    first = spool.create(title="First", source="adhoc", now=WHEN)
    link = munin_home / "latest"
    assert link.is_symlink()
    assert not os.path.isabs(os.readlink(link))
    assert link.resolve() == first.directory.resolve()

    later = spool.create(title="Second", source="adhoc", now=WHEN + timedelta(hours=1))
    assert link.resolve() == later.directory.resolve()


# -- the disk guard ---------------------------------------------------------


def test_a_full_disk_refuses_the_recording(cfg, tight_disk: None) -> None:
    """Spec 11: refuse to start rather than truncate a meeting halfway through."""
    spool = Spool(cfg)
    with pytest.raises(NoSpaceError) as excinfo:
        spool.create(title="Weekly quality sync", source="adhoc", now=WHEN)
    assert "2048 MB required" in str(excinfo.value)
    # A NoSpaceError is a StateError, so a caller that only knows the base class
    # still refuses correctly.
    assert isinstance(excinfo.value, StateError)
    assert list(spool.iter_sessions()) == []


def test_free_mb_reads_the_recordings_filesystem(spool: Spool) -> None:
    assert spool.free_mb() == 400 * 1024
    assert spool.has_space() is True


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------


def test_every_declared_transition_is_walkable(spool: Spool) -> None:
    """Each legal edge, taken by its rightful writer, on a real session."""
    for (source, target), writer in TRANSITIONS.items():
        if source is None:
            continue
        session = spool.create(title=f"Edge {source} to {target}", source="adhoc")
        path = {
            "recording": (),
            "ending": ("ending",),
            "captured": ("captured",),
            "pending": ("captured", "pending"),
            "transcribing": ("captured", "pending", "transcribing"),
        }[source]
        advance(session, *path)
        assert session.state == source
        session.transition(target, by=writer)
        assert session.state == target
        assert read_session(session.directory).state == target


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("recording", "recording"),
        ("recording", "pending"),
        ("recording", "done"),
        ("captured", "ending"),
        ("captured", "done"),
        ("pending", "captured"),
        ("done", "recording"),
        ("done", "pending"),
        ("failed", "recording"),
        ("transcribing", "captured"),
    ],
)
def test_an_illegal_transition_raises(spool: Spool, source: str, target: str) -> None:
    session = spool.create(title="Illegal", source="adhoc")
    path = {
        "recording": (),
        "captured": ("captured",),
        "pending": ("captured", "pending"),
        "transcribing": ("captured", "pending", "transcribing"),
        "done": ("captured", "pending", "transcribing", "done"),
        "failed": ("failed",),
    }[source]
    advance(session, *path)
    with pytest.raises(StateError, match="not a legal transition"):
        session.transition(target, by=TRANSITIONS.get((source, target), REC))
    assert read_session(session.directory).state == source


def test_a_state_that_does_not_exist_is_refused(spool: Spool) -> None:
    session = spool.create(title="Nonsense", source="adhoc")
    with pytest.raises(StateError, match="not a session state"):
        session.transition("uploading", by=REC)


def test_the_daemon_may_not_write_worker_states(spool: Spool) -> None:
    """Spec 11: after captured, only the worker writes state."""
    session = spool.create(title="Ownership", source="adhoc")
    advance(session, "captured")
    with pytest.raises(StateError, match="written by munin-work"):
        session.transition("pending", by=REC)
    assert read_session(session.directory).state == "captured"


def test_the_worker_may_not_write_daemon_states(spool: Spool) -> None:
    session = spool.create(title="Ownership", source="adhoc")
    with pytest.raises(StateError, match="written by munin-rec"):
        session.transition("ending", by=WORK)


def test_ending_can_go_back_to_recording(spool: Spool) -> None:
    """The stream came back, or the user chose Keep recording."""
    session = spool.create(title="Flap", source="detected")
    session.transition("ending", by=REC)
    session.transition("recording", by=REC)
    assert session.state == "recording"
    assert [entry["to"] for entry in session.history] == [
        "recording",
        "ending",
        "recording",
    ]


def test_history_is_append_only_and_names_the_writer(spool: Spool) -> None:
    """This is the audit trail of spec 12; it is never trimmed."""
    session = spool.create(title="Audit", source="adhoc", now=WHEN)
    advance(session, "captured", "pending")
    session.transition("transcribing", by=WORK)
    session.transition("done", by=WORK)

    history = read_session(session.directory).history
    assert [(e["from"], e["to"], e["by"]) for e in history] == [
        (None, "recording", REC),
        ("recording", "captured", REC),
        ("captured", "pending", WORK),
        ("pending", "transcribing", WORK),
        ("transcribing", "done", WORK),
    ]
    assert all(from_iso(entry["at"]).tzinfo is not None for entry in history)


def test_a_transition_carries_fields_into_the_file(spool: Spool) -> None:
    session = spool.create(title="Pending", source="adhoc")
    advance(session, "captured")
    session.transition(
        "pending", by=WORK, pending_reason="no transcription backend configured"
    )
    on_disk = read_session(session.directory)
    assert on_disk.state == "pending"
    assert on_disk.pending_reason == "no transcription backend configured"


def test_a_transition_cannot_set_state_or_history_behind_the_machine(
    spool: Spool,
) -> None:
    session = spool.create(title="Sneaky", source="adhoc")
    with pytest.raises(TypeError):
        session.transition("captured", by=REC, state="done")
    with pytest.raises(TypeError):
        session.transition("captured", by=REC, history=[])
    with pytest.raises(TypeError):
        session.transition("captured", by=REC, nonexistent=1)


def test_a_session_changed_underneath_us_is_not_overwritten(spool: Spool) -> None:
    """The file is the record. A stale in-memory copy must not win."""
    session = spool.create(title="Race", source="adhoc")
    other = read_session(session.directory)
    other.transition("captured", by=REC)

    with pytest.raises(StateError, match="refusing to overwrite"):
        session.transition("ending", by=REC)
    assert read_session(session.directory).state == "captured"


def test_starting_twice_is_a_no_op_that_reports_the_running_session(
    spool: Spool,
) -> None:
    """A duplicate call-started must not open a second directory."""
    first = spool.create(title="Weekly quality sync", source="detected", now=WHEN)
    assert spool.active() is not None
    assert spool.active().id == first.id

    # The daemon asks first, so no second session is created; and taking the
    # transition anyway is refused by the machine.
    with pytest.raises(StateError, match="not a legal transition"):
        first.transition("recording", by=REC)
    assert len(list(spool.iter_sessions())) == 1

    advance(first, "captured")
    assert spool.active() is None


# --------------------------------------------------------------------------
# Segments
# --------------------------------------------------------------------------


def test_segment_filenames_come_from_the_capture_layer(spool: Spool) -> None:
    session = spool.create(title="Segments", source="adhoc", now=WHEN)
    first = spool.add_segment(session, 1, started_at=WHEN)
    second = spool.add_segment(session, 2, started_at=WHEN + timedelta(minutes=30))

    assert (first.mic, first.app) == segment_filenames(1) == ("mic.opus", "app.opus")
    assert (second.mic, second.app) == segment_filenames(2)
    assert second.mic == "mic.002.opus"

    on_disk = read_session(session.directory)
    assert [s.index for s in on_disk.segments] == [1, 2]
    assert [s.mic for s in on_disk.segments] == ["mic.opus", "mic.002.opus"]


def test_the_first_segment_sets_the_session_clock(spool: Spool) -> None:
    """The bar counts up from started_at, which is when audio began."""
    session = spool.create(title="Clock", source="adhoc", now=WHEN)
    assert session.started_at is None
    began = WHEN + timedelta(seconds=1)
    spool.add_segment(session, 1, started_at=began)
    assert read_session(session.directory).started_at == began

    # A later segment does not move it.
    spool.add_segment(session, 2, started_at=began + timedelta(minutes=5))
    assert read_session(session.directory).started_at == began


def test_segments_must_be_contiguous_and_one_based(spool: Spool) -> None:
    session = spool.create(title="Gaps", source="adhoc")
    with pytest.raises(StateError, match="next is 1"):
        spool.add_segment(session, 2)
    spool.add_segment(session, 1)
    with pytest.raises(StateError, match="next is 2"):
        spool.add_segment(session, 1)
    with pytest.raises(StateError, match="next is 2"):
        spool.add_segment(session, 3)


def test_duration_is_captured_audio_time_not_wall_clock(spool: Spool) -> None:
    """A 3-minute break between segments is a gap line, not 3 minutes of audio."""
    session = spool.create(title="Resumed", source="adhoc", now=WHEN)
    spool.add_segment(session, 1, started_at=WHEN)
    spool.complete_segment(
        session, 1, stopped_at=WHEN + timedelta(minutes=27), duration_seconds=1652.0
    )
    resumed = WHEN + timedelta(minutes=31)
    spool.add_segment(session, 2, started_at=resumed)
    spool.complete_segment(
        session, 2, stopped_at=resumed + timedelta(minutes=6), duration_seconds=361.0
    )

    on_disk = read_session(session.directory)
    assert on_disk.duration_seconds == pytest.approx(2013.0)
    assert on_disk.audio_duration_seconds == pytest.approx(2013.0)
    # Wall clock across the whole session is longer than the audio.
    wall = (on_disk.stopped_at - on_disk.created_at).total_seconds()
    assert wall > on_disk.duration_seconds


def test_completing_a_segment_that_does_not_exist_is_refused(spool: Spool) -> None:
    session = spool.create(title="Missing", source="adhoc")
    with pytest.raises(StateError, match="no segment 1"):
        spool.complete_segment(
            session, 1, stopped_at=WHEN, duration_seconds=1.0
        )


# --------------------------------------------------------------------------
# The inbox
# --------------------------------------------------------------------------


def test_the_inbox_link_is_relative_and_removable(
    spool: Spool, munin_home: Path
) -> None:
    session = spool.create(title="Queued", source="adhoc", now=WHEN)
    link = spool.link_inbox(session)

    assert link == munin_home / "inbox" / session.id
    assert link.is_symlink()
    assert os.readlink(link) == os.path.join(
        "..", "recordings", "2026", "09", session.id
    )
    assert link.resolve() == session.directory.resolve()
    assert spool.queue_depth() == 1

    spool.link_inbox(session)  # idempotent
    assert spool.queue_depth() == 1

    spool.unlink_inbox(session)
    assert spool.queue_depth() == 0
    spool.unlink_inbox(session)  # tolerates being gone


def test_a_broken_inbox_link_still_counts(spool: Spool, munin_home: Path) -> None:
    """The symlink is what makes an unfinished session visible in the bar."""
    (munin_home / "inbox").mkdir(exist_ok=True)
    (munin_home / "inbox" / "2026-09-14T1325-gone").symlink_to("../recordings/nowhere")
    assert spool.queue_depth() == 1


def test_the_worker_falls_back_to_a_scan_when_the_inbox_is_missing(
    spool: Spool, munin_home: Path
) -> None:
    """The index is not the record: losing it costs speed and nothing else."""
    captured = spool.create(title="Captured", source="adhoc", now=WHEN)
    advance(captured, "captured")
    spool.link_inbox(captured)
    done = spool.create(title="Done", source="adhoc", now=WHEN + timedelta(hours=1))
    advance(done, "captured", "pending", "transcribing", "done")

    import shutil as _shutil

    _shutil.rmtree(munin_home / "inbox")
    pending = spool.pending_sessions()
    assert [s.id for s in pending] == [captured.id]


# --------------------------------------------------------------------------
# Resume (D15)
# --------------------------------------------------------------------------


def stopped_session(spool: Spool, *, state: str, stopped_at: datetime) -> Session:
    session = spool.create(title="Weekly quality sync", source="adhoc", now=WHEN)
    spool.add_segment(session, 1, started_at=WHEN)
    spool.complete_segment(
        session, 1, stopped_at=stopped_at, duration_seconds=600.0
    )
    path = {"captured": ("captured",), "pending": ("captured", "pending")}.get(state)
    if path is None:
        path = ("captured", "pending", "transcribing")
        if state == "done":
            path = path + ("done",)
    advance(session, *path)
    return session


@pytest.mark.parametrize("state", ["captured", "pending"])
def test_resume_inside_the_window(spool: Spool, state: str) -> None:
    stopped = WHEN + timedelta(minutes=20)
    session = stopped_session(spool, state=state, stopped_at=stopped)
    found = spool.resumable(now=stopped + timedelta(seconds=599))
    assert found is not None
    assert found.id == session.id


def test_resume_exactly_at_the_window_still_counts(spool: Spool) -> None:
    """600 seconds is inside; the boundary belongs to the user, not the clock."""
    stopped = WHEN + timedelta(minutes=20)
    session = stopped_session(spool, state="captured", stopped_at=stopped)
    found = spool.resumable(now=stopped + timedelta(seconds=600))
    assert found is not None
    assert found.id == session.id


def test_resume_past_the_window_starts_fresh(spool: Spool) -> None:
    stopped = WHEN + timedelta(minutes=20)
    stopped_session(spool, state="captured", stopped_at=stopped)
    assert spool.resumable(now=stopped + timedelta(seconds=601)) is None


@pytest.mark.parametrize("state", ["transcribing", "done"])
def test_a_session_the_worker_has_started_is_not_resumable(
    spool: Spool, state: str
) -> None:
    """Moving state backwards past the worker is the one thing resume may not do."""
    stopped = WHEN + timedelta(minutes=20)
    stopped_session(spool, state=state, stopped_at=stopped)
    assert spool.resumable(now=stopped + timedelta(seconds=10)) is None


def test_resume_only_considers_the_newest_session(spool: Spool) -> None:
    old_stop = WHEN + timedelta(minutes=20)
    stopped_session(spool, state="captured", stopped_at=old_stop)
    newer = spool.create(title="Newer", source="adhoc", now=WHEN + timedelta(hours=2))
    spool.add_segment(newer, 1, started_at=WHEN + timedelta(hours=2))
    spool.complete_segment(
        newer, 1, stopped_at=WHEN + timedelta(hours=3), duration_seconds=10.0
    )
    advance(newer, "captured")
    # The newer one is outside the window, and the older one does not stand in.
    assert spool.resumable(now=WHEN + timedelta(hours=5)) is None


def test_the_resume_window_is_configurable(spool: Spool) -> None:
    stopped = WHEN + timedelta(minutes=20)
    stopped_session(spool, state="captured", stopped_at=stopped)
    narrow = with_resume_window(spool, 60)
    assert narrow.resumable(now=stopped + timedelta(seconds=61)) is None
    assert narrow.resumable(now=stopped + timedelta(seconds=59)) is not None


def test_resuming_reopens_the_same_directory(spool: Spool) -> None:
    stopped = WHEN + timedelta(minutes=20)
    session = stopped_session(spool, state="captured", stopped_at=stopped)
    resumable = spool.resumable(now=stopped + timedelta(minutes=1))
    resumable.transition("recording", by="munin-rec")
    segment = spool.add_segment(resumable, 2, started_at=stopped + timedelta(minutes=1))

    assert resumable.directory == session.directory
    assert segment.mic == "mic.002.opus"
    assert read_session(session.directory).state == "recording"
    assert len(list(spool.iter_sessions())) == 1


# --------------------------------------------------------------------------
# Files on disk
# --------------------------------------------------------------------------


def test_checksums_are_sha256_prefixed(spool: Spool) -> None:
    session = spool.create(title="Checksums", source="adhoc", now=WHEN)
    spool.add_segment(session, 1, started_at=WHEN)
    (session.directory / "mic.opus").write_bytes(b"not really opus")
    (session.directory / "app.opus").write_bytes(b"nor is this")

    sums = spool.checksum_segments(session)
    assert set(sums) == {"mic.opus", "app.opus"}
    assert sums["mic.opus"].startswith("sha256:")
    assert len(sums["mic.opus"].split(":")[1]) == 64
    assert sums["mic.opus"] != sums["app.opus"]
    assert spool.checksum(session.directory / "mic.opus") == sums["mic.opus"]


def test_a_missing_track_is_simply_absent_from_the_checksums(spool: Spool) -> None:
    """One track failing must not cost us the other (contracts section 5)."""
    session = spool.create(title="Half", source="adhoc", now=WHEN)
    spool.add_segment(session, 1, started_at=WHEN)
    (session.directory / "mic.opus").write_bytes(b"mic only")
    assert set(spool.checksum_segments(session)) == {"mic.opus"}


def test_the_write_is_atomic_and_a_leftover_tmp_is_ignored(spool: Spool) -> None:
    """A crash mid-write leaves the old file plus a .tmp nobody reads."""
    session = spool.create(title="Crash", source="adhoc", now=WHEN)
    advance(session, "captured")

    tmp = session.json_path.with_name("session.json.tmp")
    tmp.write_text("{ this is half a file", encoding="utf-8")

    reloaded = read_session(session.directory)
    assert reloaded.state == "captured"
    assert tmp.exists()
    assert list(spool.iter_sessions())[0].id == session.id

    # The next real write replaces the leftover rather than tripping over it.
    session.transition("pending", by=WORK, pending_reason="queued")
    assert json.loads(session.json_path.read_text(encoding="utf-8"))["state"] == "pending"


def test_a_newer_schema_is_refused_rather_than_guessed_at(spool: Spool) -> None:
    session = spool.create(title="Future", source="adhoc", now=WHEN)
    data = json.loads(session.json_path.read_text(encoding="utf-8"))
    data["schema_version"] = SCHEMA_VERSION + 1
    session.json_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(SchemaError, match="refusing to guess"):
        read_session(session.directory)
    # A refused session must not take `munin list` down with it.
    assert list(spool.iter_sessions()) == []


def test_unknown_fields_survive_a_round_trip(spool: Spool) -> None:
    """A newer build's key must not be dropped by an older one reading the file."""
    session = spool.create(title="Forward", source="adhoc", now=WHEN)
    data = json.loads(session.json_path.read_text(encoding="utf-8"))
    data["retention_policy"] = "keep-90-days"
    session.json_path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = read_session(session.directory)
    reloaded.transition("captured", by=REC)
    assert json.loads(session.json_path.read_text(encoding="utf-8"))[
        "retention_policy"
    ] == "keep-90-days"


def test_a_corrupt_session_is_skipped_not_fatal(spool: Spool) -> None:
    good = spool.create(title="Good", source="adhoc", now=WHEN)
    bad = spool.create(title="Bad", source="adhoc", now=WHEN + timedelta(hours=1))
    bad.json_path.write_text("{ not json", encoding="utf-8")

    listed = [s.id for s in spool.iter_sessions()]
    assert listed == [good.id]
    with pytest.raises(SchemaError):
        read_session(bad.directory)


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


def test_sessions_list_newest_first_with_a_limit(spool: Spool) -> None:
    ids = [
        spool.create(title=f"Meeting {n}", source="adhoc", now=WHEN + timedelta(hours=n)).id
        for n in range(4)
    ]
    assert [s.id for s in spool.iter_sessions()] == sorted(ids, reverse=True)
    assert [s.id for s in spool.iter_sessions(limit=2)] == sorted(ids, reverse=True)[:2]
    assert spool.latest().id == max(ids)


def test_listing_crosses_month_and_year_boundaries(spool: Spool) -> None:
    december = datetime(2026, 12, 31, 23, 50, tzinfo=WHEN.tzinfo)
    january = datetime(2027, 1, 1, 0, 10, tzinfo=WHEN.tzinfo)
    old = spool.create(title="New year eve", source="adhoc", now=december)
    new = spool.create(title="New year day", source="adhoc", now=january)

    assert old.directory.parent.parent.name == "2026"
    assert new.directory.parent.parent.name == "2027"
    assert [s.id for s in spool.iter_sessions()] == [new.id, old.id]


def test_find_and_load_by_id(spool: Spool) -> None:
    session = spool.create(title="Findable", source="adhoc", now=WHEN)
    assert spool.find(session.id).id == session.id
    assert spool.load(session.id).directory == session.directory
    assert spool.find("2026-09-14T1325-nothing-here") is None
    assert spool.find("not-an-id") is None
    with pytest.raises(SchemaError):
        spool.load("2026-09-14T1325-nothing-here")


def test_an_empty_spool_has_no_latest(spool: Spool) -> None:
    assert spool.latest() is None
    assert spool.active() is None
    assert spool.queue_depth() == 0
    assert spool.pending_sessions() == []


# --------------------------------------------------------------------------
# Crash recovery (spec 11)
# --------------------------------------------------------------------------


def test_a_session_left_recording_becomes_captured_with_a_note(spool: Spool) -> None:
    """Opus is a streaming format: what reached the disk is still playable."""
    session = spool.create(title="Interrupted", source="detected", now=WHEN)
    spool.add_segment(session, 1, started_at=WHEN)
    (session.directory / "mic.opus").write_bytes(b"partial but playable")
    (session.directory / "app.opus").write_bytes(b"partial but playable too")

    recovered = spool.recover_for_daemon()
    assert [s.id for s in recovered] == [session.id]

    on_disk = read_session(session.directory)
    assert on_disk.state == "captured"
    assert on_disk.checksums["mic.opus"].startswith("sha256:")
    assert on_disk.segments[0].stopped_at is not None
    note = on_disk.history[-1]
    assert note["from"] == "recording" and note["to"] == "captured"
    assert note["by"] == REC
    assert "recovered at daemon start" in note["note"]
    # And it is queued, so the worker sees it rather than it being stranded.
    assert spool.queue_depth() == 1


def test_a_session_left_ending_is_recovered_too(spool: Spool) -> None:
    session = spool.create(title="Grace", source="detected", now=WHEN)
    spool.add_segment(session, 1, started_at=WHEN)
    (session.directory / "mic.opus").write_bytes(b"audio")
    session.transition("ending", by=REC)

    spool.recover_for_daemon()
    assert read_session(session.directory).state == "captured"


def test_a_session_with_no_audio_at_all_fails_rather_than_lying(spool: Spool) -> None:
    session = spool.create(title="Nothing", source="detected", now=WHEN)
    spool.recover_for_daemon()

    on_disk = read_session(session.directory)
    assert on_disk.state == "failed"
    assert on_disk.error["code"] == "interrupted"
    assert on_disk.error["at"]
    assert spool.queue_depth() == 0


def test_recovery_leaves_finished_sessions_alone(spool: Spool) -> None:
    done = spool.create(title="Finished", source="adhoc", now=WHEN)
    advance(done, "captured", "pending", "transcribing", "done")
    assert spool.recover_for_daemon() == []
    assert read_session(done.directory).state == "done"


def test_a_session_left_transcribing_is_requeued(spool: Spool) -> None:
    """Spec 11: the worker died mid-file; nothing trusts a partial transcript."""
    session = spool.create(title="Half transcribed", source="adhoc", now=WHEN)
    advance(session, "captured", "pending", "transcribing")

    recovered = spool.recover_for_worker()
    assert [s.id for s in recovered] == [session.id]

    on_disk = read_session(session.directory)
    assert on_disk.state == "pending"
    assert on_disk.pending_reason == "requeued after the worker was interrupted"
    assert "interrupted" in on_disk.history[-1]["note"]
    assert spool.queue_depth() == 1


def test_worker_recovery_is_idempotent(spool: Spool) -> None:
    session = spool.create(title="Twice", source="adhoc", now=WHEN)
    advance(session, "captured", "pending", "transcribing")
    spool.recover_for_worker()
    assert spool.recover_for_worker() == []
    assert read_session(session.directory).state == "pending"


# --------------------------------------------------------------------------
# The per-session lock
# --------------------------------------------------------------------------


def test_the_lock_keeps_a_second_writer_out(spool: Spool) -> None:
    """The daemon resuming and the worker queueing must not interleave."""
    fcntl = pytest.importorskip("fcntl")
    session = spool.create(title="Locked", source="adhoc", now=WHEN)

    with session.lock():
        with session.lock_path.open("a+") as rival:
            with pytest.raises(BlockingIOError):
                fcntl.flock(rival, fcntl.LOCK_EX | fcntl.LOCK_NB)

    # Released again afterwards.
    with session.lock_path.open("a+") as rival:
        fcntl.flock(rival, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(rival, fcntl.LOCK_UN)


def test_the_lock_file_is_not_mistaken_for_a_session(spool: Spool) -> None:
    session = spool.create(title="Locked", source="adhoc", now=WHEN)
    with session.lock():
        pass
    assert session.lock_path.exists()
    assert [s.id for s in spool.iter_sessions()] == [session.id]


# --------------------------------------------------------------------------
# The declared machine itself
# --------------------------------------------------------------------------


def test_every_state_is_reachable_and_every_transition_is_between_states() -> None:
    reachable = {"recording"}
    for _ in range(len(STATES)):
        for (source, target) in TRANSITIONS:
            if source is None or source in reachable:
                reachable.add(target)
    assert reachable == set(STATES)
    for (source, target), writer in TRANSITIONS.items():
        assert source is None or source in STATES
        assert target in STATES
        # D26: ``munin`` joins the two daemons as a writer. It writes only the
        # states that belong to a session nobody is capturing -- the split
        # parent, and the derived halves at their birth.
        assert writer in {REC, WORK, CLI}
        if writer == CLI:
            assert (source, target) in {
                (None, "captured"),
                ("captured", "split"),
                ("pending", "split"),
            }
