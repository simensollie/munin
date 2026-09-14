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
