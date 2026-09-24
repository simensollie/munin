"""Microsoft 365 enrichment, the pure half (spec 7.6): matching and parsing.

No socket is opened: Graph is a fake transport and the keyring a dict. Every
subject and name is synthetic, per the repo's public-copy rule.
"""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from munin import m365
from munin.m365 import CalendarEvent, call_ended_events, direct_call_name, match_event

UTC = timezone.utc
T0 = datetime(2026, 9, 23, 7, 0, tzinfo=UTC)  # 09:00 in Oslo


def _event(subject: str, start_min: int, end_min: int, *, online: bool = True) -> CalendarEvent:
    return CalendarEvent(
        id=f"evt-{subject}",
        subject=subject,
        start=T0 + timedelta(minutes=start_min),
        end=T0 + timedelta(minutes=end_min),
        online=online,
        attendees=4,
    )


# -- matching -----------------------------------------------------------------


def test_a_call_inside_an_event_is_that_event() -> None:
    events = [_event("Risk review", 0, 60)]
    assert match_event(events, T0 + timedelta(minutes=1)).subject == "Risk review"


def test_joining_a_few_minutes_early_still_matches() -> None:
    events = [_event("Risk review", 0, 60)]
    assert match_event(events, T0 - timedelta(minutes=5)) is not None
    assert match_event(events, T0 - timedelta(minutes=15)) is None


def test_no_event_means_no_match() -> None:
    assert match_event([_event("Risk review", 0, 60)], T0 + timedelta(minutes=61)) is None


def test_the_next_meeting_wins_over_the_one_running_late() -> None:
    """09:00-10:00 overruns; a call at 09:58 is the 10:00 meeting."""
    events = [_event("Risk review", 0, 60), _event("Supplier audit", 60, 90)]
    assert match_event(events, T0 + timedelta(minutes=58)).subject == "Supplier audit"


def test_an_online_meeting_beats_a_room_booking_at_the_same_time() -> None:
    events = [_event("Office day", 0, 480, online=False), _event("Risk review", 60, 90)]
    assert match_event(events, T0 + timedelta(minutes=61)).subject == "Risk review"


def test_graph_rows_that_are_not_meetings_are_dropped() -> None:
    base = {
        "id": "a",
        "subject": "Risk review",
        "start": {"dateTime": "2026-09-23T07:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-23T08:00:00.0000000", "timeZone": "UTC"},
        "isOnlineMeeting": True,
        "attendees": [{}, {}],
    }
    event = CalendarEvent.from_graph(base)
    assert event is not None and event.start == T0 and event.attendees == 2
    assert CalendarEvent.from_graph({**base, "isAllDay": True}) is None
    assert CalendarEvent.from_graph({**base, "isCancelled": True}) is None
    assert CalendarEvent.from_graph({**base, "showAs": "free"}) is None
    assert CalendarEvent.from_graph({**base, "subject": "  "}) is None


# -- the local copy ---------------------------------------------------------------


def test_the_calendar_copy_round_trips_and_is_private(tmp_path: Path) -> None:
    events = [_event("Risk review", 0, 60)]
    path = m365.write_calendar(tmp_path, events, fetched_at=T0, window=(T0, T0))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert m365.read_calendar(tmp_path, max_age=timedelta(hours=1), now=T0) == events
    # Only what naming needs: never a body, never an attendee list.
    row = json.loads(path.read_text())["events"][0]
    assert set(row) == {"id", "subject", "start", "end", "online", "attendees"}


def test_a_stale_copy_reads_as_empty(tmp_path: Path) -> None:
    m365.write_calendar(tmp_path, [_event("Risk review", 0, 60)], fetched_at=T0, window=(T0, T0))
    later = T0 + timedelta(hours=2)
    assert m365.read_calendar(tmp_path, max_age=timedelta(hours=1), now=later) == []


def test_a_missing_or_broken_copy_reads_as_empty(tmp_path: Path) -> None:
    assert m365.read_calendar(tmp_path, max_age=timedelta(hours=1)) == []
    m365.calendar_path(tmp_path).parent.mkdir(parents=True)
    m365.calendar_path(tmp_path).write_text("{not json")
    assert m365.read_calendar(tmp_path, max_age=timedelta(hours=1)) == []


# -- direct calls -------------------------------------------------------------------

ME = "user-me"


def _call_ended(at: datetime, *people: tuple[str, str], kind: str = "call") -> dict:
    return {
        "messageType": "systemEventMessage",
        "createdDateTime": at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "eventDetail": {
            "@odata.type": "#microsoft.graph.callEndedEventMessageDetail",
            "callEventType": kind,
            "callParticipants": [
                {"participant": {"user": {"id": uid, "displayName": name}}}
                for uid, name in people
            ],
        },
    }


def test_you_are_taken_out_of_the_roster() -> None:
    [event] = call_ended_events(
        [_call_ended(T0, (ME, "Me Myself"), ("u1", "Ola Nordmann"))], ME
    )
    assert event.others == ("Ola Nordmann",)


def test_a_call_you_were_not_on_is_not_evidence() -> None:
    """Another chat's call, or one you never answered, names nobody."""
    assert call_ended_events([_call_ended(T0, ("u1", "Ola Nordmann"))], ME) == []
    assert call_ended_events(
        [_call_ended(T0, ("u1", "Ola Nordmann"), ("u2", "Kari Nordmann"))], ME
    ) == []


def test_graph_times_parse_with_any_offset_and_seven_digit_fractions() -> None:
    parse = m365._parse_time
    assert parse("2026-09-23T07:00:00.0000000") == T0
    assert parse({"dateTime": "2026-09-23T07:00:00.1234567Z"}) == T0 + timedelta(microseconds=123456)
    assert parse("2026-09-23T09:00:00.5+02:00") == T0 + timedelta(microseconds=500000)


def test_a_hand_mangled_calendar_copy_reads_as_empty(tmp_path: Path) -> None:
    path = m365.calendar_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2]")
    assert m365.read_calendar(tmp_path, max_age=timedelta(days=1)) == []
    path.write_text(json.dumps({
        "fetched_at": T0.isoformat(),
        "events": [{"id": "a", "subject": "x", "start": T0.isoformat(), "end": T0.isoformat(), "attendees": "many"}],
    }))
    assert m365.read_calendar(tmp_path, max_age=timedelta(days=1), now=T0) == []


def test_other_system_messages_are_ignored() -> None:
    added = {
        "messageType": "systemEventMessage",
        "createdDateTime": "2026-09-23T07:00:00Z",
        "eventDetail": {"@odata.type": "#microsoft.graph.membersAddedEventMessageDetail"},
    }
    assert call_ended_events([added], ME) == []


def test_a_one_to_one_call_is_named_after_the_other_person() -> None:
    events = call_ended_events(
        [_call_ended(T0 + timedelta(minutes=30), (ME, "Me"), ("u1", "Ola Nordmann"))], ME
    )
    name, decided = direct_call_name(
        events, started_at=T0, stopped_at=T0 + timedelta(minutes=32)
    )
    assert (name, decided) == ("Ola Nordmann", True)


def test_a_group_call_keeps_the_clock_name() -> None:
    events = call_ended_events(
        [
            _call_ended(
                T0 + timedelta(minutes=30),
                (ME, "Me"),
                ("u1", "Ola Nordmann"),
                ("u2", "Kari Nordmann"),
            )
        ],
        ME,
    )
    assert direct_call_name(
        events, started_at=T0, stopped_at=T0 + timedelta(minutes=30)
    ) == (None, True)


def test_a_meeting_is_not_a_direct_call() -> None:
    events = call_ended_events(
        [_call_ended(T0 + timedelta(minutes=30), (ME, "Me"), ("u1", "Ola Nordmann"), kind="meeting")], ME
    )
    assert direct_call_name(
        events, started_at=T0, stopped_at=T0 + timedelta(minutes=30)
    ) == (None, False)


def test_a_call_that_ended_at_another_time_is_not_evidence() -> None:
    events = call_ended_events(
        [_call_ended(T0 + timedelta(hours=2), (ME, "Me"), ("u1", "Ola Nordmann"))], ME
    )
    assert direct_call_name(
        events, started_at=T0, stopped_at=T0 + timedelta(minutes=30)
    ) == (None, False)


def test_two_plausible_calls_decide_nothing_rather_than_guess() -> None:
    at = T0 + timedelta(minutes=30)
    events = call_ended_events(
        [
            _call_ended(at, (ME, "Me"), ("u1", "Ola Nordmann")),
            _call_ended(at, (ME, "Me"), ("u2", "Kari Nordmann")),
        ],
        ME,
    )
    assert direct_call_name(events, started_at=T0, stopped_at=at) == (None, True)


# -- sign-in ----------------------------------------------------------------------


class DictKeyring:
    def __init__(self, secret: str | None = None) -> None:
        self.secret = secret

    def get(self) -> str | None:
        return self.secret

    def set(self, secret: str) -> None:
        self.secret = secret

    def clear(self) -> None:
        self.secret = None


class Transport:
    def __init__(self, *replies: tuple[int, dict]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.replies.pop(0)


def _graph(keyring: DictKeyring, transport: Transport, clock=lambda: 1000.0) -> m365.Graph:
    return m365.Graph("tenant", "client", keyring=keyring, transport=transport, clock=clock)


def test_device_login_polls_until_the_code_is_entered_and_keeps_the_refresh_token() -> None:
    keyring = DictKeyring()
    transport = Transport(
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
    )
    graph = _graph(keyring, transport)
    naps: list[float] = []

    graph.finish_device_login({"device_code": "dc", "interval": 5, "expires_in": 900}, sleep=naps.append)

    assert keyring.secret == "rt"
    assert naps == [5.0, 5.0, 10.0]  # slow_down adds five seconds
    assert "device_code" in transport.calls[0][3].decode()


def test_a_declined_sign_in_raises_rather_than_polling_forever() -> None:
    graph = _graph(DictKeyring(), Transport((400, {"error": "access_denied"})))
    with pytest.raises(m365.M365Error, match="access_denied"):
        graph.finish_device_login({"device_code": "dc", "interval": 1}, sleep=lambda _s: None)


def test_the_refresh_token_is_rotated_on_every_refresh() -> None:
    keyring = DictKeyring("old")
    transport = Transport(
        (200, {"access_token": "at", "refresh_token": "new", "expires_in": 3600}),
        (200, {"id": "user-me"}),
    )
    graph = _graph(keyring, transport)

    assert graph.me() == "user-me"
    assert keyring.secret == "new"
    assert transport.calls[1][2]["Authorization"] == "Bearer at"


def test_the_access_token_is_reused_until_it_nears_expiry() -> None:
    now = [1000.0]
    transport = Transport(
        (200, {"access_token": "at1", "expires_in": 3600}),
        (200, {"access_token": "at2", "expires_in": 3600}),
    )
    graph = _graph(DictKeyring("rt"), transport, clock=lambda: now[0])
    assert graph.token() == "at1"
    now[0] += 3000
    assert graph.token() == "at1"
    now[0] += 600  # inside the minute of slack before expiry
    assert graph.token() == "at2"


def test_no_refresh_token_is_not_signed_in() -> None:
    graph = _graph(DictKeyring(), Transport())
    assert not graph.signed_in()
    with pytest.raises(m365.NotSignedIn):
        graph.token()


def test_a_revoked_refresh_token_says_to_sign_in_again() -> None:
    graph = _graph(
        DictKeyring("rt"),
        Transport((400, {"error": "invalid_grant", "error_description": "AADSTS700082: expired\nTrace ID: x"})),
    )
    with pytest.raises(m365.NotSignedIn, match="munin m365 login"):
        graph.token()


def test_the_keyring_is_secret_tool_with_the_secret_on_stdin() -> None:
    calls: list[tuple[list[str], dict]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("R", (), {"returncode": 0, "stdout": "rt\n", "stderr": ""})()

    keyring = m365.Keyring("tenant/client", runner=runner)
    keyring.set("s3cret")
    assert keyring.get() == "rt"

    store_argv, store_kwargs = calls[0]
    assert store_argv[:2] == ["secret-tool", "store"]
    assert "s3cret" not in store_argv, "a secret on the command line is visible in ps"
    assert store_kwargs["input"] == "s3cret"
    assert store_argv[-4:] == ["service", "munin-m365", "account", "tenant/client"]


def test_chat_read_is_asked_for_only_with_direct_calls() -> None:
    from munin.config import M365Config

    calendar_only = m365.Graph.from_settings(
        M365Config(enabled=True, tenant_id="t", client_id="c", direct_calls=False),
        keyring=DictKeyring(),
    )
    both = m365.Graph.from_settings(
        M365Config(enabled=True, tenant_id="t", client_id="c", direct_calls=True),
        keyring=DictKeyring(),
    )
    assert "Chat.Read" not in calendar_only.scopes
    assert "Calendars.Read" in calendar_only.scopes
    assert "Chat.Read" in both.scopes

    transport = Transport((200, {"device_code": "dc", "message": "go"}))
    m365.Graph("t", "c", keyring=DictKeyring(), transport=transport,
               scopes=calendar_only.scopes).start_device_login()
    assert b"Chat.Read" not in transport.calls[0][3]
