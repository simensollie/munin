"""The private sink: command shape, routing plans, teardown order, recovery.

Deterministic and offline. ``pactl`` is a :class:`FakeRunner` that keeps a
small model of the audio graph -- sink-inputs, their sinks, the loaded modules
-- and applies the commands to it, so a move that a test asserts on is a move
the model actually made. That is what lets the interesting assertions be about
*order* and *end state* rather than about argv alone: the audio has to be back
where it started before the sink is unloaded, and no test passes by unloading
things in a sequence that would leave the user unable to hear a meeting.

The fixtures are the shapes measured on this machine on 2026-09-15 with every
identity replaced by a synthetic one (see ``fixtures/README.md``).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from munin.capture import private_sink
from munin.capture.base import CaptureTarget
from munin.capture.linux import PipewireCapturer
from munin.capture.private_sink import (
    SINK_PREFIX,
    CommandResult,
    PrivateSink,
    SinkInput,
    list_clients_command,
    list_modules_command,
    list_sink_inputs_command,
    load_loopback_command,
    SETTLE_SECONDS,
    load_null_sink_command,
    move_sink_input_command,
    owns_stream,
    parse_clients,
    parse_modules,
    parse_sink_inputs,
    parse_sinks,
    recover,
    restore_plan,
    routing_plan,
    sink_name_for,
    stale_modules,
    stale_sink_names,
    unload_module_command,
)

from .test_capture_linux import SpawnRecorder, load_fixture

FIXTURES = Path(__file__).parent / "fixtures"

#: The Chromium audio process in the fixtures, and the window process above it
#: (``ppid-map.json``: 4310 -> 4300).
CHROMIUM_PID = 4310
CHROMIUM_WINDOW_PID = 4300
#: The two native ``pw-play`` players, and the PulseAudio one.
PLAYER_A_PID = 9101
PLAYER_B_PID = 9102
PULSE_PLAYER_PID = 9107

DEFAULT_SINK = "alsa_output.pci-0000_00_1f.3.analog-stereo"
DEFAULT_SINK_INDEX = 57


def load(name: str):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


def ppid_of(pid: int) -> int | None:
    table = {int(k): int(v) for k, v in load("ppid-map.json").items()}
    return table.get(pid)


# --------------------------------------------------------------------------
# Command shape.
# --------------------------------------------------------------------------


def test_the_sink_name_is_prefixed_and_safe():
    assert sink_name_for(4310) == "munin-app-4310"
    assert sink_name_for("2026-09-15T1030-weekly quality sync").startswith(SINK_PREFIX)
    assert " " not in sink_name_for("weekly quality sync")
    assert sink_name_for("") == "munin-app-session"


def test_the_null_sink_command_names_the_sink_and_describes_it():
    argv = load_null_sink_command("munin-app-4310", description="Munin meeting audio")
    assert argv[:3] == ["pactl", "load-module", "module-null-sink"]
    assert "sink_name=munin-app-4310" in argv
    assert "sink_properties=device.description=Munin_meeting_audio" in argv


def test_the_loopback_is_not_pinned_to_a_sink():
    """Measured: an unpinned loopback follows a default-sink change."""
    argv = load_loopback_command("munin-app-4310", latency_msec=30)
    assert "source=munin-app-4310.monitor" in argv
    assert "latency_msec=30" in argv
    assert "source_dont_move=true" in argv
    assert not any(a.startswith("sink=") for a in argv)


def test_the_remaining_commands_are_the_documented_ones():
    assert unload_module_command(536870916) == ["pactl", "unload-module", "536870916"]
    assert move_sink_input_command(9301, "munin-app-4310") == [
        "pactl", "move-sink-input", "9301", "munin-app-4310",
    ]
    assert list_modules_command() == ["pactl", "list", "modules", "short"]
    assert list_sink_inputs_command() == ["pactl", "-f", "json", "list", "sink-inputs"]
    assert list_clients_command() == ["pactl", "-f", "json", "list", "clients"]


# --------------------------------------------------------------------------
# Reading the graph.
# --------------------------------------------------------------------------


def test_a_native_client_publishes_its_pid_on_the_client_not_the_stream():
    """Measured: ``pw-play``'s sink-input carries no ``application.process.id``."""
    raw = load("pactl-sink-inputs-two-players.json")
    assert all(
        "application.process.id" not in entry["properties"]
        for entry in raw
        if entry["properties"]["application.name"] == "pw-play"
    )
    clients = parse_clients(load("pactl-clients-two-players.json"))
    streams = {s.index: s for s in parse_sink_inputs(raw, clients)}
    assert streams[7782].pid == PLAYER_A_PID     # only reachable through the client
    assert streams[7781].pid == PLAYER_B_PID
    assert streams[7783].pid == PULSE_PLAYER_PID  # this one said so itself


def test_the_declared_pid_wins_over_the_socket_peer():
    """``pipewire.sec.pid`` is the pulse daemon for a PulseAudio client."""
    clients = parse_clients(load("pactl-clients-chromium.json"))
    assert clients[7077].pid == CHROMIUM_PID      # not 1125, the daemon
    assert clients[7077].binary == "teams-for-linux"


def test_a_client_with_only_a_peer_pid_still_resolves():
    clients = parse_clients([{"index": 5, "properties": {"pipewire.sec.pid": "4242"}}])
    assert clients[5].pid == 4242


def test_parse_modules_reads_the_short_form():
    modules = parse_modules(load("pactl-modules-stale.txt"))
    assert [m.index for m in modules] == [10, 11, 536870916, 536870917, 536870918]
    assert modules[2].name == "module-null-sink"
    assert "sink_name=munin-app-4310" in modules[2].argument


def test_parse_sinks_maps_names_to_indexes():
    sinks = {s.name: s.index for s in parse_sinks(load("pactl-sinks.json"))}
    assert sinks["munin-app-4310"] == 84
    assert sinks[DEFAULT_SINK] == DEFAULT_SINK_INDEX


# --------------------------------------------------------------------------
# The routing plan.
# --------------------------------------------------------------------------


@pytest.fixture()
def chromium_streams() -> list[SinkInput]:
    return parse_sink_inputs(
        load("pactl-sink-inputs-chromium.json"),
        parse_clients(load("pactl-clients-chromium.json")),
    )


def test_every_playback_stream_of_the_process_is_planned(chromium_streams):
    """Five identical "Playback" nodes at once is the measured Chromium shape."""
    plan = routing_plan(chromium_streams, CHROMIUM_PID, sink_index=84)
    assert [s.index for s in plan] == [9301, 9302, 9303, 9304, 9305]
    assert all(s.app_name == "Chromium" for s in plan)


def test_another_application_is_left_alone(chromium_streams):
    plan = routing_plan(chromium_streams, CHROMIUM_PID, sink_index=84)
    assert 9310 not in [s.index for s in plan], "the music player stays on the speakers"


def test_streams_already_on_the_private_sink_are_not_moved_again(chromium_streams):
    settled = [
        SinkInput(**{**s.__dict__, "sink": 84}) if s.pid == CHROMIUM_PID else s
        for s in chromium_streams
    ]
    assert routing_plan(settled, CHROMIUM_PID, sink_index=84) == []


def test_the_parent_walk_reaches_a_chromium_audio_child(chromium_streams):
    """A target named by the window process still owns its audio child's streams."""
    plan = routing_plan(
        chromium_streams, CHROMIUM_WINDOW_PID, sink_index=84, ppid_of=ppid_of
    )
    assert [s.index for s in plan] == [9301, 9302, 9303, 9304, 9305]


def test_without_a_parent_walk_only_the_exact_pid_matches(chromium_streams):
    assert routing_plan(chromium_streams, CHROMIUM_WINDOW_PID, sink_index=84) == []


def test_the_parent_walk_is_bounded(chromium_streams):
    """pid 1 is nobody's meeting: an unbounded walk would sweep in the world."""
    assert routing_plan(chromium_streams, 1, sink_index=84, ppid_of=ppid_of) == []


def test_a_stream_with_no_pid_belongs_to_nobody():
    orphan = SinkInput(
        index=1, sink=57, pid=None, client=None, app_name=None, binary=None, media_name=None
    )
    assert owns_stream(orphan, CHROMIUM_PID) is False


# --------------------------------------------------------------------------
# Putting the audio back.
# --------------------------------------------------------------------------


def test_every_stream_goes_back_where_it_came_from():
    plan = restore_plan(
        {9301: 57, 9302: 57},
        live_streams=[9301, 9302],
        live_sinks=[57, 84],
        default_sink=DEFAULT_SINK,
    )
    assert plan == [(9301, 57), (9302, 57)]


def test_a_sink_that_is_gone_sends_the_stream_to_the_default_one():
    """The headset was unplugged mid-meeting; its index means nothing now."""
    plan = restore_plan(
        {9301: 99},
        live_streams=[9301],
        live_sinks=[57, 84],
        default_sink=DEFAULT_SINK,
    )
    assert plan == [(9301, DEFAULT_SINK)]


def test_a_stream_that_is_gone_is_not_chased():
    plan = restore_plan(
        {9301: 57}, live_streams=[], live_sinks=[57], default_sink=DEFAULT_SINK
    )
    assert plan == []


def test_with_no_default_sink_an_unknown_previous_sink_is_left_alone():
    """PipeWire rehomes an orphan itself; a guess would be worse than nothing."""
    plan = restore_plan(
        {9301: 99}, live_streams=[9301], live_sinks=[57], default_sink=None
    )
    assert plan == []


# --------------------------------------------------------------------------
# Leftovers from a previous life.
# --------------------------------------------------------------------------


def test_stale_modules_are_ours_only_and_loopbacks_first():
    modules = parse_modules(load("pactl-modules-stale.txt"))
    stale = stale_modules(modules)
    assert [m.index for m in stale] == [536870917, 536870916]
    assert [m.name for m in stale] == ["module-loopback", "module-null-sink"]
    assert 536870918 not in [m.index for m in stale], "someone else's null sink"


def test_stale_sink_names_are_extracted_for_the_rescue():
    assert stale_sink_names(parse_modules(load("pactl-modules-stale.txt"))) == [
        "munin-app-4310"
    ]


# --------------------------------------------------------------------------
# A fake pactl that keeps a model of the graph.
# --------------------------------------------------------------------------


class FakeRunner:
    """``pactl``, modelled: commands are applied to a small audio graph."""

    def __init__(
        self,
        *,
        streams: list[dict] | None = None,
        clients: list[dict] | None = None,
        sinks: list[dict] | None = None,
        modules: str = "",
        default_sink: str | None = DEFAULT_SINK,
        fail: tuple[str, ...] = (),
        missing: bool = False,
    ) -> None:
        self.streams = [dict(s) for s in (streams or [])]
        self.clients = list(clients or [])
        self.sinks = [dict(s) for s in (sinks or [])]
        self.modules = modules
        self.default_sink = default_sink
        self.fail = fail
        self.missing = missing
        self.calls: list[list[str]] = []
        self.unloaded: list[int] = []
        self._next_module = 536870916
        self._next_sink_index = 84
        self._lock = threading.Lock()

    # -- helpers used by the tests ------------------------------------
    def verbs(self) -> list[str]:
        """One short word per call, so an ordering assertion reads as prose."""
        out = []
        for argv in self.calls:
            if argv[1] == "load-module":
                out.append(f"load {argv[3].split('=')[1]}")
            elif argv[1] == "unload-module":
                out.append(f"unload {argv[2]}")
            elif argv[1] == "move-sink-input":
                out.append(f"move {argv[2]}->{argv[3]}")
        return out

    def sink_of(self, index: int) -> int | str | None:
        for stream in self.streams:
            if stream["index"] == index:
                return stream["sink"]
        return None

    def add_stream(self, index: int, client: str, sink: int = DEFAULT_SINK_INDEX) -> None:
        """A stream the application created after the recording started."""
        with self._lock:
            self.streams.append(
                {
                    "index": index,
                    "client": client,
                    "sink": sink,
                    "properties": {
                        "application.name": "Chromium",
                        "media.name": "Playback",
                        "object.serial": str(index),
                    },
                }
            )

    # -- the runner itself ---------------------------------------------
    def __call__(self, argv):
        argv = [str(a) for a in argv]
        with self._lock:
            self.calls.append(argv)
            if self.missing:
                return CommandResult(127, "", "pactl is not installed")
            joined = " ".join(argv)
            if any(marker in joined for marker in self.fail):
                return CommandResult(1, "", "Failure: Input/Output error")
            return self._apply(argv)

    def _apply(self, argv):
        verb = argv[1]
        if verb == "load-module":
            index = self._next_module
            self._next_module += 1
            if argv[2] == "module-null-sink":
                name = argv[3].split("=", 1)[1]
                self.sinks.append({"index": self._next_sink_index, "name": name})
                self._next_sink_index += 1
            return CommandResult(0, f"{index}\n", "")
        if verb == "unload-module":
            self.unloaded.append(int(argv[2]))
            return CommandResult(0, "", "")
        if verb == "move-sink-input":
            target = argv[3]
            by_name = {s["name"]: s["index"] for s in self.sinks}
            index = by_name.get(target, None)
            if index is None:
                try:
                    index = int(target)
                except ValueError:
                    return CommandResult(1, "", f"Failure: no sink {target}")
            for stream in self.streams:
                if stream["index"] == int(argv[2]):
                    stream["sink"] = index
                    return CommandResult(0, "", "")
            return CommandResult(1, "", "Failure: no such sink input")
        if verb == "get-default-sink":
            return CommandResult(0, f"{self.default_sink or ''}\n", "")
        if verb == "-f":
            what = argv[-1]
            if what == "sink-inputs":
                return CommandResult(0, json.dumps(self.streams), "")
            if what == "clients":
                return CommandResult(0, json.dumps(self.clients), "")
            if what == "sinks":
                return CommandResult(0, json.dumps(self.sinks), "")
        if verb == "list" and argv[2] == "modules":
            return CommandResult(0, self.modules, "")
        return CommandResult(1, "", f"unhandled: {argv}")


@pytest.fixture()
def chromium_runner() -> FakeRunner:
    return FakeRunner(
        streams=load("pactl-sink-inputs-chromium.json"),
        clients=load("pactl-clients-chromium.json"),
        sinks=[{"index": DEFAULT_SINK_INDEX, "name": DEFAULT_SINK}],
    )


def make_sink(runner: FakeRunner, pid: int = CHROMIUM_PID, **kwargs) -> PrivateSink:
    kwargs.setdefault("watch", False)
    # The settle window is real time on a real machine and nothing at all in a
    # test; a test that wants to see it passes its own recorder.
    kwargs.setdefault("sleep", lambda _seconds: None)
    return PrivateSink(str(pid), pid, runner=runner, **kwargs)


# --------------------------------------------------------------------------
# Opening, routing, closing.
# --------------------------------------------------------------------------


def test_open_creates_the_sink_then_the_loopback_then_moves_the_streams(chromium_runner):
    sink = make_sink(chromium_runner)
    assert sink.open() is True
    verbs = chromium_runner.verbs()
    assert verbs[0] == "load munin-app-4310"
    assert verbs[1] == "load munin-app-4310.monitor"
    assert [v for v in verbs if v.startswith("move")] == [
        f"move {i}->munin-app-4310" for i in (9301, 9302, 9303, 9304, 9305)
    ]
    assert sink.moves == 5
    assert chromium_runner.sink_of(9310) == DEFAULT_SINK_INDEX, "the music never moved"


def test_the_loopback_settles_on_silence_before_the_meeting_goes_through_it(chromium_runner):
    """The crackle a user hears on `munin start` mid-call, and why the wait is here.

    A null sink has no clock of its own, so the loopback drives the path and
    its resampler converges on the rate of whatever it is feeding. Moving the
    application's streams before that has happened puts the meeting through
    the convergence. The wait has to fall after the loopback is loaded and
    before the first move -- anywhere else and it buys nothing.
    """
    waits: list[float] = []
    sink = make_sink(chromium_runner, sleep=waits.append)
    assert sink.open() is True

    assert waits == [SETTLE_SECONDS]
    verbs = chromium_runner.verbs()
    assert verbs.index("load munin-app-4310.monitor") < verbs.index(
        "move 9301->munin-app-4310"
    ), "the loopback is loaded before the streams are moved"


def test_a_settle_of_zero_waits_not_at_all(chromium_runner):
    """A caller that has its own timing is not made to wait by this module."""
    waits: list[float] = []
    sink = make_sink(chromium_runner, settle_seconds=0, sleep=waits.append)
    assert sink.open() is True
    assert waits == []


def test_a_stream_created_mid_call_is_picked_up_by_the_next_pass(chromium_runner):
    """Chromium creates streams during a call; five at start is not the end of it."""
    sink = make_sink(chromium_runner)
    sink.open()
    chromium_runner.add_stream(9306, client="7077")
    assert sink.route() == 1
    assert chromium_runner.sink_of(9306) == 84
    assert sink.moves == 6


def test_routing_again_with_nothing_new_moves_nothing(chromium_runner):
    sink = make_sink(chromium_runner)
    sink.open()
    assert sink.route() == 0
    assert sink.moves == 5


def test_close_restores_the_streams_before_unloading_anything(chromium_runner):
    sink = make_sink(chromium_runner)
    sink.open()
    chromium_runner.calls.clear()
    assert sink.close() == []
    verbs = chromium_runner.verbs()
    moves = [i for i, v in enumerate(verbs) if v.startswith("move")]
    unloads = [i for i, v in enumerate(verbs) if v.startswith("unload")]
    assert moves and unloads
    assert max(moves) < min(unloads), "audio goes back before the plumbing goes away"
    assert all(chromium_runner.sink_of(i) == DEFAULT_SINK_INDEX for i in range(9301, 9306))


def test_close_unloads_the_loopback_before_the_sink(chromium_runner):
    """A loopback left reading a source that is gone is a module with no job."""
    sink = make_sink(chromium_runner)
    sink.open()
    sink.close()
    assert chromium_runner.unloaded == [536870917, 536870916]


def test_close_still_unloads_when_a_move_back_fails(chromium_runner):
    sink = make_sink(chromium_runner)
    sink.open()
    chromium_runner.fail = ("move-sink-input 9303",)
    warnings = sink.close()
    assert chromium_runner.unloaded == [536870917, 536870916]
    assert any("9303" in w for w in warnings)


def test_a_previous_sink_that_vanished_sends_the_stream_to_the_default(chromium_runner):
    sink = make_sink(chromium_runner)
    sink.open()
    # The headset the call was on is unplugged while the meeting runs.
    chromium_runner.sinks = [s for s in chromium_runner.sinks if s["name"] != DEFAULT_SINK]
    chromium_runner.sinks.append({"index": 91, "name": "alsa_output.usb-headset"})
    chromium_runner.default_sink = "alsa_output.usb-headset"
    sink.close()
    assert all(chromium_runner.sink_of(i) == 91 for i in range(9301, 9306))


def test_a_sink_that_cannot_be_created_leaves_nothing_behind(chromium_runner):
    chromium_runner.fail = ("module-null-sink",)
    sink = make_sink(chromium_runner)
    assert sink.open() is False
    assert chromium_runner.unloaded == []
    assert any("private sink" in w for w in sink.warnings)


def test_a_loopback_that_cannot_be_created_undoes_the_sink(chromium_runner):
    """Without the loopback the user would not hear the meeting at all."""
    chromium_runner.fail = ("module-loopback",)
    sink = make_sink(chromium_runner)
    assert sink.open() is False
    assert chromium_runner.unloaded == [536870916]
    assert any("speakers" in w for w in sink.warnings)


def test_a_missing_pactl_is_a_refusal_not_an_exception():
    sink = make_sink(FakeRunner(missing=True))
    assert sink.open() is False


def test_streams_present_follows_the_process_not_one_node(chromium_runner):
    sink = make_sink(chromium_runner)
    sink.open()
    assert sink.streams_present() is True
    # Chromium recycles nodes mid-call: four of the five vanish, and the
    # meeting is still running.
    chromium_runner.streams = [s for s in chromium_runner.streams if s["index"] != 9301]
    chromium_runner.streams = [s for s in chromium_runner.streams if s["index"] < 9303]
    assert sink.streams_present() is True
    chromium_runner.streams = [s for s in chromium_runner.streams if s["client"] != "7077"]
    assert sink.streams_present() is False


def test_streams_present_is_none_when_pipewire_cannot_be_asked():
    assert make_sink(FakeRunner(missing=True)).streams_present() is None


def test_describe_reports_the_sink_and_the_moves(chromium_runner):
    sink = make_sink(chromium_runner)
    sink.open()
    described = sink.describe()
    assert described["app_sink"] == "munin-app-4310"
    assert described["app_sink_streams"] == "5"
    assert described["app_sink_moves"] == "5"
    assert all(isinstance(v, str) for v in described.values())


def test_the_watcher_thread_keeps_moving_and_stops_at_close(chromium_runner):
    sink = PrivateSink(
        str(CHROMIUM_PID),
        CHROMIUM_PID,
        runner=chromium_runner,
        watch=True,
        watch_interval=0.02,
    )
    sink.open()
    chromium_runner.add_stream(9307, client="7077")
    deadline = threading.Event()
    for _ in range(200):
        if chromium_runner.sink_of(9307) == 84:
            break
        deadline.wait(0.01)
    assert chromium_runner.sink_of(9307) == 84, "the watcher never picked it up"
    sink.close()
    moves_after_close = len(chromium_runner.calls)
    deadline.wait(0.15)
    assert len(chromium_runner.calls) == moves_after_close, "the watcher outlived close()"


# --------------------------------------------------------------------------
# Recovery from a killed daemon.
# --------------------------------------------------------------------------


def _stale_runner(**kwargs) -> FakeRunner:
    return FakeRunner(
        modules=load("pactl-modules-stale.txt"),
        sinks=[{"index": DEFAULT_SINK_INDEX, "name": DEFAULT_SINK},
               {"index": 84, "name": "munin-app-4310"}],
        streams=[
            {"index": 9301, "client": "7077", "sink": 84,
             "properties": {"application.process.id": "4310"}},
            {"index": 9310, "client": "7500", "sink": DEFAULT_SINK_INDEX,
             "properties": {"application.process.id": "5150"}},
        ],
        **kwargs,
    )


def test_recovery_rescues_stranded_streams_then_unloads():
    runner = _stale_runner()
    notes = recover(runner=runner)
    assert runner.sink_of(9301) == DEFAULT_SINK_INDEX, "the meeting is audible again"
    assert runner.sink_of(9310) == DEFAULT_SINK_INDEX, "untouched"
    assert runner.unloaded == [536870917, 536870916]
    assert any("stranded" in note for note in notes)
    verbs = runner.verbs()
    assert verbs.index("move 9301->" + DEFAULT_SINK) < verbs.index("unload 536870917")


def test_recovery_leaves_other_peoples_modules_alone():
    runner = _stale_runner()
    recover(runner=runner)
    assert 536870918 not in runner.unloaded


def test_recovery_with_nothing_to_recover_does_nothing():
    runner = FakeRunner(modules="10\tmodule-device-restore\t\n")
    assert recover(runner=runner) == []
    assert runner.unloaded == []


def test_recovery_without_pactl_is_silent():
    runner = FakeRunner(missing=True)
    assert recover(runner=runner) == []


def test_recovery_reports_a_module_it_could_not_unload():
    runner = _stale_runner(fail=("unload-module 536870916",))
    notes = recover(runner=runner)
    assert any("could not unload" in note for note in notes)


# --------------------------------------------------------------------------
# The capturer in process-sink mode.
# --------------------------------------------------------------------------


MIC = CaptureTarget(kind="mic", handle="56", label="Synthetic Headset Mono")
APP = CaptureTarget(
    kind="app",
    handle="1301",
    label="Microsoft Teams",
    pid=CHROMIUM_PID,
    app_id="teams-tab",
)


class Session:
    def __init__(self, directory: Path) -> None:
        self.id = "2026-09-15T1030-weekly-quality-sync"
        self.directory = directory


@pytest.fixture()
def capture_env(monkeypatch, tmp_path, chromium_runner):
    """pw-record, ffmpeg and pactl all faked; the probe does not sleep."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr("munin.capture.linux.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr("munin.capture.linux._pw_dump", lambda tool: None)
    monkeypatch.setattr("munin.capture.linux.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        private_sink, "subprocess_runner", lambda env=None: chromium_runner
    )
    # The watcher would otherwise race the assertions.
    monkeypatch.setattr(private_sink, "WATCH_INTERVAL_SECONDS", 3600.0)
    return chromium_runner


def make_capturer(spawn, app=APP) -> PipewireCapturer:
    capturer = PipewireCapturer(MIC, app)
    capturer._spawn = spawn  # type: ignore[method-assign]
    return capturer


def test_a_target_with_a_pid_gets_its_own_sink(capture_env, tmp_path):
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    assert capturer.app_source == "process-sink"
    capturer.start(Session(tmp_path / "session"), 1)
    try:
        app_argv = spawn.commands("pw-record")[-1]
        assert app_argv[app_argv.index("--target") + 1] == "munin-app-4310"
        properties = app_argv[app_argv.index("-P") + 1]
        assert "stream.capture.sink = true" in properties
        assert "node.dont-reconnect = true" in properties
        assert capturer.app_source == "process-sink"
        assert capturer.describe()["app_sink"] == "munin-app-4310"
        assert capturer.describe()["app_sink_streams"] == "5"
        assert capturer.warnings == []
    finally:
        capturer.stop()


def test_stop_gives_the_audio_back(capture_env, tmp_path):
    capturer = make_capturer(SpawnRecorder())
    capturer.start(Session(tmp_path / "session"), 1)
    capturer.stop()
    assert capture_env.unloaded == [536870917, 536870916]
    assert all(capture_env.sink_of(i) == DEFAULT_SINK_INDEX for i in range(9301, 9306))


def test_a_failed_mic_start_still_gives_the_audio_back(capture_env, tmp_path):
    capturer = make_capturer(SpawnRecorder(fail=("--target 56",)))
    with pytest.raises(Exception, match="microphone capture failed"):
        capturer.start(Session(tmp_path / "session"), 1)
    assert capture_env.unloaded == [536870917, 536870916]


def test_without_pactl_the_app_track_falls_back_to_the_bound_stream(
    monkeypatch, tmp_path
):
    """A private sink is an improvement, never a precondition for recording."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr("munin.capture.linux.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr("munin.capture.linux.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "munin.capture.linux._pw_dump", lambda tool: load_fixture("pw-dump-teams-tab.json")
    )
    monkeypatch.setattr(
        private_sink, "subprocess_runner", lambda env=None: FakeRunner(missing=True)
    )
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(Session(tmp_path / "session"), 1)
    try:
        assert capturer.app_source == "stream"
        app_argv = spawn.commands("pw-record")[-1]
        assert app_argv[app_argv.index("--target") + 1] == "1301"
        assert any("private sink" in w for w in capturer.warnings)
    finally:
        capturer.stop()


def test_with_no_stream_left_to_bind_the_fallback_is_the_whole_desktop(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr("munin.capture.linux.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr("munin.capture.linux.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "munin.capture.linux._pw_dump", lambda tool: load_fixture("pw-dump-idle.json")
    )
    monkeypatch.setattr(
        private_sink, "subprocess_runner", lambda env=None: FakeRunner(missing=True)
    )
    capturer = make_capturer(SpawnRecorder())
    capturer.start(Session(tmp_path / "session"), 1)
    try:
        assert capturer.app_source == "sink-monitor"
    finally:
        capturer.stop()


def test_app_stream_present_asks_the_process_in_process_sink_mode(capture_env, tmp_path):
    capturer = make_capturer(SpawnRecorder())
    capturer.start(Session(tmp_path / "session"), 1)
    try:
        assert capturer.app_stream_present() is True
        # The node that detection bound is recycled; the call is not over.
        capture_env.streams = [s for s in capture_env.streams if s["index"] != 9301]
        assert capturer.app_stream_present() is True
        capture_env.streams = [s for s in capture_env.streams if s["client"] != "7077"]
        assert capturer.app_stream_present() is False
    finally:
        capturer.stop()


def test_a_recorder_that_dies_on_the_private_sink_falls_down_the_ladder(
    capture_env, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "munin.capture.linux._pw_dump", lambda tool: load_fixture("pw-dump-teams-tab.json")
    )
    spawn = SpawnRecorder(fail=("--target munin-app-4310",))
    capturer = make_capturer(spawn)
    capturer.start(Session(tmp_path / "session"), 1)
    try:
        assert capturer.app_source == "stream"
        assert capture_env.unloaded == [536870917, 536870916], "the sink was taken down"
        assert all(capture_env.sink_of(i) == DEFAULT_SINK_INDEX for i in range(9301, 9306))
    finally:
        capturer.stop()
