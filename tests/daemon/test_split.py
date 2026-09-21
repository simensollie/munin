"""The boundary between two meetings in one recording (D26).

The case these cover is the one that actually happens: a meeting ends, the user
walks into the next one, and nobody stops the recording. The daemon can see it
-- a different call goes live while it is capturing -- and D4 says what it may
do about it, which is ask. Nothing here splits anything on a timer.

The other half of D26, cutting a session that was already captured, is
``tests/worker/test_split.py``: that one needs ffmpeg, this one needs a clock.
"""

from __future__ import annotations

import logging

from dataclasses import replace
from datetime import datetime

import pytest

from munin.detect.base import CallEvidence, DetectedCall, Identity, WindowInfo
from munin.ipc import IpcError
from munin.notify import build_argv

from .conftest import Harness

FIRST = {
    "event": "call-started",
    "pid": 4242,
    "app": "Beacon 365",
    "app_id": "beacon-tab",
    "title": "Weekly quality sync | Beacon 365",
    "handle": "57",
}
SECOND = {
    "event": "call-started",
    "pid": 5150,
    "app": "Beacon 365",
    "app_id": "beacon-tab",
    "title": "Supplier audit follow-up | Beacon 365",
    "handle": "61",
}


def _call(event: dict) -> DetectedCall:
    """The shape ``Detector.scan`` returns, built from one of the event dicts."""
    return DetectedCall(
        evidence=CallEvidence(
            pid=event["pid"],
            has_playback=True,
            has_capture=True,
            playback_handle=event["handle"],
        ),
        identity=Identity(
            app_id=event["app_id"], label=event["app"], matched_by="window_title"
        ),
        window=WindowInfo(pid=event["pid"], title=event["title"]),
        observed_at=datetime(2026, 9, 14, 13, 25, 7).astimezone(),
    )


def _poll_from_the_daemon(daemon) -> None:
    """Switch this daemon onto its own poller, with two live calls to find.

    The config is frozen (it is read by three processes), so the switch is a
    replacement rather than an assignment.
    """
    daemon.config = replace(
        daemon.config, detection=replace(daemon.config.detection, source="daemon")
    )
    daemon._detector = TwoCallDetector()


class TwoCallDetector:
    """Two meetings live at once, the recorded one first: the D26 case."""

    def scan(self, *, now=None) -> list[DetectedCall]:
        return [_call(FIRST), _call(SECOND)]

    def describe(self) -> dict[str, str]:
        return {"platform": "fake"}


def _record_first_meeting(harness: Harness, *, seconds: float = 1800) -> str:
    """Record the first meeting, and let it run: a split needs a meeting to cut."""
    harness.daemon.handle_event(dict(FIRST))
    started = harness.daemon.handle_start({"from_detection": True})
    harness.clock.advance(seconds)
    harness.notifications.clear()
    return started["session_id"]


def test_a_different_call_while_recording_offers_a_split(harness: Harness) -> None:
    daemon = harness.daemon
    _record_first_meeting(harness)

    accepted = daemon.handle_event(dict(SECOND))

    assert accepted == {"accepted": True, "state": "recording"}
    assert harness.titles() == ["New meeting detected"]
    note = harness.notifications[0]
    assert note.action == ("munin", "split", "--now")
    assert "--exec" in build_argv(note)
    # D4 and D26: suggested, never acted on. The first meeting is still the one
    # being recorded, and it is still the same session.
    assert daemon.state.state == "recording"
    assert daemon.session is not None
    assert len(daemon.session.segments) == 1


def test_the_offer_is_made_once_per_call_not_once_per_poll(harness: Harness) -> None:
    """``_poll_detection`` re-reports the same call every 5 s. Ask once."""
    daemon = harness.daemon
    _record_first_meeting(harness)

    for _ in range(4):
        daemon.handle_event(dict(SECOND))

    assert harness.titles() == ["New meeting detected"]


def test_the_same_call_reappearing_is_not_a_new_meeting(harness: Harness) -> None:
    daemon = harness.daemon
    _record_first_meeting(harness)

    daemon.handle_event(dict(FIRST))

    assert harness.titles() == []


def test_a_title_change_alone_is_enough_to_ask(harness: Harness) -> None:
    """Two meetings in one Teams window share a pid and differ in the title.

    The cost of reading that as a new meeting when it is a renamed window is one
    dismissed notification; the cost of missing it is two meetings in one file.
    """
    daemon = harness.daemon
    _record_first_meeting(harness)

    daemon.handle_event(dict(FIRST, title="Supplier audit follow-up | Beacon 365"))

    assert harness.titles() == ["New meeting detected"]


def test_a_same_pid_rename_is_logged_for_the_open_question(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """OQ13 is "how often does an application rename its own window mid-call",
    and nothing has ever counted it. The prompt is unchanged -- the false
    positive is still the error worth having -- but the case now leaves a line
    behind, so a week of real meetings answers the question.
    """
    _record_first_meeting(harness)

    with caplog.at_level(logging.INFO, logger="munin.daemon"):
        harness.daemon.handle_event(dict(FIRST, title="Screen sharing | Beacon 365"))

    assert harness.titles() == ["New meeting detected"]
    logged = [r.getMessage() for r in caplog.records if "renamed or next meeting" in r.getMessage()]
    assert len(logged) == 1
    assert "Screen sharing" in logged[0]


def test_a_different_pid_is_not_logged_as_a_rename(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A second client is a second call, not a renamed window; counting it would
    make the OQ13 tally useless."""
    _record_first_meeting(harness)

    with caplog.at_level(logging.INFO, logger="munin.daemon"):
        harness.daemon.handle_event(dict(SECOND))

    assert harness.titles() == ["New meeting detected"]
    assert not [r for r in caplog.records if "renamed or next meeting" in r.getMessage()]


def test_the_panels_keep_recording_button_never_offers_a_split(
    harness: Harness,
) -> None:
    """The panel sends a bare ``munin event call-started`` to cancel the grace
    period. It carries no pid, and it is an answer about *this* meeting."""
    daemon = harness.daemon
    _record_first_meeting(harness)
    daemon.handle_event({"event": "call-ended", "pid": FIRST["pid"]})
    assert daemon.state.state == "ending"
    harness.notifications.clear()

    daemon.handle_event({"event": "call-started"})

    assert daemon.state.state == "recording"
    assert harness.titles() == []


def test_a_new_call_inside_the_grace_period_offers_a_split(harness: Harness) -> None:
    """The commonest shape of the bug: meeting one ends, meeting two starts
    inside the two-minute grace period, and the returning-stream rule glues them
    together. Keep the audio, then ask."""
    daemon = harness.daemon
    _record_first_meeting(harness)
    daemon.handle_event({"event": "call-ended", "pid": FIRST["pid"]})
    assert daemon.state.state == "ending"
    harness.notifications.clear()

    daemon.handle_event(dict(SECOND))

    assert daemon.state.state == "recording", "the recording must survive the boundary"
    assert daemon.state.grace_deadline is None
    assert harness.titles() == ["New meeting detected"]


def test_an_adhoc_recording_is_not_offered_a_split(harness: Harness) -> None:
    """Nothing was identified for this session, so there is nothing to compare a
    call against -- and a call going live is as likely to be this meeting being
    joined late as a second one. ``on_call_ended`` declines the mirror case."""
    daemon = harness.daemon
    daemon.handle_start({"title": "Desk notes"})
    harness.notifications.clear()

    daemon.handle_event(dict(SECOND))

    assert harness.titles() == []


def test_the_poller_reports_the_call_that_is_not_being_recorded(
    harness: Harness,
) -> None:
    """``detector.scan()`` returns both live calls, in PipeWire's order, not in
    the order they started. Reporting the first would re-report the meeting
    already on the record for ever, and the second would never be seen."""
    daemon = harness.daemon
    _record_first_meeting(harness)
    _poll_from_the_daemon(daemon)

    daemon._poll_detection(harness.clock())

    assert harness.titles() == ["New meeting detected"]
    assert daemon._pending_call is not None
    assert daemon._pending_call["pid"] == SECOND["pid"]


def test_the_poller_still_reports_the_one_call_when_nothing_is_recording(
    harness: Harness,
) -> None:
    daemon = harness.daemon
    _poll_from_the_daemon(daemon)

    daemon._poll_detection(harness.clock())

    assert harness.titles() == ["Meeting detected"]
    assert daemon.state.detected_app["pid"] == FIRST["pid"], "the first, as before"


def test_split_closes_one_meeting_and_opens_the_next(harness: Harness) -> None:
    daemon = harness.daemon
    first_id = _record_first_meeting(harness)
    daemon.handle_event(dict(SECOND))
    harness.notifications.clear()

    payload = daemon.handle_split({})

    second_id = payload["session_id"]
    assert payload["closed_id"] == first_id
    assert payload["state"] == "recording"
    assert payload["closed_duration_seconds"] == pytest.approx(1800, abs=2)
    assert second_id != first_id

    closed = harness.session_json(first_id)
    assert closed["state"] == "captured"
    assert closed["split_into"] == [second_id]
    last = closed["history"][-1]
    assert last["to"] == "captured"
    assert "split here" in last["note"]

    opened = harness.session_json(second_id)
    assert opened["state"] == "recording"
    assert opened["split_from"] == {
        "session": first_id,
        "kind": "live",
        "part": 2,
        "offset_seconds": None,
    }
    # The new session records the call that prompted the split, not the one that
    # has just ended -- otherwise the second meeting carries the first one's
    # application and window title on its record.
    assert opened["app"]["app_id"] == "beacon-tab"
    assert daemon.state.detected_app["pid"] == SECOND["pid"]
    assert daemon.state.state == "recording"
    assert daemon.state.session_id == second_id


def test_split_takes_a_title_for_the_new_meeting(harness: Harness) -> None:
    daemon = harness.daemon
    _record_first_meeting(harness)
    daemon.handle_event(dict(SECOND))

    payload = daemon.handle_split({"title": "Supplier audit follow-up"})

    assert harness.session_json(payload["session_id"])["title"] == (
        "Supplier audit follow-up"
    )


def test_split_without_a_second_call_still_starts_a_new_session(
    harness: Harness,
) -> None:
    """The keybind and the panel can split without a detection behind it."""
    daemon = harness.daemon
    first_id = _record_first_meeting(harness)

    payload = daemon.handle_split({})

    assert payload["closed_id"] == first_id
    opened = harness.session_json(payload["session_id"])
    assert opened["state"] == "recording"
    # No new call named it, but the daemon is still bound to the application it
    # was recording, and the record has to say what the app track holds.
    assert opened["app"]["app_id"] == "beacon-tab"
    assert opened["source"] == "adhoc", "nothing prompted this one"


def test_a_recording_seconds_old_has_nothing_to_split_off(harness: Harness) -> None:
    """The notification's action, clicked twice, arrives here twice."""
    daemon = harness.daemon
    _record_first_meeting(harness)
    daemon.handle_event(dict(SECOND))
    daemon.handle_split({})
    sessions_before = len(harness.spool.sessions)

    with pytest.raises(IpcError) as excinfo:
        daemon.handle_split({})

    assert excinfo.value.code == "too_soon"
    assert len(harness.spool.sessions) == sessions_before, "no session for nothing"
    assert daemon.state.state == "recording", "the second meeting keeps running"


def test_a_call_that_ends_stops_being_a_split_candidate(harness: Harness) -> None:
    """Offered, ignored, and then that meeting closes. A later panel split must
    not bind the new session to an application that is gone: the record would
    name it and the capturer would take the desktop mix instead (spec 12)."""
    daemon = harness.daemon
    _record_first_meeting(harness)
    daemon.handle_event(dict(SECOND))
    daemon.handle_event({"event": "call-ended", "pid": SECOND["pid"]})

    payload = daemon.handle_split({})

    opened = harness.session_json(payload["session_id"])
    assert opened["app"]["pid"] == FIRST["pid"], "the live call, not the dead one"
    assert daemon.state.detected_app["pid"] == FIRST["pid"]


def test_split_with_nothing_recording_is_refused(harness: Harness) -> None:
    with pytest.raises(IpcError) as excinfo:
        harness.daemon.handle_split({})
    assert excinfo.value.code == "not_recording"


def test_split_refuses_a_title_that_is_not_a_string(harness: Harness) -> None:
    _record_first_meeting(harness)
    with pytest.raises(IpcError) as excinfo:
        harness.daemon.handle_split({"title": 17})
    assert excinfo.value.code == "bad_request"


def test_the_new_session_starts_with_a_clean_slate(harness: Harness) -> None:
    """A split offered on the first meeting must not count against the second:
    the same call going live again after the split is a new question."""
    daemon = harness.daemon
    _record_first_meeting(harness)
    daemon.handle_event(dict(SECOND))
    daemon.handle_split({})
    harness.notifications.clear()

    daemon.handle_event(dict(FIRST))

    assert harness.titles() == ["New meeting detected"]
