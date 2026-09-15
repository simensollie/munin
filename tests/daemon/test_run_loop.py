"""The daemon as a process: socket, loop, shutdown, and the polling detector.

``run()`` is the only place the socket, the timers and the signal handling meet,
so it gets exercised for real -- on a thread, over a real Unix socket in
``tmp_path``, with the clock and the capturer still faked.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any

import pytest

from munin import ipc
from munin.detect.base import CallEvidence, DetectedCall, Identity, WindowInfo

from .conftest import Harness


def _run_in_thread(harness: Harness) -> threading.Thread:
    thread = threading.Thread(target=harness.daemon.run, daemon=True)
    thread.start()
    for _ in range(200):
        if harness.daemon.socket_path.exists():
            return thread
        threading.Event().wait(0.01)
    raise AssertionError("the daemon never bound its socket")


def test_the_daemon_serves_a_whole_recording_over_the_socket(harness: Harness) -> None:
    thread = _run_in_thread(harness)
    path = harness.daemon.socket_path
    try:
        assert ipc.call("ping", path, timeout=5)["version"]
        assert ipc.call("status", path, timeout=5)["state"] == "idle"

        started = ipc.call("start", path, timeout=5, title="Weekly quality sync")
        assert started["state"] == "recording"
        harness.clock.advance(12)

        status = ipc.call("status", path, timeout=5)
        assert status["state"] == "recording"
        assert status["session_id"] == started["session_id"]

        stopped = ipc.call("stop", path, timeout=5)
        assert stopped["state"] == "captured"
        assert stopped["duration_seconds"] == pytest.approx(12.0)

        listing = ipc.call("list", path, timeout=5, limit=5)
        assert [row["id"] for row in listing["sessions"]] == [started["session_id"]]
    finally:
        harness.daemon._stopping = True
        thread.join(5)

    assert not path.exists(), "a clean exit takes the socket with it"
    assert json.loads(harness.state_path.read_text())["state"] == "captured"


def test_a_second_daemon_on_the_same_socket_exits_three(harness: Harness, tmp_path) -> None:
    from dataclasses import replace

    from munin.daemon import Daemon

    thread = _run_in_thread(harness)
    try:
        second = Daemon(
            replace(harness.daemon.config),
            spool=harness.spool,
            capturer_factory=harness.daemon.capturer_factory,
            notifier=harness.daemon.notifier,
            clock=harness.clock,
            idle_runner=harness.daemon.idle_runner,
            socket_path_override=harness.daemon.socket_path,
            state_path_override=harness.state_path,
        )
        assert second.run() == 3
    finally:
        harness.daemon._stopping = True
        thread.join(5)


def test_shutdown_mid_recording_over_the_socket_keeps_the_segment(harness: Harness) -> None:
    thread = _run_in_thread(harness)
    path = harness.daemon.socket_path
    started = ipc.call("start", path, timeout=5, title="Weekly quality sync")
    harness.clock.advance(31)

    harness.daemon._stopping = True  # what SIGTERM sets
    thread.join(5)

    on_disk = harness.session_json(started["session_id"])
    assert on_disk["state"] == "captured"
    assert on_disk["segments"][0]["duration_seconds"] == pytest.approx(31.0)


class ScriptedDetector:
    """A detector that returns whatever the test put in ``calls``."""

    def __init__(self, script: list[list[DetectedCall]]) -> None:
        self.script = script
        self.scans = 0

    def scan(self, *, now: datetime | None = None) -> list[DetectedCall]:
        index = min(self.scans, len(self.script) - 1)
        self.scans += 1
        return self.script[index]

    def describe(self) -> dict[str, str]:
        return {"platform": "fake"}


def _call(pid: int) -> DetectedCall:
    return DetectedCall(
        evidence=CallEvidence(
            pid=pid, has_playback=True, has_capture=True,
            playback_handle="57", client_name="Beacon 365",
        ),
        identity=Identity("beacon-tab", "Beacon 365", "window_title"),
        window=WindowInfo(pid, "google-chrome", "Weekly quality sync | Beacon 365"),
        observed_at=datetime(2026, 9, 14, 13, 25).astimezone(),
    )


def test_the_polling_detector_raises_the_same_events(harness: Harness) -> None:
    from dataclasses import replace

    daemon, clock = harness.daemon, harness.clock
    daemon.config = replace(
        daemon.config, detection=replace(daemon.config.detection, source="daemon")
    )
    detector = ScriptedDetector([[_call(4242)], [_call(4242)], []])
    daemon._detector = detector

    daemon.tick()
    assert daemon.state.state == "detected"
    assert harness.titles() == ["Meeting detected"]

    daemon.handle_start({"from_detection": True})
    clock.advance(5)
    daemon.tick()  # still on the call
    assert daemon.state.state == "recording"

    clock.advance(5)
    daemon.tick()  # the streams are gone
    assert daemon.state.state == "ending"


def test_polling_honours_poll_seconds(harness: Harness) -> None:
    from dataclasses import replace

    daemon, clock = harness.daemon, harness.clock
    daemon.config = replace(
        daemon.config,
        detection=replace(daemon.config.detection, source="daemon", poll_seconds=3),
    )
    detector = ScriptedDetector([[]])
    daemon._detector = detector

    daemon.tick()
    daemon.tick()
    assert detector.scans == 1, "two ticks a second apart are one poll"
    clock.advance(4)
    daemon.tick()
    assert detector.scans == 2


def test_a_detector_that_throws_does_not_stop_the_daemon(harness: Harness) -> None:
    from dataclasses import replace

    class Broken:
        def scan(self, *, now: Any = None) -> list[DetectedCall]:
            raise RuntimeError("pw-dump is not installed")

    daemon = harness.daemon
    daemon.config = replace(
        daemon.config, detection=replace(daemon.config.detection, source="daemon")
    )
    daemon._detector = Broken()
    daemon.tick()
    assert daemon.state.state == "idle"


def test_the_heartbeat_refreshes_the_state_file(harness: Harness) -> None:
    from munin.daemon import HEARTBEAT_SECONDS

    daemon, clock = harness.daemon, harness.clock
    daemon.tick()
    first = json.loads(harness.state_path.read_text())["updated_at"]
    clock.advance(HEARTBEAT_SECONDS + 1)
    daemon.tick()
    second = json.loads(harness.state_path.read_text())
    assert second["daemon_pid"] > 0
    assert second["updated_at"] >= first


def test_the_heartbeat_follows_the_session_into_the_worker(harness: Harness) -> None:
    """The bar must keep up once the worker takes over (contracts 7.2)."""
    from munin.daemon import HEARTBEAT_SECONDS

    daemon, clock = harness.daemon, harness.clock
    started = daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(20)
    daemon.handle_stop({})
    assert json.loads(harness.state_path.read_text())["state"] == "captured"

    session = harness.spool.load(started["session_id"])
    session.transition("pending", by="munin-work")
    session.transition("transcribing", by="munin-work")

    clock.advance(HEARTBEAT_SECONDS + 1)
    daemon.tick()
    assert json.loads(harness.state_path.read_text())["state"] == "transcribing"

    session.transition("done", by="munin-work")
    clock.advance(HEARTBEAT_SECONDS + 1)
    daemon.tick()
    assert json.loads(harness.state_path.read_text())["state"] == "done"


def test_pending_is_shown_as_captured(harness: Harness) -> None:
    """``pending`` is not one of the bar's states; captured-and-safe is."""
    from munin.daemon import HEARTBEAT_SECONDS

    daemon, clock = harness.daemon, harness.clock
    started = daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(20)
    daemon.handle_stop({})
    harness.spool.load(started["session_id"]).transition("pending", by="munin-work")

    clock.advance(HEARTBEAT_SECONDS + 1)
    daemon.tick()
    assert json.loads(harness.state_path.read_text())["state"] == "captured"


# ---------------------------------------------------------------------------
# Crash recovery at start (spec 11). A SIGKILL, an OOM kill or a power cut skips
# shutdown() entirely, so the next munin-rec inherits a session still marked
# `recording` -- with real audio in it.
# ---------------------------------------------------------------------------


def _strand_a_recording(harness: Harness, *, with_audio: bool) -> Any:
    """Leave a session in `recording` the way a killed daemon would."""
    session = harness.spool.create(
        title="Weekly quality sync", source="adhoc", now=harness.clock()
    )
    segment = harness.spool.add_segment(session, 1)
    if with_audio:
        (session.directory / segment.mic).write_bytes(b"OpusHead-fake")
    return session


def test_a_killed_daemon_recovers_its_session_at_start(harness: Harness) -> None:
    """The audio reaches the worker, and the bar stops claiming a live recording."""
    session = _strand_a_recording(harness, with_audio=True)
    assert session.state == "recording"

    harness.daemon._stopping = True  # serve_forever returns on the first check
    assert harness.daemon.run() == 0

    assert harness.spool.load(session.id).state == "captured"
    assert (harness.home / "inbox" / session.id).is_symlink(), (
        "a recovered session that never reaches the inbox is invisible to munin-work"
    )
    state = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert state["state"] == "captured"
    assert state["session_id"] == session.id


def test_a_killed_daemon_with_no_audio_recovers_the_session_as_failed(
    harness: Harness,
) -> None:
    session = _strand_a_recording(harness, with_audio=False)

    harness.daemon._stopping = True
    assert harness.daemon.run() == 0

    assert harness.spool.load(session.id).state == "failed"
    assert json.loads(harness.state_path.read_text(encoding="utf-8"))["state"] == "failed"


def test_recovery_that_throws_does_not_stop_the_daemon_binding(harness: Harness) -> None:
    def broken() -> list[Any]:
        raise RuntimeError("session.json is unreadable")

    harness.spool.recover_for_daemon = broken  # type: ignore[method-assign]
    _strand_a_recording(harness, with_audio=True)

    harness.daemon._stopping = True
    assert harness.daemon.run() == 0, "one broken session must not cost us the socket"


def test_state_json_never_reports_a_recording_this_daemon_is_not_making(
    harness: Harness,
) -> None:
    """D10: the bar is the only reliable stop control, so it must never lie.

    Recovery has been sabotaged here, so the stranded session survives into
    ``_refresh_idle_state`` -- which must still refuse to publish `recording`.
    """
    harness.spool.recover_for_daemon = lambda: []  # type: ignore[method-assign]
    _strand_a_recording(harness, with_audio=True)

    harness.daemon._stopping = True
    harness.daemon.run()

    state = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert state["state"] == "captured"
    assert harness.daemon.session is None


# ---------------------------------------------------------------------------
# Detection polling must not end a recording it never identified.
# ---------------------------------------------------------------------------


def test_an_adhoc_recording_survives_the_daemon_poller(harness: Harness) -> None:
    """`munin start` in a room with no call must not auto-stop two minutes in."""
    from dataclasses import replace

    daemon, clock = harness.daemon, harness.clock
    daemon.config = replace(
        daemon.config, detection=replace(daemon.config.detection, source="daemon")
    )
    daemon._detector = ScriptedDetector([[]])

    started = daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(5)
    daemon.tick()
    assert daemon.state.state == "recording", "no detected call means nothing ended"

    clock.advance(daemon.config.detection.grace_seconds + 60)
    daemon.tick()
    assert daemon.state.state == "recording"
    assert harness.session_json(started["session_id"])["state"] == "recording"


# ---------------------------------------------------------------------------
# The unit file. The daemon's clean-shutdown ordering only survives if systemd
# signals munin-rec alone rather than the whole cgroup.
# ---------------------------------------------------------------------------


def test_the_unit_signals_only_the_daemon_on_stop() -> None:
    from pathlib import Path

    unit = Path(__file__).resolve().parents[2] / "systemd" / "munin.service"
    body = unit.read_text(encoding="utf-8")
    assert "KillMode=mixed" in body, (
        "control-group kill would SIGTERM pw-record and ffmpeg alongside the "
        "daemon, and the Ogg trailer is written by the ordering munin-rec runs"
    )
    assert "TimeoutStopSec=" in body


# ---------------------------------------------------------------------------
# An ad-hoc start is usually the keybind pressed during a call nobody told the
# daemon about. It must look once, bind the call it finds, and never leave the
# meeting track silent by default.
# ---------------------------------------------------------------------------


def _unidentified_call(pid: int, handle: str) -> DetectedCall:
    return DetectedCall(
        evidence=CallEvidence(
            pid=pid, has_playback=True, has_capture=True,
            playback_handle=handle, client_name="Some WebRTC app",
        ),
        identity=None,
        window=None,
        observed_at=datetime(2026, 9, 14, 13, 25).astimezone(),
    )


def test_an_adhoc_start_binds_the_live_call_it_finds(harness: Harness) -> None:
    daemon = harness.daemon
    daemon._detector = ScriptedDetector([[_call(4242)]])

    started = daemon.handle_start({"title": "Weekly quality sync"})

    capturer = daemon.capturer
    assert capturer is not None and capturer.app is not None
    assert capturer.app.handle == "57", "the app track is the call's own stream"
    assert capturer.app.app_id == "beacon-tab"
    session = harness.session_json(started["session_id"])
    assert session["source"] == "adhoc", "the user started it, not a detection"
    assert session["app"]["app_id"] == "beacon-tab"
    assert session["app"]["pid"] == 4242
    assert harness.state_json()["detected_app"]["pid"] == 4242
    assert "No meeting found, recording desktop audio" not in harness.titles()


def test_an_adhoc_start_adopts_a_single_unidentified_call(harness: Harness) -> None:
    daemon = harness.daemon
    daemon._detector = ScriptedDetector([[_unidentified_call(77, "901")]])

    started = daemon.handle_start({"title": "Ad hoc"})

    assert daemon.capturer.app.handle == "901"
    assert harness.session_json(started["session_id"])["app"]["app_id"] == "unknown"


def test_two_unidentified_calls_are_ambiguous_so_the_output_mix_is_taken(
    harness: Harness,
) -> None:
    from munin.capture.base import SYSTEM_OUTPUT_HANDLE

    daemon = harness.daemon
    daemon._detector = ScriptedDetector(
        [[_unidentified_call(1, "10"), _unidentified_call(2, "20")]]
    )
    daemon.handle_start({"title": "Ad hoc"})
    assert daemon.capturer.app.handle == SYSTEM_OUTPUT_HANDLE


def test_an_adhoc_start_with_no_call_records_the_output_mix_and_says_so(
    harness: Harness,
) -> None:
    from munin.capture.base import SYSTEM_OUTPUT_HANDLE

    daemon = harness.daemon
    started = daemon.handle_start({"title": "Ad hoc"})

    capturer = daemon.capturer
    assert capturer.app is not None
    assert capturer.app.handle == SYSTEM_OUTPUT_HANDLE
    assert "No meeting found, recording desktop audio" in harness.titles()
    assert harness.state_json()["last_error"] is None, "asked for, not a failure"
    assert harness.session_json(started["session_id"])["app"] is None


def test_adhoc_app_source_silent_keeps_the_old_behaviour(harness: Harness) -> None:
    from dataclasses import replace

    daemon = harness.daemon
    daemon.config = replace(
        daemon.config, capture=replace(daemon.config.capture, adhoc_app_source="silent")
    )
    daemon.handle_start({"title": "Ad hoc"})
    assert daemon.capturer.app is None
    assert "No meeting found, recording desktop audio" not in harness.titles()


def test_a_broken_detector_never_blocks_an_adhoc_start(harness: Harness) -> None:
    from munin.capture.base import SYSTEM_OUTPUT_HANDLE

    class BrokenDetector:
        def scan(self, *, now=None):
            raise RuntimeError("pw-dump exploded")

        def describe(self):
            return {"platform": "fake"}

    daemon = harness.daemon
    daemon._detector = BrokenDetector()
    daemon.handle_start({"title": "Ad hoc"})
    assert daemon.state.state == "recording"
    assert daemon.capturer.app.handle == SYSTEM_OUTPUT_HANDLE


def test_a_call_adopted_at_start_can_be_ended_by_the_plugin(harness: Harness) -> None:
    """The adopted call is a real detection: its call-ended starts the grace period."""
    daemon, clock = harness.daemon, harness.clock
    daemon._detector = ScriptedDetector([[_call(4242)]])
    daemon.handle_start({"title": "Weekly quality sync"})

    assert daemon.on_call_ended(pid=4242) is True
    assert daemon.state.state == "ending"
    clock.advance(daemon.config.detection.grace_seconds + 1)
    daemon.tick()
    assert daemon.state.state != "recording"
