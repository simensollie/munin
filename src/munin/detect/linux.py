"""PipeWire + Hyprland detection evidence (spec 6.3, 16.3).

Verified on this machine: process identity lives on the PipeWire *Client*
object, not on the stream node. Build ``client.id -> (pid, binary,
application.name)`` from the Client objects in ``pw-dump``, then join every node
whose ``media.class`` starts with ``Stream/`` to its client. A pid holding both
``Stream/Output/Audio`` and ``Stream/Input/Audio`` satisfies condition 1.

The window owning an audio stream is often a parent of the process that holds it
(Chrome's audio process sits one hop below its window), so the pid walk through
``/proc/<pid>/status`` is bounded rather than assumed to be exactly one hop.

Everything that reasons about a dump is a pure function over already-parsed
JSON, so the detection matrix of spec 13 is tested against committed fixtures
with no PipeWire, no Hyprland and no audio hardware in sight. The detector class
is a thin shell that runs the two commands and hands their output to those
functions.

Owner: capture workstream.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Literal, Mapping, Sequence

from munin.detect.base import AppRule, CallEvidence, Detector, WindowInfo

__all__ = [
    "CallEvent",
    "CallWatcher",
    "MAX_PARENT_HOPS",
    "PipewireDetector",
    "PwClient",
    "default_source_name",
    "hyprland_signature",
    "parse_clients",
    "parse_evidence",
    "parse_windows",
    "read_ppid",
    "window_for_pid",
]

log = logging.getLogger("munin.detect")

#: How far up ``/proc/<pid>/status`` PPid to look for the window owning a stream.
MAX_PARENT_HOPS = 5

#: The two PipeWire ``media.class`` values that make up condition 1.
PLAYBACK_CLASS = "Stream/Output/Audio"
CAPTURE_CLASS = "Stream/Input/Audio"

#: The shell's own audio tap. It is not an application and must never look like
#: a call; the first-party Omarchy audio panel filters the same node by name.
IGNORED_NODE_NAMES = frozenset({"quickshell"})

_PW_DUMP_TIMEOUT = 5.0
_HYPRCTL_TIMEOUT = 3.0


# --------------------------------------------------------------------------
# Pure parsing: everything below operates on already-decoded JSON.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PwClient:
    """One ``PipeWire:Interface:Client``, reduced to the identity fields."""

    id: int
    pid: int | None = None
    binary: str | None = None
    name: str | None = None


def _props(obj: Mapping[str, Any]) -> dict[str, Any]:
    """Property bag of a ``pw-dump`` object.

    Nodes and Clients carry theirs under ``info.props``; Metadata objects carry
    theirs under a top-level ``props``. Prefer the first and fall back, so one
    accessor serves every object type.
    """
    info = obj.get("info")
    if isinstance(info, Mapping):
        props = info.get("props")
        if isinstance(props, Mapping):
            return dict(props)
    props = obj.get("props")
    return dict(props) if isinstance(props, Mapping) else {}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_clients(dump: Iterable[Mapping[str, Any]]) -> dict[int, PwClient]:
    """Map ``client.id -> PwClient`` from a decoded ``pw-dump``.

    Process identity lives here and nowhere else for the applications that
    matter: a Chrome stream node carries ``client.id`` and no pid at all.
    """
    clients: dict[int, PwClient] = {}
    for obj in dump:
        if not str(obj.get("type", "")).endswith(":Client"):
            continue
        cid = _as_int(obj.get("id"))
        if cid is None:
            continue
        props = _props(obj)
        clients[cid] = PwClient(
            id=cid,
            pid=_as_int(props.get("application.process.id")),
            binary=_as_str(props.get("application.process.binary")),
            name=_as_str(props.get("application.name")),
        )
    return clients


def parse_evidence(dump: Iterable[Mapping[str, Any]]) -> list[CallEvidence]:
    """Condition-1 evidence, one entry per process holding any audio stream.

    A process appears once however many streams it holds. ``playback_handle``
    is the ``object.serial`` of its output stream as a string -- the opaque
    token the capturer hands to ``pw-record --target``. When a process holds
    several output streams (a browser with two tabs making noise) the highest
    serial wins: serials only increase, so that is the most recently created
    stream and, for a call that just started, the right one.
    """
    dump = list(dump)
    clients = parse_clients(dump)

    playback: dict[int, bool] = {}
    capture: dict[int, bool] = {}
    handles: dict[int, tuple[int, str]] = {}  # pid -> (serial, handle)
    names: dict[int, str | None] = {}
    binaries: dict[int, str | None] = {}
    order: list[int] = []

    for obj in dump:
        if not str(obj.get("type", "")).endswith(":Node"):
            continue
        props = _props(obj)
        media_class = str(props.get("media.class", ""))
        if media_class not in (PLAYBACK_CLASS, CAPTURE_CLASS):
            continue
        if _as_str(props.get("node.name")) in IGNORED_NODE_NAMES:
            continue

        client_id = _as_int(props.get("client.id"))
        client = clients.get(client_id) if client_id is not None else None
        pid = (client.pid if client else None) or _as_int(props.get("application.process.id"))
        if pid is None:
            continue

        if pid not in playback:
            order.append(pid)
            playback[pid] = False
            capture[pid] = False
        # Client props win; the node's own copy is a fallback some apps set.
        names.setdefault(
            pid,
            (client.name if client else None) or _as_str(props.get("application.name")),
        )
        binaries.setdefault(
            pid,
            (client.binary if client else None)
            or _as_str(props.get("application.process.binary")),
        )

        if media_class == CAPTURE_CLASS:
            capture[pid] = True
            continue

        playback[pid] = True
        serial = _as_int(props.get("object.serial"))
        if serial is None:
            serial = _as_int(props.get("object.id"))
        if serial is None:
            continue
        current = handles.get(pid)
        if current is None or serial > current[0]:
            handles[pid] = (serial, str(serial))

    return [
        CallEvidence(
            pid=pid,
            has_playback=playback[pid],
            has_capture=capture[pid],
            playback_handle=handles[pid][1] if pid in handles else None,
            client_name=names.get(pid),
            binary=binaries.get(pid),
        )
        for pid in order
    ]


def parse_windows(clients: Iterable[Mapping[str, Any]]) -> list[WindowInfo]:
    """Turn ``hyprctl clients -j`` into :class:`WindowInfo` rows."""
    windows: list[WindowInfo] = []
    for entry in clients:
        pid = _as_int(entry.get("pid"))
        if pid is None or pid <= 0:
            continue
        windows.append(
            WindowInfo(
                pid=pid,
                window_class=_as_str(entry.get("class")) or _as_str(entry.get("initialClass")),
                title=_as_str(entry.get("title")),
            )
        )
    return windows


def read_ppid(pid: int, *, proc: Path | str = "/proc") -> int | None:
    """Parent of ``pid`` from ``/proc/<pid>/status``, or ``None`` if unknowable."""
    try:
        text = (Path(proc) / str(pid) / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("PPid:"):
            return _as_int(line.split(":", 1)[1].strip())
    return None


def window_for_pid(
    windows: Sequence[WindowInfo],
    pid: int,
    ppid_of: Callable[[int], int | None] = read_ppid,
    *,
    max_hops: int = MAX_PARENT_HOPS,
) -> WindowInfo | None:
    """The window owning ``pid``, walking up to ``max_hops`` parents.

    Chrome's audio process sits one hop below the window that owns the tab, and
    only that one hop was observed on this machine -- so the walk is bounded
    rather than fixed at one, and stops at pid 1 or on a cycle.
    """
    by_pid = {w.pid: w for w in windows}
    seen: set[int] = set()
    current: int | None = pid
    for hop in range(max_hops + 1):
        if current is None or current <= 1 or current in seen:
            return None
        seen.add(current)
        window = by_pid.get(current)
        if window is not None:
            return window
        if hop == max_hops:
            return None
        current = ppid_of(current)
    return None


def default_source_name(dump: Iterable[Mapping[str, Any]]) -> str | None:
    """``node.name`` of the default audio source, from the ``default`` metadata.

    ``pw-cli info @DEFAULT_SOURCE@`` does not work on this machine (the scouts
    checked), so the metadata object is the supported way to ask.
    """
    for obj in dump:
        if not str(obj.get("type", "")).endswith(":Metadata"):
            continue
        if _props(obj).get("metadata.name") != "default":
            continue
        for row in obj.get("metadata") or []:
            if row.get("key") != "default.audio.source":
                continue
            value = row.get("value")
            if isinstance(value, Mapping):
                return _as_str(value.get("name"))
            return _as_str(value)
    return None


# --------------------------------------------------------------------------
# The live detector.
# --------------------------------------------------------------------------


def hyprland_signature(runtime_dir: str | os.PathLike[str] | None = None) -> str | None:
    """``HYPRLAND_INSTANCE_SIGNATURE``, from the environment or the runtime dir.

    ``hyprctl`` refuses to run without it, and it is unset in anything that did
    not inherit the graphical session. There is normally exactly one instance
    directory under ``$XDG_RUNTIME_DIR/hypr/``; more than one is ambiguous and
    we decline to guess, degrading to window-free detection instead.
    """
    from_env = _as_str(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
    if from_env:
        return from_env
    base = runtime_dir or os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        return None
    try:
        entries = sorted(p.name for p in Path(base, "hypr").iterdir() if p.is_dir())
    except OSError:
        return None
    return entries[0] if len(entries) == 1 else None


class PipewireDetector(Detector):
    """Evidence from ``pw-dump``, window facts from ``hyprctl clients -j``.

    Never raises for a missing tool or an unreachable compositor: evidence
    degrades to an empty list and windows degrade to ``None`` (PipeWire-only
    matching), with the reason kept for :meth:`describe` so ``munin doctor``
    can say which half is broken.
    """

    platform: ClassVar[str] = "linux"

    def __init__(
        self,
        rules: Sequence[AppRule],
        *,
        pw_dump: str = "pw-dump",
        hyprctl: str = "hyprctl",
    ) -> None:
        super().__init__(rules)
        self._pw_dump = pw_dump
        self._hyprctl = hyprctl
        self._pw_error: str | None = None
        self._wm_error: str | None = None
        self._windows_cached: list[WindowInfo] | None = None

    # -- evidence ---------------------------------------------------------

    def evidence(self) -> list[CallEvidence]:
        # A fresh scan must not reuse the window list of the previous one.
        self._windows_cached = None
        dump = self._run_json([self._pw_dump], _PW_DUMP_TIMEOUT, "pw")
        if not isinstance(dump, list):
            return []
        self._pw_error = None
        return parse_evidence(dump)

    def window_for_pid(self, pid: int) -> WindowInfo | None:
        windows = self._windows()
        if windows is None:
            return None
        return window_for_pid(windows, pid)

    def describe(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "pw_dump": shutil.which(self._pw_dump) or f"{self._pw_dump} (not found)",
            "hyprctl": shutil.which(self._hyprctl) or f"{self._hyprctl} (not found)",
            "runtime_dir": os.environ.get("XDG_RUNTIME_DIR", "") or "(unset)",
            "hyprland_signature": hyprland_signature() or "(unresolved)",
            "rules": str(len(self.rules)),
            "pw_error": self._pw_error or "",
            "wm_error": self._wm_error or "",
        }

    # -- plumbing ---------------------------------------------------------

    def _windows(self) -> list[WindowInfo] | None:
        """Window list for this scan, fetched at most once per :meth:`evidence`."""
        if self._windows_cached is not None:
            return self._windows_cached
        signature = hyprland_signature()
        if not signature:
            self._wm_error = "HYPRLAND_INSTANCE_SIGNATURE is unset and could not be resolved"
            return None
        payload = self._run_json(
            [self._hyprctl, "clients", "-j"],
            _HYPRCTL_TIMEOUT,
            "wm",
            extra_env={"HYPRLAND_INSTANCE_SIGNATURE": signature},
        )
        if not isinstance(payload, list):
            return None
        self._wm_error = None
        self._windows_cached = parse_windows(payload)
        return self._windows_cached

    def _run_json(
        self,
        argv: Sequence[str],
        timeout: float,
        which: Literal["pw", "wm"],
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> Any:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except FileNotFoundError:
            self._record(which, f"{argv[0]} is not installed")
            return None
        except subprocess.TimeoutExpired:
            self._record(which, f"{argv[0]} timed out after {timeout:g} s")
            return None
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            self._record(
                which,
                f"{argv[0]} exited {proc.returncode}: {tail[-1] if tail else ''}".strip(),
            )
            return None
        try:
            return json.loads(proc.stdout.decode("utf-8", "replace") or "null")
        except json.JSONDecodeError as exc:
            self._record(which, f"{argv[0]} produced unparseable JSON: {exc}")
            return None

    def _record(self, which: Literal["pw", "wm"], message: str) -> None:
        log.warning("detection: %s", message)
        if which == "pw":
            self._pw_error = message
        else:
            self._wm_error = message


# --------------------------------------------------------------------------
# The poll loop the daemon runs when detection.source = "daemon".
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CallEvent:
    """A change between two scans, shaped like ``munin event`` (contracts 7).

    ``call-ended`` carries the identity the call had while it was live, because
    by the time it ends there is nothing left to identify.
    """

    event: Literal["call-started", "call-ended"]
    pid: int
    app_id: str | None
    label: str | None
    title: str | None
    handle: str | None
    at: datetime


class CallWatcher:
    """Turns successive :meth:`Detector.scan` results into start/end events.

    The daemon owns the state machine and every timer; this only says what
    changed. It is edge-triggered on the pid, so a call that survives a scan
    produces no event, and a browser that keeps the tab open after hanging up
    produces exactly one ``call-ended``.

    ``identified_only`` is the D4 guard: an unidentified live call is real, but
    Munin was not asked to care about it, so it stays visible to ``munin
    doctor`` (through ``scan()``) and never prompts.
    """

    def __init__(self, detector: Detector, *, identified_only: bool = True) -> None:
        self._detector = detector
        self._identified_only = identified_only
        self._live: dict[int, CallEvent] = {}

    @property
    def live_pids(self) -> frozenset[int]:
        """Pids that were live at the last :meth:`poll`."""
        return frozenset(self._live)

    def poll(self, *, now: datetime | None = None) -> list[CallEvent]:
        """One scan, diffed against the previous one. Never raises."""
        try:
            calls = self._detector.scan(now=now)
        except Exception as exc:  # a broken detector must not kill the daemon
            log.warning("detection scan failed: %s", exc)
            return []

        current: dict[int, CallEvent] = {}
        for call in calls:
            if self._identified_only and call.identity is None:
                continue
            current[call.evidence.pid] = CallEvent(
                event="call-started",
                pid=call.evidence.pid,
                app_id=call.identity.app_id if call.identity else None,
                label=call.identity.label if call.identity else None,
                title=call.window.title if call.window else None,
                handle=call.evidence.playback_handle,
                at=call.observed_at,
            )

        at = (
            next(iter(current.values())).at
            if current
            else (now or datetime.now().astimezone())
        )
        events = [event for pid, event in current.items() if pid not in self._live]
        for pid, previous in self._live.items():
            if pid not in current:
                events.append(
                    CallEvent(
                        event="call-ended",
                        pid=pid,
                        app_id=previous.app_id,
                        label=previous.label,
                        title=previous.title,
                        handle=previous.handle,
                        at=at,
                    )
                )
        self._live = current
        return events

    def run(
        self,
        interval: float,
        handler: Callable[[CallEvent], None],
        *,
        stop: threading.Event | None = None,
        max_polls: int | None = None,
    ) -> None:
        """Poll every ``interval`` seconds until ``stop`` is set.

        Blocking, so the daemon runs it on its own thread. ``max_polls`` exists
        so a test can run the loop a fixed number of times instead of racing a
        timer.
        """
        polls = 0
        while True:
            if stop is not None and stop.is_set():
                return
            for event in self.poll():
                try:
                    handler(event)
                except Exception as exc:  # one bad handler call is not fatal
                    log.warning("detection handler failed for %s: %s", event.event, exc)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return
            if stop is not None:
                if stop.wait(interval):
                    return
            else:
                time.sleep(interval)
