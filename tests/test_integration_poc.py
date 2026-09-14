"""The PoC end to end, in process, with no fakes between the modules.

Every other test directory mocks its neighbours: the daemon drives a ``FakeSpool``,
the worker drives a test double, the spool is exercised on its own. That is how
six workstreams built in parallel, and it leaves exactly one thing unproven --
that the real modules agree with each other.

This file closes that gap. It drives the *real* :class:`munin.daemon.Daemon`
against the *real* :class:`munin.spool.Spool`, stops it, and then hands the spool
to the *real* :class:`munin.worker.Worker`, asserting the session lands in
``pending`` with the contract's reason. The only substitution is the capturer:
PipeWire is not available in CI and a test must not open the user's microphone,
so a capturer that writes byte-identical placeholder tracks stands in for it. It
implements the frozen :class:`munin.capture.base.Capturer` interface, so the
daemon cannot tell the difference.

Fixtures are synthetic throughout: no real customer audio, no real names
(spec 13, and the public-copy rule in CLAUDE.md).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from munin.capture.base import CaptureResult, CaptureTarget, Capturer, segment_filenames
from munin.config import Config, DetectionConfig, IdleConfig, NotificationConfig
from munin.daemon import Daemon
from munin.spool import Spool
from munin.worker import Worker

#: The exact string ``backends/none.py`` raises; contracts section 10 fixes it.
NO_BACKEND = "no transcription backend configured"

#: Placeholder Opus-ish bytes. Never played, only checksummed and counted, so a
#: real encode would buy nothing and would need PipeWire.
TRACK_BYTES = b"OggS" + b"\0" * 512


class StubCapturer(Capturer):
    """Writes a two-track pair without touching audio hardware.

    Deliberately implements the frozen interface rather than monkeypatching it:
    if a future change to ``capture/base.py`` breaks the contract the daemon
    codes against, this class stops matching and the test fails.
    """

    method = "test-stub"

    def __init__(
        self,
        mic: CaptureTarget,
        app: CaptureTarget | None,
        *,
        bitrate_kbps: int = 24,
        channels: int = 1,
        sample_rate: int = 48000,
    ) -> None:
        super().__init__(
            mic,
            app,
            bitrate_kbps=bitrate_kbps,
            channels=channels,
            sample_rate=sample_rate,
        )
        self._session: Any | None = None
        self._index = 0
        self._started: datetime | None = None
        self._running = False
        self.clock: Any = None

    def start(self, session: Any, segment_index: int) -> None:
        if self._running:
            raise RuntimeError("already running")
        self._session = session
        self._index = segment_index
        self._started = self.clock()
        self._running = True

    def stop(self) -> CaptureResult:
        if not self._running:
            raise RuntimeError("not running")
        self._running = False
        assert self._session is not None and self._started is not None
        mic_name, app_name = segment_filenames(self._index)
        directory = Path(self._session.directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name in (mic_name, app_name):
            (directory / name).write_bytes(TRACK_BYTES)
        stopped = self.clock()
        return CaptureResult(
            segment_index=self._index,
            mic_path=directory / mic_name,
            app_path=directory / app_name,
            started_at=self._started,
            stopped_at=stopped,
            duration_seconds=(stopped - self._started).total_seconds(),
        )

    @property
    def is_running(self) -> bool:
        return self._running

    def describe(self) -> dict[str, str]:
        return {"method": self.method, "app_source": "stub"}


class Clock:
    """A clock the test advances by hand, so nothing sleeps."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 14, 13, 25, 7).astimezone()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real Spool and a real Daemon over a throwaway root."""
    home = tmp_path / "munin"
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("MUNIN_HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    config = Config(
        home=home,
        detection=DetectionConfig(source="plugin"),
        # Nothing in this test may reach the desktop.
        idle=IdleConfig(inhibit=False),
        notifications=NotificationConfig(enabled=False),
    )
    spool = Spool(config)
    spool.ensure_dirs()
    clock = Clock()

    def factory(mic: CaptureTarget, app: CaptureTarget | None) -> Capturer:
        capturer = StubCapturer(mic, app)
        capturer.clock = clock
        return capturer

    daemon = Daemon(
        config,
        spool=spool,
        capturer_factory=factory,
        notifier=lambda note: True,
        clock=clock,
        socket_path_override=run / "munin" / "rec.sock",
        state_path_override=run / "munin" / "state.json",
    )
    return daemon, spool, clock, config, home


def _session_json(session) -> dict:
    return json.loads(Path(session.json_path).read_text(encoding="utf-8"))


def test_capture_then_worker_leaves_the_session_pending(wiring) -> None:
    """The whole PoC: record, stop, drain, land in pending with the reason.

    This is the acceptance criterion in contracts section 0 -- "leave the session
    in the spool as pending with a clear reason" -- asserted against the real
    modules rather than against any workstream's idea of its neighbour.
    """
    daemon, spool, clock, config, home = wiring

    # --- the daemon's half ------------------------------------------------
    started = daemon.handle_start({"title": "Weekly quality sync"})
    assert started["state"] == "recording"
    session_id = started["session_id"]

    clock.advance(90)
    stopped = daemon.handle_stop({})
    assert stopped["state"] == "captured"
    assert stopped["duration_seconds"] == pytest.approx(90.0)

    # The spool, read fresh off disk, must agree.
    session = spool.load(session_id)
    assert session.state == "captured"
    assert [seg.index for seg in session.segments] == [1]
    assert (session.segments[0].mic, session.segments[0].app) == segment_filenames(1)
    for name in segment_filenames(1):
        assert (session.directory / name).exists()
        assert session.checksums[name].startswith("sha256:")
    # The daemon owns everything up to captured, and nothing past it.
    assert [row["to"] for row in session.history] == ["recording", "captured"]
    assert {row["by"] for row in session.history} == {"munin-rec"}
    # The worker finds its queue through the inbox symlink.
    assert session_id in spool.inbox_ids()

    # --- the worker's half ------------------------------------------------
    worker = Worker(config)
    assert worker.drain() == 1

    final = spool.load(session_id)
    assert final.state == "pending"
    assert final.pending_reason == NO_BACKEND
    # Contracts section 10: the session stays queued, so a backend added later
    # picks it up without the user doing anything.
    assert session_id in spool.inbox_ids()
    assert final.error is None

    # The audit trail is append-only and names both writers (spec 12). The
    # worker claims the session (pending), attempts the backend (transcribing)
    # and records the refusal (pending) -- so the record shows a transcription
    # was actually attempted, not merely that one never happened.
    assert [row["to"] for row in final.history] == [
        "recording",
        "captured",
        "pending",
        "transcribing",
        "pending",
    ]
    assert [row["by"] for row in final.history] == [
        "munin-rec",
        "munin-rec",
        "munin-work",
        "munin-work",
        "munin-work",
    ]

    # session.json is the record; it must be conformant on disk, not just in
    # memory (contracts section 3).
    on_disk = _session_json(final)
    assert on_disk["schema_version"] == 1
    assert on_disk["state"] == "pending"
    assert on_disk["pending_reason"] == NO_BACKEND
    assert on_disk["title"] == "Weekly quality sync"
    assert on_disk["capture_method"] == "test-stub"
    assert on_disk["calendar_event_id"] is None
    # What the app track actually holds is on the record: a capturer that fell
    # back to the desktop mix recorded more than this meeting, and a reader of
    # session.json must be able to tell (spec 12).
    assert on_disk["segments"][0]["app_source"] == "stub"
    # No transcript is produced in the PoC.
    assert not (final.directory / "transcript.txt").exists()


def test_a_resumed_session_reaches_the_worker_as_one_two_segment_session(wiring) -> None:
    """D15: a resume inside the window is the same session, not a second one."""
    daemon, spool, clock, config, home = wiring

    started = daemon.handle_start({"title": "Ad-hoc call with Ola Nordmann"})
    session_id = started["session_id"]
    clock.advance(60)
    daemon.handle_stop({})

    # Back inside resume_window_seconds (600 by default).
    clock.advance(120)
    resumed = daemon.handle_start({"resume": True})
    assert resumed["resumed"] is True
    assert resumed["session_id"] == session_id
    assert resumed["segment"] == 2

    clock.advance(30)
    daemon.handle_stop({})

    session = spool.load(session_id)
    assert session.state == "captured"
    assert [seg.index for seg in session.segments] == [1, 2]
    assert (session.segments[1].mic, session.segments[1].app) == segment_filenames(2)
    # duration_seconds is captured-audio time, so it excludes the 120 s gap.
    assert session.duration_seconds == pytest.approx(90.0)
    assert len(spool.inbox_ids()) == 1

    worker = Worker(config)
    worker.drain()
    assert spool.load(session_id).state == "pending"


def test_the_worker_is_idempotent_over_a_pending_session(wiring) -> None:
    """Draining twice must not double-append history or change the state.

    ``munin-work`` runs on a timer in the real system, so it meets the same
    pending session over and over.
    """
    daemon, spool, clock, config, home = wiring
    session_id = daemon.handle_start({"title": "Board review"})["session_id"]
    clock.advance(45)
    daemon.handle_stop({})

    worker = Worker(config)
    worker.drain()
    first = spool.load(session_id)
    worker.drain()
    second = spool.load(session_id)

    assert second.state == "pending"
    assert second.pending_reason == NO_BACKEND
    assert len(second.history) == len(first.history)
