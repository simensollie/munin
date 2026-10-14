"""Discarding a live recording (D27).

A discard is the one user action that deletes audio. What it must get right:
the audio goes, ``session.json`` stays with the deletion on the record, the
worker never sees the session, a resume cannot reopen it, and the bar goes
back to idle -- or to ``detected`` when the call is still live.
"""

from __future__ import annotations

import pytest

from munin.ipc import IpcError
from munin.spool import TERMINAL

from .conftest import Harness


def _audio_paths(harness: Harness, session_id: str) -> list:
    session = harness.spool.load(session_id)
    return [session.directory / seg.mic for seg in session.segments] + [
        session.directory / seg.app for seg in session.segments
    ]


def test_discard_deletes_the_audio_and_keeps_the_record(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    started = daemon.handle_start({"title": "Quality sync"})
    sid = started["session_id"]
    clock.advance(90)

    reply = daemon.handle_discard({})

    assert reply["session_id"] == sid
    assert reply["state"] == "discarded"
    assert reply["duration_seconds"] == pytest.approx(90.0)
    assert sorted(reply["deleted"]) == ["app.opus", "mic.opus"]
    for path in _audio_paths(harness, sid):
        assert not path.exists(), path

    record = harness.session_json(sid)
    assert record["state"] == "discarded"
    assert record["duration_seconds"] == pytest.approx(90.0)
    last = record["history"][-1]
    assert (last["from"], last["to"], last["by"]) == ("recording", "discarded", "munin-rec")
    assert "audio deleted" in last["note"]
    assert harness.spool.load(sid).json_path.exists()


def test_discarded_is_terminal_and_never_work(harness: Harness) -> None:
    daemon = harness.daemon
    sid = daemon.handle_start({"title": "Quality sync"})["session_id"]
    daemon.handle_discard({})

    assert "discarded" in TERMINAL
    assert harness.spool.queue_depth() == 0
    assert sid not in [s.id for s in harness.spool.sessions_in_state("captured", "pending")]
    assert harness.state_json()["queue_depth"] == 0


def test_the_bar_returns_to_idle_with_nothing_to_point_at(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_start({"title": "Quality sync"})
    clock.advance(5)
    daemon.handle_discard({})

    state = harness.state_json()
    assert state["state"] == "idle"
    assert state["session"] is None
    assert state["session_id"] is None
    assert state["started_at"] is None
    assert state["grace_deadline"] is None
    assert daemon.is_recording is False
    # The idle hold is released, as it is on a stop.
    assert harness.idle_calls and harness.idle_calls[-1] != harness.idle_calls[0]


def test_a_discard_cannot_be_resumed(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    first = daemon.handle_start({"title": "Quality sync"})["session_id"]
    clock.advance(30)
    daemon.handle_discard({})
    clock.advance(10)

    again = daemon.handle_start({"resume": True})
    assert again["resumed"] is False
    assert again["session_id"] != first
    assert harness.session_json(first)["state"] == "discarded"


def test_discard_during_the_grace_period_clears_it(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_event({"event": "call-started", "pid": 77, "app": "Beacon 365"})
    sid = daemon.handle_start({"from_detection": True})["session_id"]
    clock.advance(20)
    daemon.handle_event({"event": "call-ended", "pid": 77})
    assert daemon.state.state == "ending"

    daemon.handle_discard({})
    record = harness.session_json(sid)
    assert record["state"] == "discarded"
    assert record["history"][-1]["from"] == "ending"
    assert harness.state_json()["grace_deadline"] is None

    clock.advance(daemon.config.detection.grace_seconds + 1)
    daemon.tick()
    assert harness.session_json(sid)["state"] == "discarded", "the timer must not fire"


def test_discard_while_the_call_is_still_live_goes_back_to_detected(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    daemon.handle_event({"event": "call-started", "pid": 77, "app": "Beacon 365"})
    daemon.handle_start({"from_detection": True})
    clock.advance(10)
    before = len(harness.notifications)

    daemon.handle_discard({})

    assert harness.state_json()["state"] == "detected"
    assert harness.state_json()["detected_app"]["pid"] == 77
    # D4 was already honoured for this call: no second prompt.
    assert len(harness.notifications) == before


def test_discard_without_a_recording_is_a_clean_error(harness: Harness) -> None:
    with pytest.raises(IpcError) as excinfo:
        harness.daemon.handle_discard({})
    assert excinfo.value.code == "not_recording"


def test_discard_survives_a_capturer_that_fails_to_stop(harness: Harness) -> None:
    daemon, clock = harness.daemon, harness.clock
    sid = daemon.handle_start({"title": "Quality sync"})["session_id"]
    clock.advance(5)
    daemon.capturer.fail_on_stop = True

    reply = daemon.handle_discard({})
    assert reply["state"] == "discarded"
    assert reply["deleted"] == []
    assert harness.session_json(sid)["state"] == "discarded"
    assert daemon.is_recording is False


def test_a_track_that_will_not_delete_is_reported_not_claimed_gone(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discard that leaves audio behind must say so everywhere it speaks.

    The session is still discarded (D27: the record stays), but the reply, the
    history note and the bar all carry what is still on disk. "Audio deleted"
    over audio that is not would be the one lie a discard can tell.
    """
    from pathlib import Path

    daemon, clock = harness.daemon, harness.clock
    sid = daemon.handle_start({"title": "Quality sync"})["session_id"]
    clock.advance(30)
    real_unlink = Path.unlink

    def denied(self: Path, *args, **kwargs):
        if self.name == "mic.opus":
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)

    reply = daemon.handle_discard({})

    assert reply["state"] == "discarded"
    assert reply["deleted"] == ["app.opus"]
    assert reply["remaining"] == ["mic.opus"]
    assert "mic.opus" in reply["error"]
    mic, app = _audio_paths(harness, sid)
    assert mic.exists() and not app.exists()

    record = harness.session_json(sid)
    assert record["state"] == "discarded"
    note = record["history"][-1]["note"]
    assert "audio deleted: app.opus" in note
    assert "could not delete: mic.opus (" in note
    assert "Permission denied" in note

    state = harness.state_json()
    assert state["state"] == "idle"
    assert state["last_error"] and "mic.opus" in state["last_error"]

    listed = daemon.handle_list({"limit": 5})["sessions"][0]
    assert listed["state"] == "discarded"
    assert listed["audio_remaining"] == ["mic.opus"]


def test_a_clean_discard_leaves_nothing_remaining(harness: Harness) -> None:
    daemon = harness.daemon
    daemon.handle_start({"title": "Quality sync"})
    reply = daemon.handle_discard({})
    assert reply["remaining"] == []
    assert reply["error"] is None
    assert harness.state_json()["last_error"] is None
    assert daemon.handle_list({"limit": 5})["sessions"][0]["audio_remaining"] == []
