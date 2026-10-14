"""Microphone preflight in the daemon (D28).

A ghost Bluetooth device leaves a microphone that resolves and records silence.
The daemon checks health at call detection and at every segment start, tells
the user while they can still fix it, puts the fact on the record, and rolls
the segment when ``munin-audiofix`` reports a repair. It never refuses to
record (D4) and the check never blocks detection.
"""

from __future__ import annotations

from dataclasses import replace

from munin.notify import build_argv

from .conftest import Harness
from .fakes import recording_capturer_factory

CALL = {
    "event": "call-started",
    "pid": 4242,
    "app": "Beacon 365",
    "app_id": "beacon-tab",
    "handle": "57",
}


def _events(harness: Harness, session_id: str, name: str) -> list[dict]:
    return [row for row in harness.session_json(session_id)["history"] if row.get("event") == name]


def _with_repair(harness: Harness, value: str) -> None:
    config = harness.daemon.config
    harness.daemon.config = replace(config, capture=replace(config.capture, audio_repair=value))


def test_a_healthy_microphone_changes_nothing(harness: Harness) -> None:
    harness.daemon.handle_event(CALL)
    assert harness.titles() == ["Meeting detected"]
    state = harness.state_json()
    assert state["mic_health"]["state"] == "ok"
    assert state["mic_health"]["label"] == "Headset"
    assert state["audio_repair"] == "munin-audiofix"
    started = harness.daemon.handle_start({"from_detection": True})
    assert _events(harness, started["session_id"], "mic_degraded") == []


def test_the_state_file_has_no_verdict_before_the_first_check(harness: Harness) -> None:
    harness.daemon._write_state()
    assert harness.state_json()["mic_health"] is None


def test_degraded_at_detection_notifies_in_addition_and_records_nothing(
    harness: Harness,
) -> None:
    harness.mic.degrade("default-sink-missing")
    reply = harness.daemon.handle_event(CALL)

    assert reply == {"accepted": True, "state": "detected"}
    assert harness.titles() == ["Meeting detected", "Microphone not working"]
    note = harness.notifications[-1]
    assert note.urgency == "critical"
    assert note.body == (
        "Headset is set as default but no longer exists. Recording will have no "
        "mic audio. Click to fix audio."
    )
    argv = build_argv(note)
    assert argv[-2:] == ["--exec", "munin-audiofix"]
    # D4: the user still decides. A degraded mic is not a reason to record.
    assert harness.daemon.session is None
    assert harness.state_json()["mic_health"]["state"] == "degraded"
    assert harness.state_json()["mic_health"]["reason"] == "default-sink-missing"


def test_a_degraded_start_still_records_and_says_so_once(harness: Harness) -> None:
    harness.mic.degrade("profile-off")
    harness.daemon.handle_event(CALL)
    started = harness.daemon.handle_start({"from_detection": True})

    assert started["state"] == "recording", "D4: degraded is a warning, not a veto"
    rows = _events(harness, started["session_id"], "mic_degraded")
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"event", "at", "segment", "reason", "label"}
    assert (row["segment"], row["reason"], row["label"]) == (1, "profile-off", "Headset")
    # Told at detection; the start that answers it does not tell again.
    assert harness.titles().count("Microphone not working") == 1
    capturer = harness.daemon.capturer
    assert capturer.warnings == [  # type: ignore[union-attr]
        "microphone degraded: Headset is connected but has no audio profile (profile-off)"
    ]
    assert harness.state_json()["mic_health"]["state"] == "degraded"


def test_an_adhoc_degraded_start_notifies_once_per_session(harness: Harness) -> None:
    harness.mic.degrade()
    started = harness.daemon.handle_start({"title": "Weekly quality sync"})
    assert harness.titles().count("Microphone not working") == 1
    note = [n for n in harness.notifications if n.title == "Microphone not working"][0]
    assert note.replaces_id is not None

    # Stop and resume: a second segment, still degraded, on the record again
    # but not notified again.
    harness.clock.advance(60)
    harness.daemon.handle_stop({})
    harness.clock.advance(30)
    resumed = harness.daemon.handle_start({"resume": True})
    assert resumed["resumed"] is True
    rows = _events(harness, started["session_id"], "mic_degraded")
    assert [r["segment"] for r in rows] == [1, 2]
    assert harness.titles().count("Microphone not working") == 1


def test_an_empty_audio_repair_suppresses_the_action_not_the_state(harness: Harness) -> None:
    _with_repair(harness, "")
    harness.mic.degrade()
    harness.daemon.handle_event(CALL)
    started = harness.daemon.handle_start({"from_detection": True})

    assert "Microphone not working" not in harness.titles()
    assert harness.state_json()["mic_health"]["state"] == "degraded"
    assert harness.state_json()["audio_repair"] == ""
    assert len(_events(harness, started["session_id"], "mic_degraded")) == 1


def test_a_custom_repair_is_split_into_an_argv(harness: Harness) -> None:
    _with_repair(harness, "munin-audiofix headset")
    harness.mic.degrade()
    harness.daemon.handle_event(CALL)
    assert harness.notifications[-1].action == ("munin-audiofix", "headset")


def test_a_failing_check_is_unknown_and_never_blocks_detection(harness: Harness) -> None:
    harness.mic.raises = True
    reply = harness.daemon.handle_event(CALL)
    assert reply["state"] == "detected"
    assert harness.titles() == ["Meeting detected"]
    assert harness.state_json()["mic_health"]["state"] == "unknown"
    started = harness.daemon.handle_start({"from_detection": True})
    assert started["state"] == "recording"


def test_a_new_call_is_a_new_chance_to_be_told(harness: Harness) -> None:
    harness.mic.degrade()
    harness.daemon.handle_event(CALL)
    harness.daemon.handle_event({"event": "call-ended", "pid": 4242})
    harness.daemon.handle_event(dict(CALL, pid=5151))
    assert harness.titles().count("Microphone not working") == 2


def test_audio_repaired_while_recording_rolls_the_segment(harness: Harness) -> None:
    harness.mic.degrade()
    started = harness.daemon.handle_start({"title": "Weekly quality sync"})
    first = harness.daemon.capturer
    harness.clock.advance(120)
    harness.mic.heal()

    reply = harness.daemon.handle_event({"event": "audio-repaired"})

    assert reply["rolled"] is True
    assert reply["segment"] == 2
    assert reply["state"] == "recording"
    assert reply["mic_health"]["state"] == "ok"
    assert harness.daemon.capturer is not first
    assert first is not None and not first.is_running
    record = harness.session_json(started["session_id"])
    assert [s["index"] for s in record["segments"]] == [1, 2]
    assert record["segments"][0]["duration_seconds"] == 120
    assert record["segments"][1]["stopped_at"] is None
    repaired = _events(harness, started["session_id"], "audio_repaired")
    assert len(repaired) == 1 and repaired[0]["segment"] == 2
    assert set(repaired[0]) == {"event", "at", "segment"}
    # The repair worked, so the new segment is not marked degraded.
    assert [r["segment"] for r in _events(harness, started["session_id"], "mic_degraded")] == [1]
    state = harness.state_json()
    assert state["segment"] == 2
    assert state["mic_health"]["state"] == "ok"

    # And the session still finishes like any other.
    harness.clock.advance(60)
    stopped = harness.daemon.handle_stop({})
    assert stopped["duration_seconds"] == 180


def test_a_roll_that_widens_the_capture_says_so_like_a_start_does(harness: Harness) -> None:
    """spec 12: the repair rebuilds the graph, and isolation can fail again.

    Every start warns when the app track fell back to the desktop mix. The
    roll after a repair restarts capture too, so it has to warn the same way,
    or the fallback happens with nobody told until ``app_source`` is read
    afterwards.
    """
    from .fakes import FakeCapturer

    daemon = harness.daemon
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": "Beacon 365", "handle": "9911"}
    )
    daemon.handle_start({"from_detection": True})
    assert "Recording all desktop audio" not in harness.titles()
    harness.clock.advance(30)

    def widened(mic, app):
        capturer = FakeCapturer(mic, app, clock=harness.clock)
        capturer.app_source = "sink-monitor"
        return capturer

    daemon.capturer_factory = widened  # type: ignore[assignment]
    reply = daemon.handle_event({"event": "audio-repaired"})

    assert reply["rolled"] is True and reply["segment"] == 2
    assert harness.titles()[-1] == "Recording all desktop audio"
    assert harness.notifications[-1].urgency == "critical"
    assert daemon.state.state == "recording", "a warning is not a failure"
    assert "not an isolated stream" in (harness.state_json()["last_error"] or "")


def test_a_roll_that_keeps_isolation_says_nothing(harness: Harness) -> None:
    daemon = harness.daemon
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": "Beacon 365", "handle": "9911"}
    )
    daemon.handle_start({"from_detection": True})
    harness.clock.advance(30)
    reply = daemon.handle_event({"event": "audio-repaired"})
    assert reply["rolled"] is True
    assert "Recording all desktop audio" not in harness.titles()


def test_audio_repaired_that_did_not_help_marks_the_new_segment(harness: Harness) -> None:
    harness.mic.degrade()
    started = harness.daemon.handle_start({"title": "Weekly quality sync"})
    harness.clock.advance(30)
    harness.daemon.handle_event({"event": "audio-repaired"})
    rows = _events(harness, started["session_id"], "mic_degraded")
    assert [r["segment"] for r in rows] == [1, 2]
    assert harness.titles().count("Microphone not working") == 1


def test_audio_repaired_when_not_recording_only_rechecks(harness: Harness) -> None:
    harness.mic.degrade()
    harness.daemon.handle_event(CALL)
    harness.mic.heal()
    reply = harness.daemon.handle_event({"event": "audio-repaired"})
    assert reply["rolled"] is False
    assert reply["state"] == "detected"
    assert harness.state_json()["mic_health"]["state"] == "ok"
    assert harness.daemon.session is None


def test_audio_repaired_is_accepted_with_detection_off(harness: Harness) -> None:
    config = harness.daemon.config
    harness.daemon.config = replace(
        config, detection=replace(config.detection, enabled=False)
    )
    reply = harness.daemon.handle_event({"event": "audio-repaired"})
    assert reply["accepted"] is True


def test_a_roll_that_cannot_restart_finishes_the_session(harness: Harness) -> None:
    started = harness.daemon.handle_start({"title": "Weekly quality sync"})
    harness.clock.advance(60)

    working = harness.daemon.capturer_factory
    broken = recording_capturer_factory(harness.clock, fail_on_start=True)
    harness.daemon.capturer_factory = broken
    try:
        reply = harness.daemon.handle_event({"event": "audio-repaired"})
    finally:
        harness.daemon.capturer_factory = working

    assert reply["rolled"] is False
    assert harness.daemon.state.state == "captured"
    record = harness.session_json(started["session_id"])
    assert record["state"] == "captured"
    # No empty segment was left behind for the restart that never happened.
    assert [s["index"] for s in record["segments"]] == [1]
    assert harness.titles()[-1] == "Recording stopped", "D14: never stop silently"


def test_a_dead_microphone_says_why_when_it_was_the_ghost(harness: Harness) -> None:
    harness.daemon.handle_start({"title": "Weekly quality sync"})
    harness.clock.advance(300)
    harness.mic.degrade("profile-off")
    harness.daemon.capturer.dead_tracks = ["mic"]  # type: ignore[union-attr]

    harness.daemon.tick()

    assert harness.daemon.state.state == "captured"
    state = harness.state_json()
    assert state["mic_health"]["state"] == "degraded"
    assert state["mic_health"]["reason"] == "profile-off"

