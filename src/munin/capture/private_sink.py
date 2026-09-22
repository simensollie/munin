"""A private null sink per recording, so the app track is one process only.

Why this exists. Spec 6.1 wants the app track "bound to the specific sink-input
rather than the whole system output, so music and notification sounds stay out
of the mix". Binding one PipeWire stream node by ``object.serial`` does that,
and it works for an application that keeps one stream. Chromium does not:
measured on 2026-09-15 during a real meeting in the Electron Teams client, the
process held **five** identical ``Stream/Output/Audio`` nodes named "Playback"
at once, and the node seen at detection time was gone 12 s later. Binding by
stream therefore cannot work for a Chromium-based application, which is every
shape Teams takes on this machine.

The mechanism here inverts the question. Instead of following the application's
streams, give the recording a sink of its own and move the application's
streams onto it:

1. ``module-null-sink`` named ``munin-app-<token>`` -- a sink with no hardware
   behind it, whose monitor is the app track.
2. ``module-loopback`` from that monitor to whatever the default sink is, so
   the user still hears the meeting. It is deliberately *not* pinned to a sink:
   measured here, an unpinned loopback follows a default-sink change (its
   sink-input moved to the new default within 1.5 s and back again), which is
   what a user switching to headphones mid-meeting needs.
3. Every output stream of the target process is moved onto the private sink,
   and a watcher keeps moving the ones Chromium creates later.
4. At stop the streams go back where they came from, then the loopback is
   unloaded, then the sink. That order matters: unloading the sink first would
   leave the application playing into something that no longer reaches the
   speakers.

Measured on this machine with two ``pw-play`` tones, one moved onto the private
sink and one left on the default: the app track held the moved tone at
-21.1 dB and the other at -94.3 dB, a rejection of 73 dB. The whole-desktop
fallback holds both.

Two more measurements shape the code:

- ``pw-record --target <sink name>`` with ``stream.capture.sink = true`` records
  the private sink's monitor. ``--target <sink name>.monitor`` does **not**:
  ``.monitor`` is a PulseAudio-compatibility name, pw-record cannot resolve it,
  and it silently recorded the microphone instead (-51.5 dB of room, identical
  to a plain default-source capture). Use the sink name.
- With ``node.dont-reconnect = true``, unloading the sink mid-capture makes the
  recorder go to true digital silence (-inf dB) rather than the microphone.
  PipeWire also moves the orphaned streams back to the default sink by itself,
  so the worst case is a quiet track and audible audio, never a dead one.

*Counterargument, and it is a real one:* this reroutes live audio the user is
listening to and inserts a 30 ms loopback into the path. A bug here is
audible -- at worst the meeting goes silent in the user's ears. That is why
every step is individually recoverable, why the teardown order is fixed, why
stale modules from a killed daemon are cleaned up at startup, and why any
failure falls back to the old behaviour rather than failing the recording.

Owner: capture workstream.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "CommandResult",
    "PrivateSink",
    "PulseClient",
    "PulseModule",
    "SINK_PREFIX",
    "SinkInput",
    "list_clients_command",
    "list_modules_command",
    "list_sink_inputs_command",
    "list_sinks_command",
    "load_loopback_command",
    "load_null_sink_command",
    "move_sink_input_command",
    "owns_stream",
    "parse_clients",
    "parse_modules",
    "parse_sink_inputs",
    "parse_sinks",
    "restore_plan",
    "routing_plan",
    "sink_name_for",
    "stale_modules",
    "unload_module_command",
]

log = logging.getLogger("munin.capture")

#: Every sink this module creates is named with this prefix, and recovery finds
#: a previous life's leftovers by matching on it. Changing it orphans modules a
#: killed daemon left behind, so it is effectively frozen.
SINK_PREFIX = "munin-app-"

#: Loopback latency. 30 ms is inaudible as an echo and small enough that the
#: user's own voice through the meeting does not feel delayed; lower values
#: start to crackle on a busy machine.
DEFAULT_LATENCY_MSEC = 30

#: How long the loopback is left running on silence before any real audio is
#: put through it.
#:
#: A null sink has no clock of its own -- ``pw-top`` reports it with neither a
#: quantum nor a rate -- so the loopback drives the path and its adaptive
#: resampler has to converge on the rate of whatever device it is feeding.
#: Moving the application's streams in the same breath as loading the module
#: puts the meeting through that convergence, which is the crackle a user
#: hears for about a second after pressing record mid-call. Converging on
#: silence costs nothing and is over before the first sample of the meeting
#: arrives. The cost is up to this much meeting audio at the very start of the
#: recording, still going to the speakers rather than into the file.
SETTLE_SECONDS = 0.2

#: How often the watcher looks for streams the application created after the
#: recording started. Chromium does this mid-call. The daemon's health tick is
#: every 5 s, which would leave up to 5 s of a new stream going to the speakers
#: and not into the recording, so this runs on its own thread instead.
WATCH_INTERVAL_SECONDS = 1.5

#: Bounded parent walk. Chromium's audio process sits one hop below the window
#: process; two hops is slack, and an unbounded walk would sweep in siblings.
MAX_PARENT_HOPS = 2

#: pactl is not slow, but a hung one must not hang a recording.
COMMAND_TIMEOUT = 5.0

_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_-]+")


# --------------------------------------------------------------------------
# Command construction: pure, so the argument shape is a unit test.
# --------------------------------------------------------------------------


def sink_name_for(token: str) -> str:
    """The private sink's name for a session id or pid.

    PulseAudio sink names are matched literally all over this module (and by
    recovery, against a previous process's modules), so the token is reduced to
    characters that survive a round trip through ``pactl`` unquoted.
    """
    cleaned = _SAFE_TOKEN.sub("-", str(token)).strip("-")
    return f"{SINK_PREFIX}{cleaned or 'session'}"


def load_null_sink_command(
    name: str, *, description: str | None = None, pactl: str = "pactl"
) -> list[str]:
    """``pactl`` argv creating the private sink. Prints the module index."""
    argv = [pactl, "load-module", "module-null-sink", f"sink_name={name}"]
    if description:
        argv.append(f"sink_properties=device.description={_SAFE_TOKEN.sub('_', description)}")
    return argv


def load_loopback_command(
    name: str, *, latency_msec: int = DEFAULT_LATENCY_MSEC, pactl: str = "pactl"
) -> list[str]:
    """``pactl`` argv sending the private sink's monitor to the default sink.

    No ``sink=`` on purpose: an unpinned loopback follows the default sink, so
    switching to headphones mid-meeting keeps working. ``source_dont_move``
    pins the *other* end, because a loopback that wandered off our monitor
    would put the meeting into the speakers twice and the recording nowhere.
    """
    return [
        pactl,
        "load-module",
        "module-loopback",
        f"source={name}.monitor",
        f"latency_msec={int(latency_msec)}",
        "source_dont_move=true",
    ]


def unload_module_command(index: int | str, *, pactl: str = "pactl") -> list[str]:
    return [pactl, "unload-module", str(index)]


def move_sink_input_command(index: int | str, sink: int | str, *, pactl: str = "pactl") -> list[str]:
    return [pactl, "move-sink-input", str(index), str(sink)]


def list_modules_command(*, pactl: str = "pactl") -> list[str]:
    """Short form: JSON has no module listing, and the short form is stable."""
    return [pactl, "list", "modules", "short"]


def list_sink_inputs_command(*, pactl: str = "pactl") -> list[str]:
    return [pactl, "-f", "json", "list", "sink-inputs"]


def list_clients_command(*, pactl: str = "pactl") -> list[str]:
    return [pactl, "-f", "json", "list", "clients"]


def list_sinks_command(*, pactl: str = "pactl") -> list[str]:
    return [pactl, "-f", "json", "list", "sinks"]


def default_sink_command(*, pactl: str = "pactl") -> list[str]:
    return [pactl, "get-default-sink"]


# --------------------------------------------------------------------------
# What pactl says, parsed.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PulseClient:
    """One connected client. Process identity lives here, not on the stream."""

    index: int
    pid: int | None
    binary: str | None
    name: str | None


@dataclass(frozen=True)
class SinkInput:
    """One playback stream, and the sink it is currently going to."""

    index: int
    sink: int | None
    pid: int | None
    client: int | None
    app_name: str | None
    binary: str | None
    media_name: str | None


@dataclass(frozen=True)
class PulseModule:
    index: int
    name: str
    argument: str


@dataclass(frozen=True)
class PulseSink:
    index: int
    name: str


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_clients(payload: Any) -> dict[int, PulseClient]:
    """``pactl -f json list clients`` -> ``index -> PulseClient``.

    Two pid properties, and the difference matters. ``application.process.id``
    is what the client said about itself. ``pipewire.sec.pid`` is the pid of the
    socket peer, which for a PulseAudio client is the *pipewire-pulse daemon*,
    not the application (measured: Chrome's client reports
    ``application.process.id = 37022`` and ``pipewire.sec.pid = 1125``, the
    daemon). For a native PipeWire client such as ``pw-play`` both are the
    application. So the declared pid wins and the peer pid is the fallback.
    """
    clients: dict[int, PulseClient] = {}
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        index = _as_int(entry.get("index"))
        if index is None:
            continue
        props = entry.get("properties")
        props = props if isinstance(props, dict) else {}
        pid = _as_int(props.get("application.process.id"))
        if pid is None:
            pid = _as_int(props.get("pipewire.sec.pid"))
        clients[index] = PulseClient(
            index=index,
            pid=pid,
            binary=_as_str(props.get("application.process.binary")),
            name=_as_str(props.get("application.name")),
        )
    return clients


def parse_sink_inputs(
    payload: Any, clients: Mapping[int, PulseClient] | None = None
) -> list[SinkInput]:
    """``pactl -f json list sink-inputs`` -> the playback streams, with pids.

    ``index`` is the PulseAudio sink-input index, which on PipeWire is the
    node's ``object.serial`` -- the same token ``pw-record --target`` takes, and
    the token ``pactl move-sink-input`` takes. One number, three tools.
    """
    clients = clients or {}
    streams: list[SinkInput] = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        index = _as_int(entry.get("index"))
        if index is None:
            continue
        props = entry.get("properties")
        props = props if isinstance(props, dict) else {}
        client_index = _as_int(entry.get("client"))
        client = clients.get(client_index) if client_index is not None else None
        pid = _as_int(props.get("application.process.id"))
        if pid is None and client is not None:
            pid = client.pid
        streams.append(
            SinkInput(
                index=index,
                sink=_as_int(entry.get("sink")),
                pid=pid,
                client=client_index,
                app_name=_as_str(props.get("application.name"))
                or (client.name if client else None),
                binary=_as_str(props.get("application.process.binary"))
                or (client.binary if client else None),
                media_name=_as_str(props.get("media.name")),
            )
        )
    return streams


def parse_sinks(payload: Any) -> list[PulseSink]:
    sinks: list[PulseSink] = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        index = _as_int(entry.get("index"))
        name = _as_str(entry.get("name"))
        if index is None or name is None:
            continue
        sinks.append(PulseSink(index=index, name=name))
    return sinks


def parse_modules(text: str) -> list[PulseModule]:
    """``pactl list modules short`` -> loaded modules. Tab-separated, three fields."""
    modules: list[PulseModule] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        index = _as_int(parts[0])
        if index is None:
            continue
        name = parts[1].strip() if len(parts) > 1 else ""
        argument = parts[2].strip() if len(parts) > 2 else ""
        modules.append(PulseModule(index=index, name=name, argument=argument))
    return modules


# --------------------------------------------------------------------------
# Plans: pure functions over parsed state, so every decision is a unit test.
# --------------------------------------------------------------------------


def owns_stream(
    stream: SinkInput,
    pid: int,
    *,
    ppid_of: Callable[[int], int | None] | None = None,
    max_hops: int = MAX_PARENT_HOPS,
) -> bool:
    """Does ``pid`` own this stream, directly or as an ancestor?

    Direct is the common case: the PipeWire client of a Chromium application is
    created by its audio process, and detection already reports that pid. The
    parent walk covers the other direction -- a target named by the window
    process, whose audio child holds the stream.
    """
    if stream.pid is None or pid <= 1:
        # pid 1 is nobody's meeting, and it is every process's ancestor.
        return False
    if stream.pid == pid:
        return True
    if ppid_of is None:
        return False
    seen: set[int] = set()
    current: int | None = stream.pid
    for _ in range(max_hops):
        if current is None or current <= 1 or current in seen:
            return False
        seen.add(current)
        current = ppid_of(current)
        if current == pid:
            return True
    return False


def routing_plan(
    streams: Sequence[SinkInput],
    pid: int,
    *,
    sink_index: int | None,
    ppid_of: Callable[[int], int | None] | None = None,
    max_hops: int = MAX_PARENT_HOPS,
) -> list[SinkInput]:
    """The process's streams that are not already on the private sink."""
    return [
        stream
        for stream in streams
        if owns_stream(stream, pid, ppid_of=ppid_of, max_hops=max_hops)
        and (sink_index is None or stream.sink != sink_index)
    ]


def restore_plan(
    moved: Mapping[int, int | None],
    *,
    live_streams: Iterable[int],
    live_sinks: Iterable[int],
    default_sink: str | int | None,
) -> list[tuple[int, str | int]]:
    """Where each moved stream should go back to, as ``(stream, sink)`` pairs.

    Three cases, and the third is the one that bites: the sink a stream came
    from may be gone by the time the meeting ends (a headset unplugged
    mid-call). Sending it back to an index that no longer exists would fail and
    strand it, so it goes to the current default sink instead. A stream that
    itself is gone is dropped -- there is nothing to move.
    """
    sinks = set(live_sinks)
    alive = set(live_streams)
    plan: list[tuple[int, str | int]] = []
    for stream, previous in moved.items():
        if stream not in alive:
            continue
        if previous is not None and previous in sinks:
            plan.append((stream, previous))
        elif default_sink is not None:
            plan.append((stream, default_sink))
    return plan


def stale_modules(modules: Sequence[PulseModule], *, prefix: str = SINK_PREFIX) -> list[PulseModule]:
    """Modules a previous ``munin-rec`` left behind, in unload order.

    Loopbacks first, then the sinks they read from: unloading a sink out from
    under its loopback leaves a module reading a source that is not there.
    """
    loopbacks = [
        module
        for module in modules
        if module.name == "module-loopback" and f"source={prefix}" in module.argument
    ]
    sinks = [
        module
        for module in modules
        if module.name == "module-null-sink" and f"sink_name={prefix}" in module.argument
    ]
    return loopbacks + sinks


def stale_sink_names(modules: Sequence[PulseModule], *, prefix: str = SINK_PREFIX) -> list[str]:
    """Names of the leftover private sinks, for moving their streams off."""
    names: list[str] = []
    for module in modules:
        if module.name != "module-null-sink":
            continue
        match = re.search(rf"sink_name=({re.escape(prefix)}[A-Za-z0-9_-]*)", module.argument)
        if match:
            names.append(match.group(1))
    return names


# --------------------------------------------------------------------------
# Running the commands.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def detail(self) -> str:
        text = (self.stderr or self.stdout or "").strip()
        return text.splitlines()[-1][:200] if text else f"exited {self.returncode}"


Runner = Callable[[Sequence[str]], CommandResult]


def subprocess_runner(env: Mapping[str, str] | None = None) -> Runner:
    """The real runner: ``pactl``, with the PipeWire session environment.

    A missing ``pactl`` is reported as a failed command rather than raised: it
    is a reason to fall back to the old app-track modes, never a reason to lose
    a recording.
    """
    child_env = dict(env) if env is not None else dict(os.environ)

    def run(argv: Sequence[str]) -> CommandResult:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                list(argv),
                capture_output=True,
                timeout=COMMAND_TIMEOUT,
                env=child_env,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(127, "", f"{argv[0]} is not installed")
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"{argv[0]} timed out")
        except OSError as exc:
            return CommandResult(1, "", str(exc))
        return CommandResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    return run


def _read_ppid(pid: int) -> int | None:
    from munin.detect.linux import read_ppid  # local: pure /proc reader

    return read_ppid(pid)


class PrivateSink:
    """One recording's private sink, its loopback, and the streams on it.

    Every method tolerates failure and records it in :attr:`warnings`; nothing
    here may raise into a recording. :meth:`open` answering ``False`` is the
    caller's signal to fall back to an older app-track mode.
    """

    def __init__(
        self,
        token: str,
        pid: int,
        *,
        description: str | None = None,
        pactl: str = "pactl",
        runner: Runner | None = None,
        env: Mapping[str, str] | None = None,
        ppid_of: Callable[[int], int | None] | None = _read_ppid,
        latency_msec: int = DEFAULT_LATENCY_MSEC,
        watch: bool = True,
        watch_interval: float | None = None,
        settle_seconds: float = SETTLE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.name = sink_name_for(token)
        self.pid = pid
        self.description = description or "Munin meeting audio"
        self._pactl = pactl
        self._run: Runner = runner or subprocess_runner(env)
        self._ppid_of = ppid_of
        self._latency_msec = latency_msec
        self._settle_seconds = settle_seconds
        self._sleep = sleep
        self._watch = watch
        # Read at construction, not bound as a default, so a test can slow
        # the watcher down to never without reaching into the instance.
        self._watch_interval = (
            WATCH_INTERVAL_SECONDS if watch_interval is None else watch_interval
        )

        self.sink_module: int | None = None
        self.loopback_module: int | None = None
        self.sink_index: int | None = None
        #: stream index -> the sink it was on before we moved it.
        self.moved: dict[int, int | None] = {}
        #: How many individual moves were made, retries and re-moves included.
        self.moves = 0
        #: Streams of the process on the private sink as of the last route().
        self.last_routed = 0
        self.warnings: list[str] = []

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def open(self) -> bool:
        """Create the sink and the loopback, and route what is playing now.

        Returns ``False`` without leaving anything loaded if either module
        cannot be created, which is the caller's cue to fall back.
        """
        loaded = self._run(
            load_null_sink_command(self.name, description=self.description, pactl=self._pactl)
        )
        index = _as_int(loaded.stdout) if loaded.ok else None
        if not loaded.ok or index is None:
            self._warn(f"could not create the private sink: {loaded.detail}")
            return False
        self.sink_module = index

        loopback = self._run(
            load_loopback_command(
                self.name, latency_msec=self._latency_msec, pactl=self._pactl
            )
        )
        loop_index = _as_int(loopback.stdout) if loopback.ok else None
        if not loopback.ok or loop_index is None:
            # Without the loopback the user would not hear the meeting at all.
            # That is worse than a wider recording, so undo and fall back.
            self._warn(f"could not route the private sink to the speakers: {loopback.detail}")
            self._unload(self.sink_module, "private sink")
            self.sink_module = None
            return False
        self.loopback_module = loop_index

        # Let the loopback converge before the meeting goes through it, not
        # while it does. See SETTLE_SECONDS.
        if self._settle_seconds > 0:
            self._sleep(self._settle_seconds)

        self.sink_index = self._sink_index(self.name)
        self.route()
        self._start_watcher()
        log.info(
            "private sink %s is up (sink module %s, loopback module %s, %d stream(s))",
            self.name,
            self.sink_module,
            self.loopback_module,
            len(self.moved),
        )
        return True

    def route(self) -> int:
        """Move every output stream of the process onto the private sink.

        Called once at open and then by the watcher, because Chromium creates
        streams during a call. Returns how many streams were moved this time.
        """
        with self._lock:
            if self.sink_module is None:
                return 0
            streams = self._streams()
            if streams is None:
                return 0
            if self.sink_index is None:
                self.sink_index = self._sink_index(self.name)
            plan = routing_plan(
                streams, self.pid, sink_index=self.sink_index, ppid_of=self._ppid_of
            )
            moved = 0
            for stream in plan:
                result = self._run(
                    move_sink_input_command(stream.index, self.name, pactl=self._pactl)
                )
                if not result.ok:
                    self._warn(
                        f"could not move stream {stream.index} onto {self.name}: {result.detail}"
                    )
                    continue
                # Only remember the first sink we took it from: a stream we
                # re-moved after the application pulled it back belongs to
                # wherever it started, not to our own sink.
                self.moved.setdefault(stream.index, stream.sink)
                self.moves += 1
                moved += 1
            owned = [
                stream
                for stream in streams
                if owns_stream(stream, self.pid, ppid_of=self._ppid_of)
            ]
            self.last_routed = len(owned) - (len(plan) - moved)
            return moved

    def routed(self) -> int:
        """How many of the process's streams are on the private sink right now."""
        if self.sink_index is None:
            return 0
        streams = self._streams()
        if streams is None:
            return 0
        return sum(
            1
            for stream in streams
            if stream.sink == self.sink_index
            and owns_stream(stream, self.pid, ppid_of=self._ppid_of)
        )

    def streams_present(self) -> bool | None:
        """Does the process still hold any output stream?

        ``None`` when PipeWire cannot be asked, which must never be read as
        "the meeting ended". Unlike a bound node this does not go false when a
        single stream is recycled -- that is the whole point of the mode.
        """
        streams = self._streams()
        if streams is None:
            return None
        return any(owns_stream(s, self.pid, ppid_of=self._ppid_of) for s in streams)

    def close(self) -> list[str]:
        """Put the audio back and unload, in the only order that is safe.

        Streams first, then the loopback, then the sink. Unloading the sink
        while the application is still on it would work -- PipeWire moves
        orphans to the default sink, measured -- but only by luck, and the
        window where the meeting is audible to nobody is exactly the failure
        this must not have.
        """
        self._stop_watcher()
        with self._lock:
            self._restore()
            self._unload(self.loopback_module, "loopback")
            self.loopback_module = None
            self._unload(self.sink_module, "private sink")
            self.sink_module = None
            self.sink_index = None
            return list(self.warnings)

    def describe(self) -> dict[str, str]:
        return {
            "app_sink": self.name,
            "app_sink_streams": str(self.last_routed),
            "app_sink_moves": str(self.moves),
        }

    # -- plumbing ---------------------------------------------------------

    def _restore(self) -> None:
        if not self.moved:
            return
        streams = self._streams()
        sinks = self._sinks()
        default = self._default_sink()
        if streams is None:
            # We cannot see the graph. Ask for the default sink by name for
            # everything we moved: a move that fails is logged, and PipeWire
            # will rehome anything left when the sink goes.
            plan: list[tuple[int, str | int]] = (
                [(index, default) for index in self.moved] if default else []
            )
        else:
            plan = restore_plan(
                self.moved,
                live_streams=[s.index for s in streams],
                live_sinks=[s.index for s in sinks or []],
                default_sink=default,
            )
        for stream, sink in plan:
            result = self._run(move_sink_input_command(stream, sink, pactl=self._pactl))
            if not result.ok:
                self._warn(f"could not move stream {stream} back to {sink}: {result.detail}")
        self.moved = {}

    def _unload(self, index: int | None, what: str) -> None:
        if index is None:
            return
        result = self._run(unload_module_command(index, pactl=self._pactl))
        if not result.ok:
            self._warn(f"could not unload the {what} (module {index}): {result.detail}")

    def _streams(self) -> list[SinkInput] | None:
        inputs = self._run(list_sink_inputs_command(pactl=self._pactl))
        if not inputs.ok:
            return None
        clients = self._run(list_clients_command(pactl=self._pactl))
        payload = _load_json(inputs.stdout)
        if payload is None:
            return None
        return parse_sink_inputs(
            payload, parse_clients(_load_json(clients.stdout) or []) if clients.ok else {}
        )

    def _sinks(self) -> list[PulseSink] | None:
        result = self._run(list_sinks_command(pactl=self._pactl))
        if not result.ok:
            return None
        payload = _load_json(result.stdout)
        return parse_sinks(payload) if payload is not None else None

    def _sink_index(self, name: str) -> int | None:
        for sink in self._sinks() or []:
            if sink.name == name:
                return sink.index
        return None

    def _default_sink(self) -> str | None:
        result = self._run(default_sink_command(pactl=self._pactl))
        if not result.ok:
            return None
        name = result.stdout.strip()
        return name or None

    def _warn(self, message: str) -> None:
        log.warning("%s", message)
        if message not in self.warnings:
            self.warnings.append(message)

    def _start_watcher(self) -> None:
        if not self._watch or self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._watch_loop, name="munin-sink-watch", daemon=True
        )
        self._thread = thread
        thread.start()

    def _stop_watcher(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=COMMAND_TIMEOUT + self._watch_interval)

    def _watch_loop(self) -> None:
        while not self._stop.wait(self._watch_interval):
            try:
                self.route()
            except Exception as exc:  # noqa: BLE001 - a watcher must not die
                log.warning("private sink watcher error=%s", exc)


def _load_json(text: str) -> Any | None:
    try:
        return json.loads(text or "null")
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# Crash recovery.
# --------------------------------------------------------------------------


def recover(
    *,
    pactl: str = "pactl",
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Unload private sinks a previous ``munin-rec`` left behind.

    A daemon that was SIGKILLed mid-meeting never ran :meth:`PrivateSink.close`,
    so the user is left listening to the meeting through a loopback nobody owns
    -- or, if the loopback died with its session, not listening to it at all.
    This runs at daemon startup: any stream still sitting on a leftover
    ``munin-app-*`` sink is moved back to the default sink first, then the
    loopbacks are unloaded, then the sinks.

    Returns one line per thing it did, for the log. Never raises.
    """
    run = runner or subprocess_runner(env)
    listed = run(list_modules_command(pactl=pactl))
    if not listed.ok:
        return []
    modules = parse_modules(listed.stdout)
    stale = stale_modules(modules)
    if not stale:
        return []

    notes: list[str] = []
    names = set(stale_sink_names(modules))
    if names:
        sinks_result = run(list_sinks_command(pactl=pactl))
        inputs_result = run(list_sink_inputs_command(pactl=pactl))
        default = run(default_sink_command(pactl=pactl))
        sinks = parse_sinks(_load_json(sinks_result.stdout) or []) if sinks_result.ok else []
        stranded = {sink.index for sink in sinks if sink.name in names}
        target = default.stdout.strip() if default.ok else ""
        if stranded and target and inputs_result.ok:
            for stream in parse_sink_inputs(_load_json(inputs_result.stdout) or []):
                if stream.sink in stranded:
                    moved = run(move_sink_input_command(stream.index, target, pactl=pactl))
                    notes.append(
                        f"moved stranded stream {stream.index} back to {target}"
                        if moved.ok
                        else f"could not rescue stranded stream {stream.index}: {moved.detail}"
                    )

    for module in stale:
        result = run(unload_module_command(module.index, pactl=pactl))
        notes.append(
            f"unloaded a leftover {module.name} (module {module.index})"
            if result.ok
            else f"could not unload leftover module {module.index}: {result.detail}"
        )
    for note in notes:
        log.info("capture recovery: %s", note)
    return notes
