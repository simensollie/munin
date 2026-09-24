"""What a detected session gets called.

The clock fallback names every meeting of the day the same thing, so the window
title is tried first. Teams shapes that title as ``[(n) ]<surface> | <context> |
<app>``, and only the context field is ever a name.

Names here are synthetic on purpose: a real window title carries a real
colleague or customer, which never enters this repo.
"""

from __future__ import annotations

import pytest

from munin.daemon import meeting_subject

from .conftest import Harness

LABEL = "Beacon 365"


@pytest.mark.parametrize(
    ("window_title", "expected"),
    [
        # The calendar surface names the invite: the one case that is a subject.
        ("Calendar | Weekly quality sync | Beacon 365", "Weekly quality sync"),
        # An unread badge is a notification count, not part of a name.
        ("(2) Calendar | Weekly quality sync | Beacon 365", "Weekly quality sync"),
        ("(17) Chat | Ola Nordmann | Beacon 365", "Ola Nordmann"),
        # A chat call has no subject in Teams at all; the participants are the
        # best the window can offer, and they still beat a clock.
        ("Chat | Ola Nordmann | Beacon 365", "Ola Nordmann"),
        # A group chat abbreviates the tail as "+2", which would slug to the
        # same "-2" a colliding directory appends.
        ("Chat | Ola Nordmann, Kari Nordmann, +2 | Beacon 365", "Ola Nordmann, Kari Nordmann"),
        # A browser appends its own name to the tab title.
        ("Weekly quality sync | Beacon 365 - Chromium", "Weekly quality sync"),
        # No recognised surface: keep the whole title rather than guess.
        ("Risk review | Beacon 365", "Risk review"),
    ],
)
def test_the_context_field_becomes_the_name(window_title: str, expected: str) -> None:
    assert meeting_subject(window_title, LABEL) == expected


@pytest.mark.parametrize(
    "window_title",
    [
        None,
        "",
        "   ",
        # Nothing but the application: a surface with no context behind it.
        "Beacon 365",
        "Chat | Beacon 365",
        "(4) Calendar | Beacon 365",
    ],
)
def test_a_title_with_no_subject_in_it_declines(window_title: str | None) -> None:
    """``None`` hands the decision back to the caller's own fallback."""
    assert meeting_subject(window_title, LABEL) is None


def test_the_app_name_is_only_stripped_from_the_end() -> None:
    """A label is never dropped from a field that is carrying the subject."""
    assert meeting_subject("Chat | Beacon 365 rollout | Beacon 365", LABEL) == (
        "Beacon 365 rollout"
    )


def test_an_unknown_label_leaves_the_title_alone() -> None:
    """Detection can identify a call with no window label to match against."""
    assert meeting_subject("Chat | Ola Nordmann | Beacon 365", None) == (
        "Ola Nordmann Beacon 365"
    )


def test_a_detected_session_is_named_from_the_window_title(harness: Harness) -> None:
    daemon = harness.daemon
    daemon.handle_event(
        {
            "event": "call-started",
            "pid": 4242,
            "app": LABEL,
            "app_id": "beacon-tab",
            "title": "Calendar | Weekly quality sync | Beacon 365",
            "handle": "57",
        }
    )
    started = daemon.handle_start({"from_detection": True})

    assert harness.session_json(started["session_id"])["title"] == "Weekly quality sync"
    assert started["session_id"].endswith("-weekly-quality-sync")


def test_an_explicit_title_still_wins(harness: Harness) -> None:
    """``munin start "..."`` is the user talking; nothing overrides it."""
    daemon = harness.daemon
    daemon.handle_event(
        {
            "event": "call-started",
            "pid": 4242,
            "app": LABEL,
            "app_id": "beacon-tab",
            "title": "Calendar | Weekly quality sync | Beacon 365",
            "handle": "57",
        }
    )
    started = daemon.handle_start({"from_detection": True, "title": "Risk review"})

    assert harness.session_json(started["session_id"])["title"] == "Risk review"


def test_a_windowless_detection_falls_back_to_the_label_and_the_clock(
    harness: Harness,
) -> None:
    """PipeWire-only matching reaches the daemon with no window at all."""
    daemon = harness.daemon
    daemon.handle_event(
        {"event": "call-started", "pid": 4242, "app": LABEL, "handle": "57"}
    )
    started = daemon.handle_start({"from_detection": True})

    title = harness.session_json(started["session_id"])["title"]
    assert title.startswith(LABEL)
