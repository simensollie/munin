"""munin-work's half of M365 enrichment (spec 7.6): refresh, then name.

The recorder names a session from the local calendar copy at the moment it is
created (daemon.py ``_calendar_event``). Everything that needs the network, or
needs the call to be over, happens here, in the worker:

* keeping ``~/munin/m365/calendar.json`` fresh, every
  ``[m365] calendar_refresh_seconds``;
* a late calendar match, for a session created while that copy was stale;
* the direct-call lookup: after a detected Teams call with no event ends, the
  *call ended* message in its chat names the other party (m365.py).

A session's title is the only thing this ever changes, and only once: the
``enrichment`` record on the session says where the title came from and what it
was before, and a session that has one is never looked at again. The id and the
directory are never renamed -- they were fixed at capture, the export ledger is
keyed by the id, and a store that renames what it captured has nothing stable to
show an auditor (D26's reasoning).

Never raises into the sweep. Microsoft being unreachable costs a clock title,
never a transcript or an export.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from munin import m365
from munin.config import Config

__all__ = ["Enricher"]

log = logging.getLogger("munin.enrich")

#: How often one held session is looked up again, seconds. The message usually
#: lands within seconds of the call ending; this bounds the Graph traffic of a
#: session whose call never produces one.
RETRY_SECONDS = 60

#: How long one "signed in?" answer from the keyring is trusted, seconds.
SIGNED_IN_CACHE_SECONDS = 60

#: After Entra refuses the stored token, how long before asking the keyring
#: again -- which is how a fresh `munin m365 login` is picked up.
SIGNED_OUT_RECHECK_SECONDS = 600

#: States in which a session is finished capturing and not being cut up.
_SETTLED = frozenset({"captured", "pending", "transcribing", "done", "failed"})


def _now() -> datetime:
    return datetime.now().astimezone()


class Enricher:
    def __init__(
        self,
        config: Config,
        *,
        graph: "m365.Graph | None" = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.config = config
        self.settings = config.m365
        self._graph = graph
        self._clock = clock
        self._next_refresh: datetime | None = None
        self._last_try: dict[str, datetime] = {}
        self._last_error: str | None = None
        #: (answer, good until): the keyring's say on being signed in.
        self._signed_in: tuple[bool, datetime] | None = None

    # -- plumbing -------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def graph(self) -> "m365.Graph":
        if self._graph is None:
            self._graph = m365.Graph.from_settings(self.settings)
        return self._graph

    def _warn(self, what: str, exc: Exception) -> None:
        # Once per distinct message: a signed-out machine would otherwise log
        # the same line every five seconds for as long as nobody signs in.
        message = f"{what}: {exc}"
        if message != self._last_error:
            log.warning("%s", message)
            self._last_error = message

    # -- calendar ---------------------------------------------------------------

    def refresh_calendar(self, *, force: bool = False) -> bool:
        """Re-fetch today's calendar when due. True if the copy was rewritten.

        Never raises: munin-work has no unit to restart it, so a locked keyring,
        a dropped connection or a full disk costs one refresh, nothing more.
        """
        if not self.enabled:
            return False
        now = self._clock()
        if not force and self._next_refresh is not None and now < self._next_refresh:
            return False
        self._next_refresh = now + timedelta(seconds=self.settings.calendar_refresh_seconds)
        # Yesterday evening to tomorrow morning: covers a call that crosses
        # midnight and the late match below without holding a week of meetings.
        start = (now - timedelta(hours=12)).replace(minute=0, second=0, microsecond=0)
        end = now + timedelta(hours=12)
        try:
            events = self.graph().calendar(start, end)
            m365.write_calendar(self.config.home, events, fetched_at=now, window=(start, end))
        except m365.NotSignedIn as exc:
            self._signed_out(exc)
            return False
        except Exception as exc:  # noqa: BLE001 - see docstring
            self._warn("calendar refresh failed", exc)
            return False
        self._last_error = None
        return True

    def _cached_events(self) -> list[m365.CalendarEvent]:
        max_age = timedelta(seconds=max(900, 3 * self.settings.calendar_refresh_seconds))
        return m365.read_calendar(self.config.home, max_age=max_age, now=self._clock())

    # -- sign-in state ------------------------------------------------------------

    def _signed_out(self, exc: Exception) -> None:
        """The keyring holds a token Entra no longer accepts: treat as signed out.

        Checked again after :data:`SIGNED_OUT_RECHECK_SECONDS`, so a
        ``munin m365 login`` takes effect without restarting munin-work.
        """
        self._warn("not signed in", exc)
        self._signed_in = (False, self._clock() + timedelta(seconds=SIGNED_OUT_RECHECK_SECONDS))

    def _can_look_up(self) -> bool:
        """Signed in, as far as the keyring says -- asked at most once a minute.

        Every held session asks on every sweep, twice (export and enrich); a
        ``secret-tool`` per question is a subprocess storm, and a locked keyring
        turns each one into an unlock prompt.
        """
        now = self._clock()
        if self._signed_in is not None and now < self._signed_in[1]:
            return self._signed_in[0]
        try:
            answer = bool(self.graph().signed_in())
        except Exception as exc:  # noqa: BLE001 - a keyring hiccup is not a crash
            self._warn("keyring unavailable", exc)
            answer = False
        self._signed_in = (answer, now + timedelta(seconds=SIGNED_IN_CACHE_SECONDS))
        return answer

    # -- naming -----------------------------------------------------------------

    def _eligible(self, session: Any) -> bool:
        """Undecided, settled, and recent.

        Recent means capture stopped less than ``export_hold_seconds`` ago. A
        session older than that -- from before enrichment existed, or one that
        ended while munin-work was down -- is left exactly as it was: nothing
        renames last week's meetings the day [m365] is switched on.
        """
        if not (
            self.enabled
            and session.enrichment is None
            and session.calendar_event_id is None
            and session.state in _SETTLED
            and session.split_from is None
            and session.started_at is not None
            and session.stopped_at is not None
        ):
            return False
        waited = (self._clock() - session.stopped_at).total_seconds()
        return waited < self.settings.export_hold_seconds or session.id in self._last_try

    @staticmethod
    def _has_default_title(session: Any) -> bool:
        """The capture-time fallback, not a title somebody typed.

        ``munin start "Risk review"`` is the user naming the meeting, and
        nothing overrides that (contracts section 3); only the label-plus-clock
        and ``Meeting HH:MM`` fallbacks are open to a better name.
        """
        created = session.created_at or session.started_at
        clock = f"{created:%H:%M}"
        label = str((session.app or {}).get("label") or "").strip()
        defaults = {f"Meeting {clock}"}
        if label:
            defaults.add(f"{label} {clock}")
        return str(session.title).strip() in defaults

    def _is_direct_call_candidate(self, session: Any) -> bool:
        label = str((session.app or {}).get("label") or "")
        return (
            self.settings.direct_calls
            and session.source == "detected"
            and "teams" in label.casefold()
        )

    def holding(self, session: Any) -> bool:
        """True while export should wait for this session's name to be decided.

        Only a detected Teams call with a default title and no calendar event is
        held, only while its *call ended* message could still arrive, and never
        when the lookup cannot run -- a signed-out machine exports at once.
        """
        if not self._eligible(session) or not self._is_direct_call_candidate(session):
            return False
        if not self._has_default_title(session):
            return False
        waited = (self._clock() - session.stopped_at).total_seconds()
        if waited >= self.settings.export_hold_seconds:
            return False
        return self._can_look_up()

    def enrich(self, session: Any) -> bool:
        """Decide this session's title if it is still open. True if it changed."""
        try:
            if not self._eligible(session) or not self._has_default_title(session):
                return False
            return self._enrich(session)
        except Exception as exc:  # noqa: BLE001 - see module docstring
            self._warn(f"enrichment failed for {session.id}", exc)
            return False

    def _enrich(self, session: Any) -> bool:
        now = self._clock()
        event = m365.match_event(self._cached_events(), session.started_at)
        if event is not None:
            # Created while the calendar copy was stale; the event was there.
            return self._decide(session, "calendar", event.subject, calendar_event_id=event.id)

        if not self._is_direct_call_candidate(session):
            return False
        waited = (now - session.stopped_at).total_seconds()
        if waited >= self.settings.export_hold_seconds:
            # Looked, and the message never came: record that, so the session
            # is settled rather than re-read on every sweep. (_eligible only
            # lets an old session this far if it was looked up before.)
            self._decide(session, None, None, reason="no call-ended message in time")
            return False
        if not self._can_look_up():
            return False
        last = self._last_try.get(session.id)
        if last is not None and (now - last).total_seconds() < RETRY_SECONDS:
            return False
        self._last_try[session.id] = now

        graph = self.graph()
        try:
            messages = graph.recent_chat_messages(session.started_at)
            events = m365.call_ended_events(messages, graph.me())
        except m365.NotSignedIn as exc:
            self._signed_out(exc)
            return False
        except m365.M365Error as exc:
            self._warn("direct-call lookup failed", exc)
            return False
        name, decided = m365.direct_call_name(
            events, started_at=session.started_at, stopped_at=session.stopped_at
        )
        if not decided:
            return False
        if name is None:
            self._decide(session, None, None, reason="not a one-to-one call")
            return False
        return self._decide(session, "direct-call", name)

    def _decide(
        self,
        session: Any,
        source: str | None,
        title: str | None,
        *,
        calendar_event_id: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Write the decision onto the record as it is on disk *now*.

        The lookup above can take seconds, and the recorder may have resumed the
        session meanwhile (D15): writing this sweep's snapshot back would undo
        that. ``Session.merge`` re-reads under the lock and declines when the
        session is no longer the one that was looked at.
        """
        record: dict[str, Any] = {
            "title_from": source,
            "original_title": session.title if title else None,
            "decided_at": self._clock().isoformat(timespec="seconds"),
        }
        if reason:
            record["reason"] = reason
        fields: dict[str, Any] = {"enrichment": record}
        if title:
            fields["title"] = title
        if calendar_event_id:
            fields["calendar_event_id"] = calendar_event_id
        seen_title = session.title

        def moved(fresh: Any) -> bool:
            return (
                fresh.state not in _SETTLED
                or fresh.enrichment is not None
                or fresh.calendar_event_id is not None
                or fresh.title != seen_title
                or fresh.stopped_at != session.stopped_at
            )

        self._last_try.pop(session.id, None)
        if not session.merge(fields, unless=moved):
            log.info("session=%s changed while being named; looking again", session.id)
            return False
        if title:
            log.info("named session=%s from %s", session.id, source)
        return True
