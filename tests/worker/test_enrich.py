"""munin-work's half of M365 enrichment (spec 7.6): refresh, name, hold export.

Graph is a fake with the three reads the enricher makes. Every subject and name
is synthetic, per the repo's public-copy rule.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import pytest

from munin import config as config_module
from munin import m365
from munin.capture.base import segment_filenames
from munin.config import ExportConfig, M365Config
from munin.enrich import RETRY_SECONDS, Enricher
from munin.mixdown import export_name
from munin.spool import Session, Spool
from munin.worker import Worker

START = datetime(2026, 9, 23, 9, 1, 22).astimezone()
STOP = START + timedelta(minutes=30)
ME = "user-me"


class FakeGraph:
    def __init__(self, *, events=(), messages=(), signed_in=True) -> None:
        self.events = list(events)
        self.messages = list(messages)
        self._signed_in = signed_in
        self.chat_lookups = 0
        self.calendar_fetches = 0

    def signed_in(self) -> bool:
        return self._signed_in

    def calendar(self, start, end):
        self.calendar_fetches += 1
        return list(self.events)

    def recent_chat_messages(self, since):
        self.chat_lookups += 1
        return list(self.messages)

    def me(self) -> str:
        return ME


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _call_ended(at: datetime, *names: str) -> dict:
    people = [{"participant": {"user": {"id": ME, "displayName": "Me"}}}]
    people += [
        {"participant": {"user": {"id": f"u{i}", "displayName": n}}} for i, n in enumerate(names)
    ]
    return {
        "messageType": "systemEventMessage",
        "createdDateTime": at.astimezone().isoformat(),
        "eventDetail": {
            "@odata.type": "#microsoft.graph.callEndedEventMessageDetail",
            "callEventType": "call",
            "callParticipants": people,
        },
    }


def _event(subject: str, start: datetime, minutes: int = 60) -> m365.CalendarEvent:
    return m365.CalendarEvent(
        id=f"evt-{subject}", subject=subject, start=start, end=start + timedelta(minutes=minutes),
        online=True, attendees=5,
    )


@pytest.fixture()
def cfg(munin_home: Path, ample_disk: None):
    base = config_module.load()
    return replace(
        base,
        m365=M365Config(enabled=True, tenant_id="t", client_id="c", export_hold_seconds=600),
    )


@pytest.fixture()
def ample_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    gib = 1024**3
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: type("U", (), {"total": 500 * gib, "used": 0, "free": 400 * gib})(),
    )


def _detected_call(spool: Spool, *, audio: Callable | None = None) -> Session:
    """A captured Teams call with no calendar event: the direct-call case."""
    session = spool.create(
        title="Microsoft Teams 09:01",
        source="detected",
        app={"app_id": "teams-native", "label": "Microsoft Teams"},
        now=START,
    )
    spool.add_segment(session, 1, started_at=START)
    if audio is not None:
        mic, app = segment_filenames(1)
        audio(session.directory, seconds=2.0, mic_name=mic, app_name=app)
    spool.complete_segment(session, 1, stopped_at=STOP, duration_seconds=1800.0)
    session.transition("ending", by="munin-rec")
    session.transition("captured", by="munin-rec", stopped_at=STOP)
    return session


def _reload(spool: Spool, session: Session) -> Session:
    return spool.load(session.id)


# -- the calendar copy ----------------------------------------------------------


def test_the_calendar_is_refreshed_on_its_interval_only(cfg) -> None:
    clock = Clock(START)
    graph = FakeGraph(events=[_event("Risk review", START)])
    enricher = Enricher(cfg, graph=graph, clock=clock)

    assert enricher.refresh_calendar() is True
    assert enricher.refresh_calendar() is False
    clock.now += timedelta(seconds=cfg.m365.calendar_refresh_seconds)
    assert enricher.refresh_calendar() is True
    assert graph.calendar_fetches == 2
    events = m365.read_calendar(cfg.home, max_age=timedelta(hours=1), now=clock.now)
    assert [e.subject for e in events] == ["Risk review"]


def test_nothing_happens_with_m365_off(munin_home: Path) -> None:
    graph = FakeGraph(events=[_event("Risk review", START)])
    enricher = Enricher(config_module.load(), graph=graph)
    assert enricher.refresh_calendar() is False
    assert graph.calendar_fetches == 0


# -- naming -----------------------------------------------------------------------


def test_a_late_calendar_match_names_the_session_and_keeps_the_old_title(cfg) -> None:
    """Created while the copy was stale; the event was there all along."""
    spool = Spool(cfg)
    session = _detected_call(spool)
    clock = Clock(STOP + timedelta(seconds=10))
    m365.write_calendar(cfg.home, [_event("Risk review", START - timedelta(minutes=1))],
                        fetched_at=clock.now, window=(START, STOP))

    assert Enricher(cfg, graph=FakeGraph(), clock=clock).enrich(session) is True

    saved = _reload(spool, session)
    assert saved.title == "Risk review"
    assert saved.calendar_event_id == "evt-Risk review"
    assert saved.enrichment["title_from"] == "calendar"
    assert saved.enrichment["original_title"] == "Microsoft Teams 09:01"
    assert saved.id == session.id, "the id and the directory are never renamed"


def test_a_direct_call_is_named_after_the_other_person(cfg) -> None:
    spool = Spool(cfg)
    session = _detected_call(spool)
    graph = FakeGraph(messages=[_call_ended(STOP - timedelta(seconds=90), "Ola Nordmann")])
    enricher = Enricher(cfg, graph=graph, clock=Clock(STOP + timedelta(seconds=20)))

    assert enricher.enrich(session) is True
    saved = _reload(spool, session)
    assert saved.title == "Ola Nordmann"
    assert saved.enrichment["title_from"] == "direct-call"
    assert saved.calendar_event_id is None


def test_a_group_call_keeps_its_clock_title_and_is_settled(cfg) -> None:
    spool = Spool(cfg)
    session = _detected_call(spool)
    graph = FakeGraph(messages=[_call_ended(STOP, "Ola Nordmann", "Kari Nordmann")])
    enricher = Enricher(cfg, graph=graph, clock=Clock(STOP + timedelta(seconds=20)))

    assert enricher.enrich(session) is False
    saved = _reload(spool, session)
    assert saved.title == "Microsoft Teams 09:01"
    assert saved.enrichment == {
        "title_from": None,
        "original_title": None,
        "decided_at": saved.enrichment["decided_at"],
        "reason": "not a one-to-one call",
    }
    # Settled means never looked at again.
    assert enricher.enrich(saved) is False
    assert graph.chat_lookups == 1


def test_no_message_yet_looks_again_but_not_every_sweep(cfg) -> None:
    spool = Spool(cfg)
    session = _detected_call(spool)
    clock = Clock(STOP + timedelta(seconds=5))
    graph = FakeGraph()
    enricher = Enricher(cfg, graph=graph, clock=clock)

    enricher.enrich(session)
    clock.now += timedelta(seconds=5)
    enricher.enrich(_reload(spool, session))
    assert graph.chat_lookups == 1
    clock.now += timedelta(seconds=RETRY_SECONDS)
    graph.messages = [_call_ended(STOP, "Ola Nordmann")]
    assert enricher.enrich(_reload(spool, session)) is True
    assert graph.chat_lookups == 2


def test_giving_up_after_the_hold_is_recorded(cfg) -> None:
    spool = Spool(cfg)
    session = _detected_call(spool)
    clock = Clock(STOP + timedelta(seconds=5))
    enricher = Enricher(cfg, graph=FakeGraph(), clock=clock)
    enricher.enrich(session)

    clock.now = STOP + timedelta(seconds=cfg.m365.export_hold_seconds + 1)
    enricher.enrich(_reload(spool, session))

    saved = _reload(spool, session)
    assert saved.title == "Microsoft Teams 09:01"
    assert saved.enrichment["reason"] == "no call-ended message in time"


def test_a_session_from_before_enrichment_is_left_exactly_as_it_was(cfg) -> None:
    spool = Spool(cfg)
    session = _detected_call(spool)
    before = session.json_path.read_bytes()
    graph = FakeGraph(messages=[_call_ended(STOP, "Ola Nordmann")])

    Enricher(cfg, graph=graph, clock=Clock(STOP + timedelta(days=3))).enrich(session)

    assert session.json_path.read_bytes() == before
    assert graph.chat_lookups == 0


def test_an_adhoc_session_is_never_looked_up_in_chats(cfg) -> None:
    spool = Spool(cfg)
    session = spool.create(title="Meeting 09:01", source="adhoc", now=START)
    session.transition("ending", by="munin-rec")
    session.transition("captured", by="munin-rec", stopped_at=STOP)
    graph = FakeGraph(messages=[_call_ended(STOP, "Ola Nordmann")])

    Enricher(cfg, graph=graph, clock=Clock(STOP)).enrich(_reload(spool, session))
    assert graph.chat_lookups == 0


def test_a_title_set_by_the_calendar_at_capture_is_final(cfg) -> None:
    spool = Spool(cfg)
    session = _detected_call(spool)
    session.update(calendar_event_id="evt", enrichment={"title_from": "calendar"})
    graph = FakeGraph(messages=[_call_ended(STOP, "Ola Nordmann")])

    assert Enricher(cfg, graph=graph, clock=Clock(STOP)).enrich(_reload(spool, session)) is False
    assert graph.chat_lookups == 0


# -- holding export -----------------------------------------------------------------


def _exporting(cfg, tmp_path: Path):
    return replace(cfg, export=ExportConfig(enabled=True, directory=str(tmp_path / "uploads")))


def test_export_waits_for_the_name_then_carries_it(cfg, tmp_path: Path, two_track) -> None:
    cfg = _exporting(cfg, tmp_path)
    spool = Spool(cfg)
    session = _detected_call(spool, audio=two_track)
    clock = Clock(STOP + timedelta(seconds=5))
    graph = FakeGraph()
    worker = Worker(cfg, enricher=Enricher(cfg, graph=graph, clock=clock))

    worker.drain()
    assert list((tmp_path / "uploads").glob("*.opus")) == []

    clock.now += timedelta(seconds=RETRY_SECONDS)
    graph.messages = [_call_ended(STOP, "Ola Nordmann")]
    worker.drain()

    [copy] = (tmp_path / "uploads").glob("*.opus")
    assert copy.name == "2026-09-23T0901 Ola Nordmann.opus"
    assert copy.name == export_name(_reload(spool, session))


def test_a_signed_out_machine_exports_at_once(cfg, tmp_path: Path, two_track) -> None:
    cfg = _exporting(cfg, tmp_path)
    spool = Spool(cfg)
    _detected_call(spool, audio=two_track)
    enricher = Enricher(cfg, graph=FakeGraph(signed_in=False), clock=Clock(STOP))

    Worker(cfg, enricher=enricher).drain()

    [copy] = (tmp_path / "uploads").glob("*.opus")
    assert copy.name == "2026-09-23T0901 Microsoft Teams.opus"


def test_the_hold_ends_even_if_microsoft_never_answers(cfg, tmp_path: Path, two_track) -> None:
    cfg = _exporting(cfg, tmp_path)
    spool = Spool(cfg)
    _detected_call(spool, audio=two_track)
    clock = Clock(STOP + timedelta(seconds=5))
    worker = Worker(cfg, enricher=Enricher(cfg, graph=FakeGraph(), clock=clock))
    worker.drain()

    clock.now = STOP + timedelta(seconds=cfg.m365.export_hold_seconds)
    worker.drain()

    assert len(list((tmp_path / "uploads").glob("*.opus"))) == 1


# -- review findings ------------------------------------------------------------------


def test_a_title_the_user_typed_is_never_replaced(cfg) -> None:
    spool = Spool(cfg)
    session = spool.create(title="Risk review", source="adhoc", now=START)
    session.transition("ending", by="munin-rec")
    session.transition("captured", by="munin-rec", stopped_at=STOP)
    clock = Clock(STOP + timedelta(seconds=10))
    m365.write_calendar(cfg.home, [_event("Supplier audit", START)], fetched_at=clock.now,
                        window=(START, STOP))

    assert Enricher(cfg, graph=FakeGraph(), clock=clock).enrich(_reload(spool, session)) is False
    assert _reload(spool, session).title == "Risk review"


def test_a_resume_during_the_lookup_is_not_undone(cfg) -> None:
    """The sweep's snapshot is stale by the time Graph answers (D15)."""
    spool = Spool(cfg)
    session = _detected_call(spool)
    snapshot = _reload(spool, session)

    class ResumingGraph(FakeGraph):
        def recent_chat_messages(self, since):
            # munin-rec reopens the session while the worker waits on Graph.
            live = _reload(spool, session)
            live.transition("recording", by="munin-rec")
            return [_call_ended(STOP, "Ola Nordmann")]

    enricher = Enricher(cfg, graph=ResumingGraph(), clock=Clock(STOP + timedelta(seconds=20)))
    assert enricher.enrich(snapshot) is False

    saved = _reload(spool, session)
    assert saved.state == "recording", "the resume survives"
    assert saved.history[-1]["to"] == "recording"
    assert saved.enrichment is None and saved.title == "Microsoft Teams 09:01"


def test_a_failing_refresh_never_takes_the_worker_down(cfg) -> None:
    class Locked(FakeGraph):
        def calendar(self, start, end):
            raise TimeoutError("keyring unlock prompt")

    assert Enricher(cfg, graph=Locked(), clock=Clock(START)).refresh_calendar() is False


def test_an_expired_sign_in_stops_holding_exports(cfg) -> None:
    spool = Spool(cfg)
    session = _detected_call(spool)

    class Expired(FakeGraph):
        def recent_chat_messages(self, since):
            raise m365.NotSignedIn("sign-in has expired")

    clock = Clock(STOP + timedelta(seconds=5))
    enricher = Enricher(cfg, graph=Expired(), clock=clock)
    assert enricher.holding(session) is True
    enricher.enrich(session)
    assert enricher.holding(_reload(spool, session)) is False


def test_the_keyring_is_asked_once_a_minute_not_every_sweep(cfg) -> None:
    spool = Spool(cfg)
    session = _detected_call(spool)

    class Counting(FakeGraph):
        asked = 0

        def signed_in(self):
            Counting.asked += 1
            return True

    clock = Clock(STOP + timedelta(seconds=5))
    enricher = Enricher(cfg, graph=Counting(), clock=clock)
    for _ in range(10):
        enricher.holding(session)
        clock.now += timedelta(seconds=5)
    assert Counting.asked == 1


def test_historic_sessions_cost_no_keyring_lookups(cfg) -> None:
    spool = Spool(cfg)
    session = _detected_call(spool)

    class Counting(FakeGraph):
        asked = 0

        def signed_in(self):
            Counting.asked += 1
            return True

    enricher = Enricher(cfg, graph=Counting(), clock=Clock(STOP + timedelta(days=2)))
    enricher.holding(session)
    enricher.enrich(session)
    assert Counting.asked == 0


def test_two_sessions_the_calendar_named_alike_both_reach_the_folder(
    cfg, tmp_path: Path, two_track
) -> None:
    cfg = _exporting(cfg, tmp_path)
    spool = Spool(cfg)
    first = _detected_call(spool, audio=two_track)
    second = spool.create(title="Meeting 09:01", source="adhoc", now=START)
    spool.add_segment(second, 1, started_at=START)
    mic, app = segment_filenames(1)
    two_track(second.directory, seconds=2.0, mic_name=mic, app_name=app)
    spool.complete_segment(second, 1, stopped_at=STOP, duration_seconds=1800.0)
    second.transition("ending", by="munin-rec")
    second.transition("captured", by="munin-rec", stopped_at=STOP)
    clock = Clock(STOP + timedelta(seconds=10))
    graph = FakeGraph(events=[_event("Risk review", START)])  # the sweep refreshes first

    Worker(cfg, enricher=Enricher(cfg, graph=graph, clock=clock)).drain()

    names = sorted(p.name for p in (tmp_path / "uploads").glob("*.opus"))
    assert names == ["2026-09-23T0901 Risk review (2).opus", "2026-09-23T0901 Risk review.opus"]
    assert first.id != second.id
