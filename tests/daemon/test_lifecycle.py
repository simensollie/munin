"""The daemon's state machine, driven with a fake clock and a fake capturer.

These are the tests the PoC is actually judged on: a meeting is detected, the
user says yes, the streams disappear, the grace period runs, the recording stops
by itself, and a resume inside the window continues the same session as a second
segment. Everything the spec calls a timer is exercised without waiting.
"""

from __future__ import annotations

import json

import pytest

from munin.daemon import RUNTIME_STATES, _rule_rows
from munin.ipc import IpcError

from .conftest import Harness


def _valid_state_file(harness: Harness) -> dict:
    payload = json.loads(harness.state_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["state"] in RUNTIME_STATES
    assert payload["updated_at"]
    return payload


def test_detect_record_grace_autostop_resume(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock

    # 1. The plugin says a call went live. D4: suggest, never auto-record.
    reply = daemon.handle_event(
        {
            "event": "call-started",
            "pid": 4242,
            "app": "Beacon 365",
            "app_id": "beacon-tab",
            "title": "Weekly quality sync | Beacon 365",
            "handle": "57",
        }
    )
    assert reply == {"accepted": True, "state": "detected"}
    assert harness.titles() == ["Meeting detected"]
    assert _valid_state_file(harness)["detected_app"]["pid"] == 4242
    assert daemon.session is None, "a detection must never start a recording"

    # 2. The user accepts the notification.
    started = daemon.handle_start({"from_detection": True})
    assert started["state"] == "recording"
    assert started["segment"] == 1
    assert started["resumed"] is False
    session_id = started["session_id"]
    on_disk = harness.session_json(session_id)
    assert on_disk["source"] == "detected"
    assert on_disk["app"]["app_id"] == "beacon-tab"
    assert on_disk["segments"][0]["mic"] == "mic.opus"
    assert harness.idle_calls == [["omarchy-toggle-idle", "stay-awake"]]
    assert _valid_state_file(harness)["state"] == "recording"

    # 3. Five minutes in, the app's streams disappear.
    clock.advance(300)
    daemon.handle_event({"event": "call-ended", "pid": 4242})
    state = _valid_state_file(harness)
    assert state["state"] == "ending"
    assert state["grace_deadline"] is not None
    assert harness.session_json(session_id)["state"] == "ending"

    # 4. Nothing is said before warn_seconds.
    clock.advance(30)
    daemon.tick()
    assert harness.titles() == ["Meeting detected"]

    # 5. The warning fires once, at grace_seconds - warn_seconds.
    clock.advance(31)
    daemon.tick()
    daemon.tick()
    assert harness.titles() == ["Meeting detected", "Meeting seems over"]
    assert harness.notifications[-1].action == ("munin", "stop")

    # 6. The grace period expires and the recording stops by itself (D14).
    clock.advance(60)
    daemon.tick()
    assert harness.titles()[-1] == "Recording stopped"
    assert harness.notifications[-1].action == ("munin", "start", "--resume")
    captured = harness.session_json(session_id)
    assert captured["state"] == "captured"
    # Capture runs through the grace period too: 300 s of call plus 121 s of
    # waiting is audio that was really recorded, and the transcript keeps it.
    assert captured["segments"][0]["duration_seconds"] == pytest.approx(421.0)
    assert captured["checksums"]["mic.opus"].startswith("sha256:")
    assert (harness.home / "inbox" / session_id).is_symlink()
    assert harness.idle_calls[-1] == ["omarchy-toggle-idle", "allow-idle"]
    assert _valid_state_file(harness)["state"] == "captured"

    # 7. A resume inside the window continues the same session as segment 2.
    clock.advance(120)
    resumed = daemon.handle_start({"resume": True})
    assert resumed["resumed"] is True
    assert resumed["session_id"] == session_id
    assert resumed["segment"] == 2
    assert (harness.spool.load(session_id).directory / "mic.002.opus").exists() is False

    clock.advance(45)
    stopped = daemon.handle_stop({})
    assert stopped["session_id"] == session_id
    assert stopped["duration_seconds"] == pytest.approx(466.0)
    final = harness.session_json(session_id)
    assert [seg["index"] for seg in final["segments"]] == [1, 2]
    assert final["segments"][1]["mic"] == "mic.002.opus"
    assert final["checksums"]["mic.002.opus"].startswith("sha256:")

    # The audit trail is whole, and only the daemon wrote it.
    moves = [(row["from"], row["to"]) for row in final["history"]]
    assert moves == [
        (None, "recording"),
        ("recording", "ending"),
        ("ending", "captured"),
        ("captured", "recording"),
        ("recording", "captured"),
    ]
    assert {row["by"] for row in final["history"]} == {"munin-rec"}


def test_resume_past_the_window_starts_a_new_session(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    first = daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(60)
    daemon.handle_stop({})

    clock.advance(harness.spool.resume_window_seconds + 1)
    second = daemon.handle_start({"resume": True})
    assert second["resumed"] is False
    assert second["session_id"] != first["session_id"]
    assert second["segment"] == 1


def test_resume_is_refused_once_the_worker_has_the_session(harness: Harness) -> None:
    """D15: a resume is legal only while the session is captured or pending."""
    daemon, clock = harness.daemon, harness.clock
    first = daemon.handle_start({"title": "Risk review"})
    clock.advance(30)
    daemon.handle_stop({})

    # The worker claims it and starts transcribing.
    session = harness.spool.load(first["session_id"])
    session.transition("pending", by="munin-work")
    session.transition("transcribing", by="munin-work")

    clock.advance(30)
    second = daemon.handle_start({"resume": True})
    assert second["resumed"] is False
    assert second["session_id"] != first["session_id"]


def test_duplicate_start_is_idempotent(harness: Harness) -> None:
    daemon = harness.daemon
    first = daemon.handle_start({"title": "Weekly quality sync"})
    again = daemon.handle_start({"title": "Weekly quality sync"})
    assert again["session_id"] == first["session_id"]
    assert again["segment"] == first["segment"] == 1
    assert len(harness.spool.sessions) == 1


def test_stop_without_a_recording_is_a_clean_error(harness: Harness) -> None:
    with pytest.raises(IpcError) as excinfo:
        harness.daemon.handle_stop({})
    assert excinfo.value.code == "not_recording"


def test_toggle_starts_then_stops(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    started = daemon.handle_toggle({"title": "Quality sync"})
    assert started["action"] == "started"
    clock.advance(10)
    stopped = daemon.handle_toggle({})
    assert stopped["action"] == "stopped"
    assert stopped["state"] == "captured"


def test_a_returning_stream_cancels_the_grace_period(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_event({"event": "call-started", "pid": 77, "app": "Beacon 365"})
    started = daemon.handle_start({"from_detection": True})
    clock.advance(20)
    daemon.handle_event({"event": "call-ended", "pid": 77})
    assert daemon.state.state == "ending"

    clock.advance(5)
    daemon.handle_event({"event": "call-started", "pid": 77, "app": "Beacon 365"})
    assert daemon.state.state == "recording"
    assert _valid_state_file(harness)["grace_deadline"] is None

    clock.advance(600)
    daemon.tick()
    assert daemon.state.state == "recording", "a cancelled grace period must not fire"
    assert harness.session_json(started["session_id"])["state"] == "recording"


def test_sigterm_mid_recording_leaves_a_captured_session(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    started = daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(90)

    daemon.shutdown()  # what the SIGTERM handler leads to

    on_disk = harness.session_json(started["session_id"])
    assert on_disk["state"] == "captured"
    assert on_disk["segments"][0]["duration_seconds"] == pytest.approx(90.0)
    assert on_disk["checksums"]
    assert (harness.home / "inbox" / started["session_id"]).is_symlink()
    assert harness.idle_calls[-1] == ["omarchy-toggle-idle", "allow-idle"]
    assert _valid_state_file(harness)["state"] == "captured"
    # D14: stopping without being asked always says so.
    assert harness.titles()[-1] == "Recording stopped"


def test_the_state_file_is_valid_json_after_every_transition(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    seen = []
    daemon.handle_event({"event": "call-started", "pid": 9, "app": "Beacon 365"})
    seen.append(_valid_state_file(harness)["state"])
    daemon.handle_start({"from_detection": True})
    seen.append(_valid_state_file(harness)["state"])
    clock.advance(10)
    daemon.handle_event({"event": "call-ended", "pid": 9})
    seen.append(_valid_state_file(harness)["state"])
    clock.advance(121)
    daemon.tick()
    seen.append(_valid_state_file(harness)["state"])
    assert seen == ["detected", "recording", "ending", "captured"]


def test_a_capture_that_never_starts_fails_the_session(harness: Harness, monkeypatch) -> None:
    from .fakes import recording_capturer_factory

    harness.daemon.capturer_factory = recording_capturer_factory(
        harness.clock, fail_on_start=True
    )
    with pytest.raises(IpcError) as excinfo:
        harness.daemon.handle_start({"title": "Weekly quality sync"})
    assert excinfo.value.code == "capture_failed"

    session = harness.spool.sessions[-1]
    on_disk = json.loads(session.json_path.read_text(encoding="utf-8"))
    assert on_disk["state"] == "failed"
    assert on_disk["error"]["code"] == "capture_failed"
    assert _valid_state_file(harness)["state"] == "failed"
    # Capture never came up, so stay-awake was never taken -- and what was never
    # taken is never released. A daemon that called `allow-idle` here would
    # switch off a hold the user had set by hand.
    assert harness.idle_calls == []


def test_a_full_disk_refuses_to_start(harness: Harness) -> None:
    harness.spool._free_mb = 10
    with pytest.raises(IpcError) as excinfo:
        harness.daemon.handle_start({"title": "Weekly quality sync"})
    assert excinfo.value.code == "no_space"
    assert harness.spool.sessions == []


def test_stay_awake_already_held_is_left_alone(harness: Harness, tmp_path, monkeypatch) -> None:
    """Contracts 12: restore the prior state, never unconditionally allow-idle."""
    indicator = tmp_path / "state" / "omarchy" / "indicators" / "stay-awake"
    indicator.parent.mkdir(parents=True)
    indicator.write_text("")

    daemon, clock = harness.daemon, harness.clock
    daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(5)
    daemon.handle_stop({})
    assert harness.idle_calls == [], "a user who keeps the machine awake keeps it awake"


def test_list_reports_what_the_plugin_needs(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    started = daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(12)
    daemon.handle_stop({})

    payload = daemon.handle_list({"limit": 5})
    assert len(payload["sessions"]) == 1
    row = payload["sessions"][0]
    assert set(row) == {
        "id", "state", "title", "started_at", "duration_seconds", "path", "pending_reason"
    }
    assert row["id"] == started["session_id"]
    assert row["state"] == "captured"

    with pytest.raises(IpcError) as excinfo:
        daemon.handle_list({"limit": 0})
    assert excinfo.value.code == "bad_request"


def test_ping_and_status_shapes(harness: Harness) -> None:
    ping = harness.daemon.handle_ping({})
    assert set(ping) == {"pid", "version"}
    status = harness.daemon.handle_status({})
    for key in ("schema_version", "state", "since", "started_at", "title", "session",
                "session_id", "segment", "detected_app", "grace_deadline", "queue_depth",
                "last_error", "idle_was_inhibited", "detection_rules", "transcription_backend", "resume_window_seconds", "updated_at",
                "daemon_pid"):
        assert key in status


def test_state_json_publishes_the_detection_table(harness: Harness) -> None:
    """D13: another meeting application is a row in config.toml, never code.

    The plugin is the default detection source and cannot read config.toml, so
    a rule the user adds reaches it only if the daemon publishes the table.
    """
    from dataclasses import replace

    from munin.detect.base import AppRule

    daemon = harness.daemon
    daemon.config = replace(
        daemon.config,
        detection=replace(
            daemon.config.detection,
            apps=(AppRule("beacon-native", "Beacon 365", client_name="Beacon"),),
        ),
    )
    daemon.state.detection_rules = _rule_rows(daemon.config)
    daemon._write_state()

    rules = _valid_state_file(harness)["detection_rules"]
    assert rules == [
        {"app_id": "beacon-native", "label": "Beacon 365", "client_name": "Beacon"}
    ], "empty fields are dropped; a null is not a constraint"


def test_an_unknown_event_is_a_bad_request(harness: Harness) -> None:
    with pytest.raises(IpcError) as excinfo:
        harness.daemon.handle_event({"event": "call-paused"})
    assert excinfo.value.code == "bad_request"


def test_detection_can_be_turned_off(harness: Harness) -> None:
    from dataclasses import replace

    harness.daemon.config = replace(
        harness.daemon.config,
        detection=replace(harness.daemon.config.detection, enabled=False),
    )
    reply = harness.daemon.handle_event({"event": "call-started", "pid": 1, "app": "X"})
    assert reply["accepted"] is False
    assert harness.notifications == []


def test_an_unbuilt_platform_capturer_is_a_clear_failure(harness: Harness) -> None:
    """A stub capturer must not reach the user as a NotImplementedError."""

    def factory(mic, app):
        raise NotImplementedError("capture backend munin.capture.linux is a stub")

    harness.daemon.capturer_factory = factory
    with pytest.raises(IpcError) as excinfo:
        harness.daemon.handle_start({"title": "Weekly quality sync"})
    assert excinfo.value.code == "capture_failed"
    assert "not implemented" in str(excinfo.value)
    assert harness.spool.sessions[-1].state == "failed"


def test_the_fake_capturer_only_appears_when_asked(monkeypatch, harness: Harness) -> None:
    """MUNIN_FAKE_CAPTURE is a development aid, never a silent fallback."""
    from munin.capture.base import CaptureTarget
    from munin.daemon import SilentCapturer, _default_capturer_factory

    monkeypatch.delenv("MUNIN_FAKE_CAPTURE", raising=False)
    factory = _default_capturer_factory(harness.daemon.config)
    mic = CaptureTarget(kind="mic", handle="default", label="microphone")
    assert not isinstance(factory(mic, None), SilentCapturer)

    monkeypatch.setenv("MUNIN_FAKE_CAPTURE", "1")
    assert isinstance(factory(mic, None), SilentCapturer)


def test_an_auto_stop_that_fails_does_not_take_the_daemon_down(harness: Harness) -> None:
    """Nobody is holding a socket for an auto-stop, so it must not raise."""
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_event({"event": "call-started", "pid": 5, "app": "Beacon 365"})
    started = daemon.handle_start({"from_detection": True})

    # Both tracks turn out to be unplayable when the encoders are flushed.
    daemon.capturer.fail_on_stop = True  # type: ignore[union-attr]
    clock.advance(10)
    daemon.handle_event({"event": "call-ended", "pid": 5})
    clock.advance(121)
    daemon.tick()  # must not raise

    assert daemon.state.state == "failed"
    assert harness.session_json(started["session_id"])["state"] == "failed"
    assert harness.titles()[-1] == "Recording stopped"
    assert harness.notifications[-1].urgency == "critical"


# ---------------------------------------------------------------------------
# The playback handle is only as good as the call behind it. Reusing one for a
# call that has ended makes pw-record fall back to the desktop-sink monitor,
# which records every other application too (contracts 3, `app_source`).
# ---------------------------------------------------------------------------


def _app_target(harness: Harness):
    made = harness.daemon.capturer_factory.made  # type: ignore[attr-defined]
    return made[-1].app


def test_a_finished_call_takes_its_handle_with_it(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": "Beacon 365", "handle": "9911"}
    )
    daemon.handle_start({"from_detection": True})
    assert _app_target(harness).handle == "9911"

    clock.advance(60)
    daemon.handle_stop({})  # the user presses the keybind: auto=False
    daemon.handle_event({"event": "call-ended", "pid": 4242})  # the call then ends

    clock.advance(3600)
    daemon.handle_start({"title": "Something else entirely"})
    from munin.capture.base import SYSTEM_OUTPUT_HANDLE

    target = _app_target(harness)
    assert target is None or target.handle == SYSTEM_OUTPUT_HANDLE, (
        "a dead handle must never be reused; an ad-hoc start may only take the "
        "deliberate output-mix target, which is announced"
    )
    assert daemon.state.detected_app is None


def test_a_manual_stop_during_a_live_call_keeps_the_handle(harness: Harness) -> None:
    """The call is still up, so a resume must rebind the same stream."""
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": "Beacon 365", "handle": "9911"}
    )
    daemon.handle_start({"from_detection": True})
    clock.advance(60)
    daemon.handle_stop({})

    clock.advance(10)
    resumed = daemon.handle_start({"resume": True})
    assert resumed["resumed"] is True
    assert _app_target(harness).handle == "9911"


def test_an_auto_stop_takes_the_handle_with_it(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": "Beacon 365", "handle": "9911"}
    )
    daemon.handle_start({"from_detection": True})
    clock.advance(30)
    daemon.handle_event({"event": "call-ended", "pid": 4242})
    clock.advance(daemon.config.detection.grace_seconds + 1)
    daemon.tick()

    assert daemon.state.state == "captured"
    assert daemon._playback_handle is None


# ---------------------------------------------------------------------------
# Racing the worker for a session (D15).
# ---------------------------------------------------------------------------


def test_a_resume_that_loses_the_race_opens_a_fresh_session(harness: Harness) -> None:
    """The worker claimed the session between resumable() and the transition."""
    daemon, clock = harness.daemon, harness.clock
    first = daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(30)
    daemon.handle_stop({})

    # munin-work gets there first: captured -> pending is its transition, and
    # the daemon's captured -> recording is then illegal.
    session = harness.spool.load(first["session_id"])
    session.transition("pending", by="munin-work")
    session.transition("transcribing", by="munin-work")

    clock.advance(10)
    started = daemon.handle_start({"title": "Weekly quality sync"})
    assert started["resumed"] is False
    assert started["session_id"] != first["session_id"]
    assert daemon.state.state == "recording"
    assert harness.session_json(first["session_id"])["state"] == "transcribing"


# ---------------------------------------------------------------------------
# Idle inhibit: release only what this process took (contracts 12).
# ---------------------------------------------------------------------------


def test_a_daemon_that_never_recorded_leaves_stay_awake_alone(harness: Harness) -> None:
    """The user set stay-awake by hand; `systemctl --user restart` must not undo it."""
    harness.daemon.shutdown()
    assert harness.idle_calls == []


def test_stopping_a_recording_still_releases_the_hold(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_start({"title": "Weekly quality sync"})
    assert harness.idle_calls == [["omarchy-toggle-idle", "stay-awake"]]
    clock.advance(30)
    daemon.handle_stop({})
    assert harness.idle_calls[-1] == ["omarchy-toggle-idle", "allow-idle"]

    harness.idle_calls.clear()
    daemon.shutdown()
    assert harness.idle_calls == [], "the hold is already released; do not release it twice"


# ---------------------------------------------------------------------------
# Capture health: a track that dies mid-meeting (spec 11, D14).
# ---------------------------------------------------------------------------


def test_a_dead_microphone_track_ends_the_recording(harness: Harness) -> None:
    """ffmpeg writes nothing until it closes the file, so waiting for stop loses the hour."""
    daemon, clock = harness.daemon, harness.clock
    started = daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(300)
    daemon.capturer.dead_tracks = ["mic"]  # type: ignore[union-attr]

    daemon.tick()

    assert daemon.state.state == "captured"
    assert harness.session_json(started["session_id"])["state"] == "captured"
    assert harness.titles()[-1] == "Recording stopped", "D14: never stop silently"


def test_a_dead_app_track_is_reported_once_and_keeps_the_microphone(
    harness: Harness,
) -> None:
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_start({"title": "Weekly quality sync"})
    clock.advance(60)
    daemon.capturer.dead_tracks = ["app"]  # type: ignore[union-attr]

    daemon.tick()
    assert daemon.state.state == "recording"
    assert harness.titles()[-1] == "Recording problem"
    assert _valid_state_file(harness)["last_error"]

    clock.advance(600)
    daemon.tick()
    assert harness.titles().count("Recording problem") == 1, (
        "a dead process stays dead; do not notify on every tick"
    )


def test_health_is_not_polled_more_often_than_it_can_change(harness: Harness) -> None:
    from munin.daemon import HEALTH_POLL_SECONDS

    daemon, clock = harness.daemon, harness.clock
    daemon.handle_start({"title": "Weekly quality sync"})
    daemon.tick()  # the first tick of a recording always looks
    daemon.capturer.dead_tracks = ["app"]  # type: ignore[union-attr]

    clock.advance(HEALTH_POLL_SECONDS - 1)
    daemon.tick()
    assert "Recording problem" not in harness.titles()

    clock.advance(2)
    daemon.tick()
    assert harness.titles().count("Recording problem") == 1


def test_a_widened_app_capture_says_so_while_it_is_happening(harness: Harness) -> None:
    """spec 12: the desktop mix holds people who were never in the meeting."""
    from .fakes import FakeCapturer

    daemon = harness.daemon

    def widened(mic, app):
        capturer = FakeCapturer(mic, app, clock=harness.clock)
        capturer.app_source = "sink-monitor"
        return capturer

    daemon.capturer_factory = widened  # type: ignore[assignment]
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": "Beacon 365", "handle": "9911"}
    )
    daemon.handle_start({"from_detection": True})

    assert harness.titles()[-1] == "Recording all desktop audio"
    assert harness.notifications[-1].urgency == "critical"
    assert daemon.state.state == "recording", "a warning is not a failure"


def test_an_isolated_stream_says_nothing(harness: Harness) -> None:
    daemon = harness.daemon
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": "Beacon 365", "handle": "9911"}
    )
    daemon.handle_start({"from_detection": True})
    assert "Recording all desktop audio" not in harness.titles()


def test_a_private_sink_is_isolated_too(harness: Harness) -> None:
    """``process-sink`` holds one application, so there is nothing to warn about."""
    from .fakes import FakeCapturer

    daemon = harness.daemon

    def isolated(mic, app):
        capturer = FakeCapturer(mic, app, clock=harness.clock)
        capturer.app_source = "process-sink"
        return capturer

    daemon.capturer_factory = isolated  # type: ignore[assignment]
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": "Beacon 365", "handle": "9911"}
    )
    daemon.handle_start({"from_detection": True})

    assert "Recording all desktop audio" not in harness.titles()
    assert daemon.state.last_error is None


def test_the_detected_call_hands_the_capturer_its_pid(harness: Harness) -> None:
    """Without the pid the capturer cannot give the application its own sink."""
    daemon = harness.daemon
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": "Beacon 365", "handle": "9911"}
    )
    daemon.handle_start({"from_detection": True})
    assert daemon.capturer.app.pid == 4242  # type: ignore[union-attr]
    assert daemon.capturer.app.handle == "9911"  # type: ignore[union-attr]


def test_a_detected_pid_without_a_stream_handle_still_gets_its_own_target(
    harness: Harness,
) -> None:
    """Measured: `munin event call-started --pid N` with no --handle fell through
    to the desktop mix. The process is what process-sink needs; the handle is
    only for the one-node fallback."""
    daemon = harness.daemon
    daemon.handle_event({"event": "call-started", "pid": 4242, "app": "Beacon 365"})
    daemon.handle_start({"from_detection": True})
    target = daemon.capturer.app  # type: ignore[union-attr]
    assert target is not None and target.pid == 4242
    assert target.handle == ""
    assert "No meeting found, recording desktop audio" not in harness.titles()
