"""Microsoft 365 enrichment (spec 7.6, M9): what a recording cannot say about itself.

Two questions, both about naming, and neither allowed to start anything (D4):

* **Which calendar event is this?** The worker keeps a small local copy of the
  day's calendar under ``~/munin/m365/calendar.json``; the recorder reads that
  file when a session is created and takes the overlapping event's subject as
  the title. The recorder never touches the network: it is one thread with one
  loop (daemon.py), and a Graph call inside ``handle_start`` would stall the
  socket for as long as Microsoft took to answer.
* **Who was on a direct call?** A call placed from a chat has no event. When it
  ends, Teams posts a *call ended* system message into that chat, naming the
  participants. The worker looks for it after capture. Exactly one other person
  names the session after them; a group keeps the clock fallback, because "one
  of five attendees" is the wrong name the window-title attempt produced
  (contracts amendment 2026-09-24, withdrawn).

Standard library only, like the rest of the runtime (pyproject.toml): the OAuth
device-code flow and three Graph GETs do not justify a dependency. The refresh
token goes to the desktop keyring through ``secret-tool`` (libsecret), never to
a file; there is deliberately no plaintext fallback. An access token lives in
memory for the life of one process.

Compliance (spec 12): this is a data flow the PoC did not have. A delegated
token for Calendars.Read and Chat.Read sits in the keyring, meeting subjects
(which name customers) are copied into ``calendar.json`` and ``session.json``,
and a direct call's other party is named in the session title. Only what naming
needs is kept: subject, times, event id, an attendee count. Never a body, never
an attendee list, never a message text.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "CHAT_SCOPE",
    "SCOPES",
    "scopes_for",
    "CalendarEvent",
    "CallEnded",
    "Graph",
    "Keyring",
    "M365Error",
    "NotSignedIn",
    "calendar_path",
    "call_ended_events",
    "direct_call_name",
    "match_event",
    "read_calendar",
    "write_calendar",
]

log = logging.getLogger("munin.m365")

#: Delegated scopes. ``offline_access`` is what returns a refresh token;
#: ``User.Read`` is how the worker tells you apart from the other participant.
SCOPES = ("offline_access", "User.Read", "Calendars.Read")

#: Only requested with ``[m365] direct_calls``. Kept apart because a tenant may
#: grant the calendar on user consent and hold Chat.Read for an admin: asking
#: for everything at once would block the calendar behind the chat approval.
CHAT_SCOPE = "Chat.Read"


def scopes_for(direct_calls: bool) -> tuple[str, ...]:
    return SCOPES + ((CHAT_SCOPE,) if direct_calls else ())

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: libsecret attributes. The account is tenant/client, so pointing the config at
#: another registration asks for a new sign-in instead of reusing a token that
#: was consented for something else.
KEYRING_SERVICE = "munin-m365"

#: A meeting you join a little early is still that meeting.
EARLY_JOIN = timedelta(minutes=10)

HTTP_TIMEOUT_SECONDS = 10


class M365Error(Exception):
    """Graph or the sign-in endpoint said no, or could not be reached."""


class NotSignedIn(M365Error):
    """No refresh token in the keyring, or it has been revoked or expired."""


# --------------------------------------------------------------------------
# Keyring
# --------------------------------------------------------------------------


class Keyring:
    """The refresh token, in libsecret via ``secret-tool``.

    A subprocess rather than a binding: the runtime imports nothing outside the
    standard library, and ``secret-tool`` ships with libsecret on every desktop
    that has a keyring at all.
    """

    def __init__(self, account: str, *, runner: Callable[..., Any] = subprocess.run) -> None:
        self.account = account
        self._run = runner

    @staticmethod
    def available() -> bool:
        return shutil.which("secret-tool") is not None

    def _attrs(self) -> list[str]:
        return ["service", KEYRING_SERVICE, "account", self.account]

    def get(self) -> str | None:
        result = self._run(
            ["secret-tool", "lookup", *self._attrs()],
            capture_output=True,
            text=True,
            timeout=10,
        )
        secret = (result.stdout or "").strip()
        return secret if result.returncode == 0 and secret else None

    def set(self, secret: str) -> None:
        result = self._run(
            ["secret-tool", "store", "--label=Munin Microsoft 365", *self._attrs()],
            input=secret,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise M365Error(
                "could not store the token in the keyring: "
                + ((result.stderr or "").strip() or f"exit {result.returncode}")
            )

    def clear(self) -> None:
        self._run(
            ["secret-tool", "clear", *self._attrs()],
            capture_output=True,
            text=True,
            timeout=10,
        )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

#: ``(method, url, headers, body) -> (status, parsed JSON)``. Injectable so the
#: tests never open a socket.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict]]


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as exc:
        raise M365Error(f"{urllib.parse.urlsplit(url).netloc} unreachable: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


class Graph:
    """Sign-in and the handful of Graph reads naming needs."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        *,
        keyring: Keyring | None = None,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.time,
        scopes: tuple[str, ...] = SCOPES,
    ) -> None:
        if not tenant_id or not client_id:
            raise M365Error("[m365] tenant_id and client_id must both be set")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.keyring = keyring or Keyring(f"{tenant_id}/{client_id}")
        self._transport = transport
        self._clock = clock
        self.scopes = tuple(scopes)
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._me: str | None = None

    @classmethod
    def from_settings(cls, settings: Any, **kwargs: Any) -> "Graph":
        """From an ``[m365]`` config section, asking only for what it uses."""
        return cls(
            settings.tenant_id,
            settings.client_id,
            scopes=scopes_for(bool(settings.direct_calls)),
            **kwargs,
        )

    # -- sign-in ------------------------------------------------------------

    def _form(self, endpoint: str, fields: dict[str, str]) -> tuple[int, dict]:
        url = f"{LOGIN}/{self.tenant_id}/oauth2/v2.0/{endpoint}"
        body = urllib.parse.urlencode(fields).encode("ascii")
        return self._transport(
            "POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, body
        )

    def start_device_login(self) -> dict:
        """Step one of the device-code flow: what to show the user."""
        status, payload = self._form(
            "devicecode", {"client_id": self.client_id, "scope": " ".join(self.scopes)}
        )
        if status != 200 or "device_code" not in payload:
            raise M365Error(_describe(payload, status))
        return payload

    def finish_device_login(
        self, flow: dict, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        """Poll until the user has signed in, then keep the refresh token."""
        interval = float(flow.get("interval", 5))
        deadline = self._clock() + float(flow.get("expires_in", 900))
        while self._clock() < deadline:
            sleep(interval)
            status, payload = self._form(
                "token",
                {
                    "grant_type": DEVICE_CODE_GRANT,
                    "client_id": self.client_id,
                    "device_code": flow["device_code"],
                },
            )
            if status == 200:
                self._accept(payload)
                return
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "expired_token":
                raise M365Error(
                    "the code expired before it was entered; run `munin m365 login` again"
                )
            if error == "authorization_declined":
                raise M365Error("sign-in was declined in the browser")
            raise M365Error(_describe(payload, status))
        raise M365Error("sign-in timed out before the code was entered")

    def _accept(self, payload: dict) -> None:
        token = payload.get("access_token")
        if not token:
            raise M365Error(_describe(payload, 200))
        self._access_token = token
        # A minute of slack, so a token never expires between check and use.
        self._expires_at = self._clock() + float(payload.get("expires_in", 3600)) - 60
        refresh = payload.get("refresh_token")
        if refresh:
            # Entra rotates refresh tokens; storing the new one each time is what
            # keeps a machine that is used every day signed in indefinitely.
            self.keyring.set(refresh)

    def signed_in(self) -> bool:
        return self.keyring.get() is not None

    def sign_out(self) -> None:
        self.keyring.clear()
        self._access_token = None
        self._me = None

    def token(self) -> str:
        if self._access_token and self._clock() < self._expires_at:
            return self._access_token
        refresh = self.keyring.get()
        if not refresh:
            raise NotSignedIn("not signed in to Microsoft 365; run `munin m365 login`")
        status, payload = self._form(
            "token",
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": refresh,
                "scope": " ".join(self.scopes),
            },
        )
        if status != 200:
            if payload.get("error") in ("invalid_grant", "interaction_required", "consent_required"):
                raise NotSignedIn(
                    "Microsoft 365 sign-in has expired; run `munin m365 login` "
                    f"({_describe(payload, status)})"
                )
            raise M365Error(_describe(payload, status))
        self._accept(payload)
        assert self._access_token is not None
        return self._access_token

    # -- reads ----------------------------------------------------------------

    def get(self, path_or_url: str, *, params: dict[str, str] | None = None) -> dict:
        url = path_or_url if path_or_url.startswith("https://") else GRAPH + path_or_url
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {
            "Authorization": f"Bearer {self.token()}",
            "Accept": "application/json",
            # Every time Graph returns comes back in UTC, whatever the mailbox
            # is set to, so there is exactly one way to parse them.
            "Prefer": 'outlook.timezone="UTC"',
        }
        status, payload = self._transport("GET", url, headers, None)
        if status != 200:
            raise M365Error(_describe(payload, status))
        return payload

    def pages(self, path: str, *, params: dict[str, str] | None = None, limit: int = 5) -> Iterable[dict]:
        payload = self.get(path, params=params)
        for _ in range(limit):
            yield from payload.get("value", [])
            link = payload.get("@odata.nextLink")
            if not link:
                return
            payload = self.get(link)

    def me(self) -> str:
        if self._me is None:
            self._me = str(self.get("/me", params={"$select": "id"}).get("id") or "")
        return self._me

    def calendar(self, start: datetime, end: datetime) -> list["CalendarEvent"]:
        rows = self.pages(
            "/me/calendarView",
            params={
                "startDateTime": _utc(start),
                "endDateTime": _utc(end),
                "$select": "id,subject,start,end,isAllDay,isCancelled,isOnlineMeeting,showAs,attendees",
                "$top": "100",
            },
        )
        events = []
        for row in rows:
            event = CalendarEvent.from_graph(row)
            if event is not None:
                events.append(event)
        return events

    def recent_chat_messages(self, since: datetime, *, max_chats: int = 25) -> list[dict]:
        """System messages in chats active since ``since``. Newest chats first.

        Graph has no "calls I was on" for a delegated user (call records need an
        application permission and tenant-admin consent), so the chat that the
        call was placed from is where the evidence is.
        """
        chats = self.get(
            "/me/chats",
            params={
                "$top": str(max_chats),
                "$orderby": "lastMessagePreview/createdDateTime desc",
                "$expand": "lastMessagePreview",
            },
        ).get("value", [])
        messages: list[dict] = []
        for chat in chats:
            preview = (chat.get("lastMessagePreview") or {}).get("createdDateTime")
            touched = _parse_time(preview) or _parse_time(chat.get("lastUpdatedDateTime"))
            if touched is not None and touched < since:
                break
            page = self.get(
                f"/me/chats/{urllib.parse.quote(chat['id'], safe='')}/messages",
                # Default order is lastModifiedDateTime desc: newest first.
                params={"$top": "20"},
            )
            for message in page.get("value", []):
                if message.get("messageType") == "systemEventMessage":
                    messages.append(message)
        return messages


def _describe(payload: dict, status: int) -> str:
    error = payload.get("error")
    if isinstance(error, dict):  # Graph shape
        return f"HTTP {status}: {error.get('code')}: {error.get('message')}"
    detail = payload.get("error_description") or ""
    # Entra appends a trace id and timestamp on new lines; the first line says it.
    detail = detail.splitlines()[0] if detail else ""
    return f"HTTP {status}: {error or 'error'}" + (f": {detail}" if detail else "")


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_FRACTION = re.compile(r"\.(\d+)")


def _parse_time(value: Any) -> datetime | None:
    """Graph's ``2026-09-23T09:00:00.0000000`` (UTC, by the Prefer header) or ISO."""
    if isinstance(value, dict):
        value = value.get("dateTime")
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Graph writes seven fractional digits; fromisoformat takes at most six.
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    subject: str
    start: datetime
    end: datetime
    online: bool
    attendees: int

    @classmethod
    def from_graph(cls, row: dict) -> "CalendarEvent | None":
        if row.get("isAllDay") or row.get("isCancelled"):
            return None
        # "free" and "workingElsewhere" are placeholders and focus blocks, not
        # meetings anyone could be recording.
        if row.get("showAs") in ("free", "workingElsewhere"):
            return None
        start, end = _parse_time(row.get("start")), _parse_time(row.get("end"))
        subject = (row.get("subject") or "").strip()
        if start is None or end is None or not subject or not row.get("id"):
            return None
        return cls(
            id=str(row["id"]),
            subject=subject,
            start=start,
            end=end,
            online=bool(row.get("isOnlineMeeting")),
            attendees=len(row.get("attendees") or []),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "subject": self.subject,
            "start": self.start.isoformat(timespec="seconds"),
            "end": self.end.isoformat(timespec="seconds"),
            "online": self.online,
            "attendees": self.attendees,
        }

    @classmethod
    def from_dict(cls, row: dict) -> "CalendarEvent | None":
        start, end = _parse_time(row.get("start")), _parse_time(row.get("end"))
        if start is None or end is None or not row.get("id") or not row.get("subject"):
            return None
        return cls(
            id=str(row["id"]),
            subject=str(row["subject"]),
            start=start,
            end=end,
            online=bool(row.get("online")),
            attendees=int(row.get("attendees") or 0),
        )


def match_event(events: Iterable[CalendarEvent], at: datetime) -> CalendarEvent | None:
    """The event a call starting at ``at`` belongs to, or ``None``.

    Overlap with a little early-join slack. When two events overlap -- the next
    meeting's slot opening while this one runs over is the common case -- an
    online meeting beats one that is not, and then the start nearest ``at``
    wins: a call beginning at 09:58 is the 10:00 meeting, not the 09:00 one it
    happens to overlap.
    """
    if at.tzinfo is None:
        at = at.astimezone()
    candidates = [e for e in events if e.start - EARLY_JOIN <= at < e.end]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda e: (not e.online, abs((e.start - at).total_seconds())),
    )


def calendar_path(home: Path) -> Path:
    """``<home>/m365/calendar.json``. Under the data root (D18), not a cache dir:
    it holds meeting subjects, and those are this user's data like any other.
    """
    return home / "m365" / "calendar.json"


def write_calendar(
    home: Path,
    events: list[CalendarEvent],
    *,
    fetched_at: datetime,
    window: tuple[datetime, datetime],
) -> Path:
    path = calendar_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "window": [window[0].isoformat(timespec="seconds"), window[1].isoformat(timespec="seconds")],
        "events": [e.to_dict() for e in events],
    }
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(path)
    return path


def read_calendar(home: Path, *, max_age: timedelta, now: datetime | None = None) -> list[CalendarEvent]:
    """The cached events, or ``[]`` if the copy is missing, unreadable or stale.

    Stale is empty on purpose: a calendar the worker stopped refreshing hours
    ago is exactly how a moved meeting ends up named after its old slot.
    """
    path = calendar_path(home)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    fetched = _parse_time(payload.get("fetched_at"))
    now = now or datetime.now().astimezone()
    if fetched is None or now - fetched > max_age:
        return []
    events = []
    for row in payload.get("events") or []:
        if isinstance(row, dict):
            try:
                event = CalendarEvent.from_dict(row)
            except (TypeError, ValueError):
                continue
            if event is not None:
                events.append(event)
    return events


# --------------------------------------------------------------------------
# Direct calls
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CallEnded:
    ended_at: datetime
    kind: str  # "call" | "meeting" | "screenShare"
    others: tuple[str, ...]  # display names, you excluded


def call_ended_events(messages: Iterable[dict], me: str) -> list[CallEnded]:
    """Every *call ended* event you took part in, with you taken out of the roster."""
    found = []
    for message in messages:
        detail = message.get("eventDetail") or {}
        if detail.get("@odata.type") != "#microsoft.graph.callEndedEventMessageDetail":
            continue
        ended = _parse_time(message.get("createdDateTime"))
        if ended is None:
            continue
        others = []
        present = False
        for row in detail.get("callParticipants") or []:
            user = ((row or {}).get("participant") or {}).get("user") or {}
            if not user:
                continue
            if user.get("id") == me:
                present = True
                continue
            others.append((user.get("displayName") or "").strip())
        if not present:
            # A call in some other chat, between other people -- or one you
            # never answered. Either way it is not evidence about your call.
            continue
        found.append(
            CallEnded(ended_at=ended, kind=str(detail.get("callEventType") or ""), others=tuple(others))
        )
    return found


#: How far a *call ended* message may sit from the moment capture stopped. The
#: recorder stops on a grace timer after the audio goes (default 120 s), or at
#: once if you press stop, so the message can come before or after.
CALL_END_TOLERANCE = timedelta(minutes=4)


def direct_call_name(
    events: Iterable[CallEnded], *, started_at: datetime, stopped_at: datetime
) -> tuple[str | None, bool]:
    """``(name, decided)`` for a session with no calendar event.

    ``decided`` is False while there is no evidence yet, so the caller can look
    again later. ``name`` is set only for exactly one other named participant;
    a group call, a nameless participant or two plausible calls all decide
    ``None``, which keeps the clock fallback. A wrong name is worse than none.
    """
    candidates = [
        e
        for e in events
        if e.kind in ("call", "")
        and started_at - CALL_END_TOLERANCE <= e.ended_at
        and abs((e.ended_at - stopped_at).total_seconds()) <= CALL_END_TOLERANCE.total_seconds()
    ]
    if not candidates:
        return None, False
    if len(candidates) > 1:
        return None, True
    others = candidates[0].others
    if len(others) == 1 and others[0]:
        return others[0], True
    return None, True
