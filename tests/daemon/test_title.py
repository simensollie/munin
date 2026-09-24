"""What a detected session gets called.

The meeting app's window title is deliberately *not* a source. Teams names its
main window after whichever chat or view is focused, not after the call: on the
reference machine one colleague's chat named four different meetings, and a
five-person calendar meeting was called after one attendee. A wrong name is
worse than a clock, so a detection falls back to the app label plus the time
(contracts amendment 2026-09-24, withdrawn the same day).

Names here are synthetic on purpose: a real window title carries a real
colleague or customer, which never enters this repo.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta

from munin import m365
from munin.config import M365Config

from .conftest import Harness

LABEL = "Beacon 365"


def _detect(harness: Harness, title: str | None) -> None:
    event = {"event": "call-started", "pid": 4242, "app": LABEL, "handle": "57"}
    if title is not None:
        event["app_id"] = "beacon-tab"
        event["title"] = title
    harness.daemon.handle_event(event)


def test_a_chat_window_title_does_not_name_the_session(harness: Harness) -> None:
    """The focused chat is not the call, so its name never becomes the title."""
    _detect(harness, "Chat | Ola Nordmann | Beacon 365")
    started = harness.daemon.handle_start({"from_detection": True})

    title = harness.session_json(started["session_id"])["title"]
    assert re.fullmatch(rf"{LABEL} \d\d:\d\d", title)
    assert "nordmann" not in started["session_id"]


def test_a_calendar_window_title_does_not_name_the_session_either(
    harness: Harness,
) -> None:
    """Even the one surface that looks like a subject is whatever view is open."""
    _detect(harness, "(2) Calendar | Weekly quality sync | Beacon 365")
    started = harness.daemon.handle_start({"from_detection": True})

    assert re.fullmatch(
        rf"{LABEL} \d\d:\d\d", harness.session_json(started["session_id"])["title"]
    )


def test_an_explicit_title_still_wins(harness: Harness) -> None:
    """``munin start "..."`` is the user talking; nothing overrides it."""
    _detect(harness, "Chat | Ola Nordmann | Beacon 365")
    started = harness.daemon.handle_start({"from_detection": True, "title": "Risk review"})

    assert harness.session_json(started["session_id"])["title"] == "Risk review"


def test_a_windowless_detection_falls_back_to_the_label_and_the_clock(
    harness: Harness,
) -> None:
    """PipeWire-only matching reaches the daemon with no window at all."""
    _detect(harness, None)
    started = harness.daemon.handle_start({"from_detection": True})

    title = harness.session_json(started["session_id"])["title"]
    assert re.fullmatch(rf"{LABEL} \d\d:\d\d", title)


# -- the calendar (spec 7.6) ----------------------------------------------------


def _with_calendar(harness: Harness, subject: str, *, fetched_ago: float = 60.0) -> None:
    now = harness.clock()
    harness.daemon.config = replace(
        harness.daemon.config, m365=M365Config(enabled=True, tenant_id="t", client_id="c")
    )
    event = m365.CalendarEvent(
        id="evt-1",
        subject=subject,
        start=now - timedelta(minutes=2),
        end=now + timedelta(minutes=28),
        online=True,
        attendees=5,
    )
    m365.write_calendar(
        harness.home,
        [event],
        fetched_at=now - timedelta(seconds=fetched_ago),
        window=(now, now),
    )


def test_the_overlapping_event_names_the_session(harness: Harness) -> None:
    _with_calendar(harness, "Weekly quality sync")
    _detect(harness, "Chat | Ola Nordmann | Beacon 365")
    started = harness.daemon.handle_start({"from_detection": True})

    record = harness.session_json(started["session_id"])
    assert record["title"] == "Weekly quality sync"
    assert record["calendar_event_id"] == "evt-1"
    assert record["enrichment"]["title_from"] == "calendar"
    assert started["session_id"].endswith("-weekly-quality-sync")


def test_the_prompt_says_which_meeting_it_would_record(harness: Harness) -> None:
    _with_calendar(harness, "Weekly quality sync")
    _detect(harness, None)
    [note] = harness.notifications
    assert note.title == "Meeting detected"
    assert note.body.startswith("Weekly quality sync\n")


def test_an_adhoc_start_inside_an_event_is_named_too(harness: Harness) -> None:
    _with_calendar(harness, "Weekly quality sync")
    started = harness.daemon.handle_start({})
    assert harness.session_json(started["session_id"])["title"] == "Weekly quality sync"


def test_the_calendar_never_overrides_an_explicit_title(harness: Harness) -> None:
    _with_calendar(harness, "Weekly quality sync")
    _detect(harness, None)
    started = harness.daemon.handle_start({"from_detection": True, "title": "Risk review"})
    record = harness.session_json(started["session_id"])
    assert record["title"] == "Risk review"
    assert record["calendar_event_id"] is None


def test_a_stale_calendar_copy_names_nothing(harness: Harness) -> None:
    """The worker stopped refreshing: a moved meeting must not keep its old slot."""
    _with_calendar(harness, "Weekly quality sync", fetched_ago=4 * 3600)
    _detect(harness, None)
    started = harness.daemon.handle_start({"from_detection": True})
    assert re.fullmatch(
        rf"{LABEL} \d\d:\d\d", harness.session_json(started["session_id"])["title"]
    )


def test_m365_off_ignores_even_a_fresh_copy(harness: Harness) -> None:
    _with_calendar(harness, "Weekly quality sync")
    harness.daemon.config = replace(harness.daemon.config, m365=M365Config())
    _detect(harness, None)
    started = harness.daemon.handle_start({"from_detection": True})
    assert harness.session_json(started["session_id"])["calendar_event_id"] is None
