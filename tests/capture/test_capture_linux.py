"""Command shape, start/stop sequencing and failure behaviour, with fake processes.

No PipeWire, no ffmpeg, no audio: every process is a :class:`FakeProcess` that
records the signals it was sent, so the stop *order* -- interrupt the recorder,
let the encoder flush by itself -- is an assertion rather than a hope. The real
thing is exercised separately as a live check (see the workstream notes).
"""

from __future__ import annotations

import json
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from munin.capture import get_capturer, segment_filenames
from munin.capture.base import CaptureError, CaptureTarget
from munin.capture.linux import (
    MIN_PLAYABLE_BYTES,
    PipewireCapturer,
    default_mic_target,
    encode_command,
    find_source,
    is_playable,
    record_command,
    shutdown_chain,
    silence_command,
    stream_exists,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """Decode one committed fixture by file name."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


MIC = CaptureTarget(kind="mic", handle="56", label="Synthetic Headset Mono")
APP = CaptureTarget(
    kind="app", handle="1301", label="Microsoft Teams", pid=4310, app_id="teams-tab"
)


@dataclass
class Session:
    """The structural slice of a session a capturer may see (contracts 5)."""

    id: str
    directory: Path


# --------------------------------------------------------------------------
# Command shape.
# --------------------------------------------------------------------------


def test_record_command_binds_one_stream():
    argv = record_command("1301", rate=48000, channels=1)
    assert argv == [
        "pw-record", "--target", "1301",
        "--format", "s16", "--rate", "48000", "--channels", "1", "-",
    ]
    # The isolating form takes no capture-sink property: that would tap the mix.
    assert "stream.capture.sink" not in " ".join(argv)


def test_record_command_omits_target_for_the_default_source():
    """pw-record has no "default" keyword; no --target is how you ask for it."""
    assert "--target" not in record_command("default", rate=48000, channels=1)
    assert "--target" not in record_command(None, rate=48000, channels=1)


def test_record_command_sink_monitor_fallback():
    argv = record_command(None, rate=48000, channels=1, capture_sink=True)
    assert argv[argv.index("-P") + 1] == "{ stream.capture.sink = true }"
    assert "--target" not in argv


def test_record_command_can_refuse_to_reconnect():
    """Measured: a lost target otherwise makes pw-record record the mic instead."""
    argv = record_command("1301", rate=48000, channels=1, dont_reconnect=True)
    assert argv[argv.index("-P") + 1] == "{ node.dont-reconnect = true }"


def test_record_command_merges_both_properties_into_one_flag():
    argv = record_command(None, rate=48000, channels=1, capture_sink=True, dont_reconnect=True)
    assert argv.count("-P") == 1
    assert argv[argv.index("-P") + 1] == (
        "{ stream.capture.sink = true, node.dont-reconnect = true }"
    )


def test_encode_command_is_opus_at_the_configured_bitrate(tmp_path):
    argv = encode_command(tmp_path / "app.opus", rate=48000, channels=1, bitrate_kbps=24)
    assert argv[:1] == ["ffmpeg"]
    assert argv[argv.index("-c:a") + 1] == "libopus"
    assert argv[argv.index("-b:a") + 1] == "24k"
    assert argv[argv.index("-i") + 1] == "pipe:0"
    assert argv[argv.index("-f") + 1] == "s16le"
    assert argv[argv.index("-ar") + 1] == "48000"
    assert argv[argv.index("-ac") + 1] == "1"
    assert argv[-1] == str(tmp_path / "app.opus")
    assert "-nostdin" in argv


def test_encode_command_honours_a_different_bitrate(tmp_path):
    argv = encode_command(tmp_path / "a.opus", rate=16000, channels=2, bitrate_kbps=48)
    assert argv[argv.index("-b:a") + 1] == "48k"
    assert argv[argv.index("-ar") + 1] == "16000"


def test_silence_command_writes_an_exact_duration(tmp_path):
    """The silent track is generated once, at the segment's measured length."""
    argv = silence_command(
        tmp_path / "app.opus", seconds=93.25, rate=48000, channels=1, bitrate_kbps=24
    )
    assert argv[argv.index("-i") + 1] == "anullsrc=r=48000:cl=mono"
    assert argv[argv.index("-t") + 1] == "93.250"
    assert argv[argv.index("-c:a") + 1] == "libopus"
    assert "-re" not in argv


def test_silence_command_never_asks_for_a_negative_duration(tmp_path):
    argv = silence_command(
        tmp_path / "app.opus", seconds=-1.0, rate=48000, channels=2, bitrate_kbps=24
    )
    assert argv[argv.index("-t") + 1] == "0.000"
    assert argv[argv.index("-i") + 1].endswith("cl=stereo")


# --------------------------------------------------------------------------
# A fake process, and the stop sequence it proves.
# --------------------------------------------------------------------------


@dataclass
class FakeProcess:
    """Enough of ``Popen`` to sequence a stop, with a record of what it was sent."""

    name: str
    signals: list[int] = field(default_factory=list)
    returncode: int | None = None
    #: Signals that this process declines to die from, to force an escalation.
    ignore: tuple[int, ...] = ()
    #: Exit code it reports once it does die.
    exit_code: int = 0
    stdout: object | None = None
    #: Shared log, so ordering across processes can be asserted.
    log: list[str] = field(default_factory=list)

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.log.append(f"{self.name}:{signal.Signals(sig).name}")
        if sig not in self.ignore and self.returncode is None:
            self.returncode = self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        self.log.append(f"{self.name}:wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.name, timeout or 0)
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        self.log.append(f"{self.name}:communicate")
        if self.returncode is None:
            self.returncode = self.exit_code
        return (b"", b"" if self.returncode == 0 else b"ffmpeg: something went wrong\n")


def test_stop_interrupts_the_recorder_before_waiting_on_the_encoder():
    log: list[str] = []
    recorder = FakeProcess("rec", log=log)
    encoder = FakeProcess("enc", log=log)
    # The encoder exits by itself once the pipe closes, as ffmpeg does.
    recorder.log = encoder.log = log

    def close_pipe(sig):
        FakeProcess.send_signal(recorder, sig)
        encoder.returncode = 0

    recorder.send_signal = close_pipe  # type: ignore[method-assign]

    assert shutdown_chain(recorder, encoder) == (0, 0)
    assert log[0] == "rec:SIGINT"
    assert encoder.signals == []  # never signalled: it flushed on its own


def test_stop_escalates_a_recorder_that_ignores_sigint():
    recorder = FakeProcess("rec", ignore=(signal.SIGINT,))
    encoder = FakeProcess("enc", returncode=0)
    shutdown_chain(recorder, encoder, recorder_timeout=0.0)
    assert recorder.signals == [signal.SIGINT, signal.SIGTERM]


def test_stop_kills_a_recorder_that_ignores_everything_but_sigkill():
    recorder = FakeProcess("rec", ignore=(signal.SIGINT, signal.SIGTERM))
    encoder = FakeProcess("enc", returncode=0)
    shutdown_chain(recorder, encoder, recorder_timeout=0.0)
    assert recorder.signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    assert recorder.poll() is not None  # reaped, not left as a zombie


def test_stop_escalates_a_stuck_encoder():
    recorder = FakeProcess("rec", returncode=0)
    encoder = FakeProcess("enc", ignore=(signal.SIGINT, signal.SIGTERM))
    shutdown_chain(recorder, encoder, recorder_timeout=0.0, encoder_timeout=0.0)
    assert encoder.signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]


def test_stop_interrupts_a_lone_encoder_directly():
    """The silence generator has no recorder to close its input for it."""
    encoder = FakeProcess("enc")
    assert shutdown_chain(None, encoder) == (None, 0)
    assert encoder.signals == [signal.SIGINT]


def test_stop_tolerates_an_already_dead_pair():
    recorder = FakeProcess("rec", returncode=0)
    encoder = FakeProcess("enc", returncode=0)
    assert shutdown_chain(recorder, encoder) == (0, 0)
    assert recorder.signals == []


# --------------------------------------------------------------------------
# The capturer, with every process faked.
# --------------------------------------------------------------------------


class SpawnRecorder:
    """Stands in for ``Popen``: records argv and hands back a fake process."""

    def __init__(self, *, fail: tuple[str, ...] = (), write_bytes: int = 4096) -> None:
        self.argv: list[list[str]] = []
        self.processes: list[FakeProcess] = []
        self._fail = fail
        self._write_bytes = write_bytes

    def __call__(self, argv, **kwargs):
        self.argv.append(list(argv))
        name = f"p{len(self.processes)}"
        proc = FakeProcess(name)
        joined = " ".join(str(a) for a in argv)
        if any(marker in joined for marker in self._fail):
            proc.returncode = 1
        # The encoder is the process that writes the output file.
        if argv[0].endswith("ffmpeg") and proc.returncode is None:
            Path(argv[-1]).write_bytes(b"O" * self._write_bytes)
        if kwargs.get("stdout") is subprocess.PIPE:
            proc.stdout = _ClosableStdout()
        self.processes.append(proc)
        return proc

    def commands(self, tool: str) -> list[list[str]]:
        return [argv for argv in self.argv if argv[0].endswith(tool)]


class _ClosableStdout:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fast_probe(monkeypatch):
    """Collapse the start probe so tests do not sleep."""
    monkeypatch.setattr("munin.capture.linux.time.sleep", lambda _s: None)


@pytest.fixture()
def tools(monkeypatch):
    """Pretend pw-record and ffmpeg are installed, and that PipeWire is mute.

    ``_pw_dump`` returning ``None`` is the "cannot ask" case, which the
    pre-flight must read as "assume bindable and let the probe decide" -- and
    it keeps the tests from shelling out to the real PipeWire.
    """
    monkeypatch.setattr("munin.capture.linux.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr("munin.capture.linux._pw_dump", lambda tool: None)


@pytest.fixture()
def gone_stream(monkeypatch):
    """PipeWire answers, and the application's stream is not in the answer."""
    monkeypatch.setattr(
        "munin.capture.linux._pw_dump", lambda tool: load_fixture("pw-dump-idle.json")
    )


@pytest.fixture()
def live_stream(monkeypatch):
    """PipeWire answers, and the application's stream is there (serial 1301)."""
    monkeypatch.setattr(
        "munin.capture.linux._pw_dump", lambda tool: load_fixture("pw-dump-teams-tab.json")
    )


@pytest.fixture()
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))


@pytest.fixture()
def session(tmp_path) -> Session:
    return Session(id="2026-09-15T1030-weekly-quality-sync", directory=tmp_path / "session")


def make_capturer(spawn, app=APP, **kwargs) -> PipewireCapturer:
    capturer = PipewireCapturer(MIC, app, **kwargs)
    capturer._spawn = spawn  # type: ignore[method-assign]
    return capturer


def test_start_writes_the_contracted_filenames(tools, runtime, fast_probe, session):
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(session, 1)
    try:
        mic_name, app_name = segment_filenames(1)
        outputs = [Path(argv[-1]).name for argv in spawn.commands("ffmpeg")]
        assert outputs == [mic_name, app_name]
        assert len(spawn.commands("pw-record")) == 2
        assert capturer.is_running
    finally:
        capturer.stop()


def test_start_uses_the_padded_filenames_for_a_resumed_segment(
    tools, runtime, fast_probe, session
):
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(session, 2)
    try:
        assert [Path(a[-1]).name for a in spawn.commands("ffmpeg")] == [
            "mic.002.opus",
            "app.002.opus",
        ]
    finally:
        capturer.stop()


def test_start_binds_the_app_stream_by_handle(tools, runtime, fast_probe, session):
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(session, 1)
    try:
        app_record = spawn.commands("pw-record")[1]
        assert app_record[app_record.index("--target") + 1] == "1301"
        assert capturer.app_source == "stream"
    finally:
        capturer.stop()


def test_start_refuses_a_second_start(tools, runtime, fast_probe, session):
    capturer = make_capturer(SpawnRecorder())
    capturer.start(session, 1)
    try:
        with pytest.raises(CaptureError, match="already running"):
            capturer.start(session, 2)
    finally:
        capturer.stop()


def test_stop_on_a_stopped_capturer_raises(tools, runtime, fast_probe, session):
    capturer = make_capturer(SpawnRecorder())
    with pytest.raises(CaptureError, match="not running"):
        capturer.stop()


def test_start_without_a_runtime_dir_says_so(monkeypatch, tools, fast_probe, session):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    capturer = make_capturer(SpawnRecorder())
    with pytest.raises(CaptureError, match="XDG_RUNTIME_DIR"):
        capturer.start(session, 1)


def test_start_without_the_tools_says_which_one(monkeypatch, runtime, fast_probe, session):
    monkeypatch.setattr(
        "munin.capture.linux.shutil.which", lambda tool: None if tool == "ffmpeg" else "/usr/bin/x"
    )
    capturer = make_capturer(SpawnRecorder())
    with pytest.raises(CaptureError, match="ffmpeg is not installed"):
        capturer.start(session, 1)


def test_a_dead_microphone_fails_the_start(tools, runtime, fast_probe, session):
    spawn = SpawnRecorder(fail=("--target 56",))
    capturer = make_capturer(spawn)
    with pytest.raises(CaptureError, match="microphone capture failed"):
        capturer.start(session, 1)
    assert not capturer.is_running


def test_the_app_track_refuses_to_reconnect_but_the_microphone_may(
    tools, runtime, fast_probe, session
):
    """Measured: a lost app target otherwise records the microphone silently."""
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(session, 1)
    try:
        mic_argv, app_argv = spawn.commands("pw-record")
        assert "node.dont-reconnect" not in " ".join(mic_argv)
        assert "node.dont-reconnect" in " ".join(app_argv)
    finally:
        capturer.stop()


def test_a_stream_that_is_already_gone_goes_straight_to_the_sink_monitor(
    tools, gone_stream, runtime, fast_probe, session
):
    """pw-record cannot be told to fail on a missing target, so we look first."""
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(session, 1)
    try:
        assert capturer.app_source == "sink-monitor"
        app_argv = spawn.commands("pw-record")[-1]
        assert "--target" not in app_argv
        assert "stream.capture.sink = true" in app_argv[app_argv.index("-P") + 1]
        assert any("is gone" in w for w in capturer.warnings)
        assert capturer.describe()["app_source"] == "sink-monitor"
    finally:
        capturer.stop()


def test_a_live_stream_is_bound_directly(tools, live_stream, runtime, fast_probe, session):
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(session, 1)
    try:
        assert capturer.app_source == "stream"
        app_argv = spawn.commands("pw-record")[-1]
        assert app_argv[app_argv.index("--target") + 1] == "1301"
        assert capturer.warnings == []
    finally:
        capturer.stop()


def test_a_recorder_that_dies_at_once_falls_back_to_the_sink_monitor(
    tools, runtime, fast_probe, session
):
    spawn = SpawnRecorder(fail=("--target 1301",))
    capturer = make_capturer(spawn)
    capturer.start(session, 1)
    try:
        assert capturer.app_source == "sink-monitor"
        fallback = spawn.commands("pw-record")[-1]
        assert "stream.capture.sink = true" in fallback[fallback.index("-P") + 1]
        assert any("failed to start" in w for w in capturer.warnings)
    finally:
        capturer.stop()


def test_a_failing_fallback_degrades_to_silence_and_keeps_the_microphone(
    tools, runtime, fast_probe, session
):
    spawn = SpawnRecorder(fail=("--target 1301", "stream.capture.sink"))
    capturer = make_capturer(spawn)
    capturer.start(session, 1)
    try:
        assert capturer.app_source == "silent"
        assert capturer.is_running
        assert [h.kind for h in capturer.health() if h.alive] == ["mic", "app"]
    finally:
        result = capturer.stop()
    assert result.mic_path.name == "mic.opus"
    assert result.app_path.name == "app.opus"
    assert any("anullsrc" in " ".join(a) for a in spawn.commands("ffmpeg"))


def test_no_app_means_a_silent_track_not_a_missing_one(tools, runtime, fast_probe, session):
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn, app=None)
    capturer.start(session, 1)
    try:
        assert capturer.app_source == "silent"
        assert len(spawn.commands("pw-record")) == 1   # only the microphone
        assert len(spawn.commands("ffmpeg")) == 1      # nothing encodes silence yet
    finally:
        result = capturer.stop()
    silence = spawn.commands("ffmpeg")[-1]
    assert "anullsrc=r=48000:cl=mono" in silence
    assert float(silence[silence.index("-t") + 1]) == pytest.approx(
        result.duration_seconds, abs=0.01
    )
    assert result.app_path.exists()


def test_a_silent_track_that_cannot_be_written_is_reported_not_raised(
    tools, runtime, fast_probe, session
):
    spawn = SpawnRecorder(fail=("anullsrc",))
    capturer = make_capturer(spawn, app=None)
    capturer.start(session, 1)
    result = capturer.stop()                      # the microphone still survives
    assert result.mic_path.exists()
    assert any("silent app track" in w for w in capturer.warnings)


def test_stop_returns_a_result_spanning_the_segment(tools, runtime, fast_probe, session):
    capturer = make_capturer(SpawnRecorder())
    capturer.start(session, 3)
    result = capturer.stop()
    assert result.segment_index == 3
    assert result.stopped_at >= result.started_at
    assert result.duration_seconds >= 0.0
    assert result.started_at.tzinfo is not None      # never naive (contracts 2)
    assert result.stopped_at.tzinfo is not None
    assert not capturer.is_running


def test_stop_succeeds_when_only_one_track_is_playable(tools, runtime, fast_probe, session):
    capturer = make_capturer(SpawnRecorder())
    capturer.start(session, 1)
    (session.directory / "app.opus").write_bytes(b"")   # encoder wrote nothing usable
    result = capturer.stop()
    assert result.mic_path.exists()
    assert any("app track is not playable" in w for w in capturer.warnings)


def test_stop_raises_when_neither_track_is_playable(tools, runtime, fast_probe, session):
    capturer = make_capturer(SpawnRecorder(write_bytes=0))
    capturer.start(session, 1)
    with pytest.raises(CaptureError, match="nothing playable"):
        capturer.stop()
    assert not capturer.is_running


def test_health_reports_a_dead_recorder(tools, runtime, fast_probe, session):
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(session, 1)
    try:
        assert [t.alive for t in capturer.health()] == [True, True]
        assert capturer.failed_tracks() == []

        app_recorder = spawn.processes[2]
        app_recorder.returncode = 0

        failed = capturer.failed_tracks()
        assert [t.kind for t in failed] == ["app"]
        assert failed[0].detail
        assert capturer.health()[0].alive is True   # the microphone is untouched
    finally:
        capturer.stop()


def test_a_silent_track_is_alive_and_says_what_it_is(tools, runtime, fast_probe, session):
    capturer = make_capturer(SpawnRecorder(), app=None)
    capturer.start(session, 1)
    try:
        app = capturer.health()[1]
        assert app.source == "silent"
        assert app.alive
        assert "written at stop" in app.detail
    finally:
        capturer.stop()


def test_app_stream_present_asks_pipewire(tools, live_stream, runtime, fast_probe, session):
    capturer = make_capturer(SpawnRecorder())
    capturer.start(session, 1)
    try:
        assert capturer.app_stream_present() is True
    finally:
        capturer.stop()


def test_app_stream_present_is_false_once_the_stream_is_gone(
    tools, runtime, fast_probe, session, monkeypatch
):
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(session, 1)          # pre-flight cannot ask: binds the stream
    try:
        assert capturer.app_source == "stream"
        monkeypatch.setattr(
            "munin.capture.linux._pw_dump", lambda tool: load_fixture("pw-dump-idle.json")
        )
        assert capturer.app_stream_present() is False
    finally:
        capturer.stop()


def test_app_stream_present_is_none_when_it_does_not_apply(tools, runtime, fast_probe, session):
    """Never answer "the meeting ended" for a question that was not asked."""
    adhoc = make_capturer(SpawnRecorder(), app=None)
    adhoc.start(session, 1)
    try:
        assert adhoc.app_stream_present() is None
    finally:
        adhoc.stop()
    assert PipewireCapturer(MIC, None).app_stream_present() is None


def test_health_is_empty_before_a_start():
    assert PipewireCapturer(MIC, APP).health() == []
    assert PipewireCapturer(MIC, APP).failed_tracks() == []


def test_failed_tracks_is_quiet_once_stopped(tools, runtime, fast_probe, session):
    capturer = make_capturer(SpawnRecorder())
    capturer.start(session, 1)
    capturer.stop()
    assert capturer.failed_tracks() == []


def test_describe_is_flat_strings(tools, runtime, fast_probe, session):
    described = PipewireCapturer(MIC, APP).describe()
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in described.items())
    assert described["method"] == "pipewire-pw-record+ffmpeg-libopus"
    assert described["encoder"] == "ffmpeg -c:a libopus -b:a 24k"
    assert described["app"] == "Microsoft Teams"
    assert described["state"] == "idle"


def test_the_capture_method_string_is_the_contracted_one():
    assert get_capturer("linux") is PipewireCapturer
    assert PipewireCapturer.method == "pipewire-pw-record+ffmpeg-libopus"


def test_the_parent_closes_the_pipe_read_end(tools, runtime, fast_probe, session):
    """Otherwise ffmpeg never sees EOF and stop() would block on the encoder."""
    spawn = SpawnRecorder()
    capturer = make_capturer(spawn)
    capturer.start(session, 1)
    try:
        pipes = [p.stdout for p in spawn.processes if p.stdout is not None]
        assert pipes and all(p.closed for p in pipes)
    finally:
        capturer.stop()


# --------------------------------------------------------------------------
# Playability and source resolution.
# --------------------------------------------------------------------------


def test_stream_exists_finds_a_live_playback_stream():
    dump = load_fixture("pw-dump-teams-tab.json")
    assert stream_exists(dump, "1301") is True
    assert stream_exists(dump, "1302") is False   # that is the capture stream
    assert stream_exists(dump, "999999") is False
    assert stream_exists(load_fixture("pw-dump-idle.json"), "1301") is False


def test_is_playable_rejects_an_empty_or_missing_file(tmp_path):
    missing = tmp_path / "gone.opus"
    assert not is_playable(missing)
    missing.write_bytes(b"")
    assert not is_playable(missing)
    missing.write_bytes(b"O" * MIN_PLAYABLE_BYTES)
    assert is_playable(missing)


def test_find_source_follows_the_default_metadata():
    target = find_source(load_fixture("pw-dump-idle.json"), "default")
    assert target is not None
    assert target.kind == "mic"
    assert target.handle == "56"
    assert target.label == "Synthetic Headset Mono"


def test_find_source_pins_a_configured_node_name():
    dump = load_fixture("pw-dump-idle.json")
    target = find_source(dump, "alsa_input.pci-0000_00_1f.3.analog-stereo")
    assert target is not None and target.handle == "58"


def test_find_source_accepts_a_serial():
    target = find_source(load_fixture("pw-dump-idle.json"), "58")
    assert target is not None and target.handle == "58"


def test_find_source_returns_none_for_an_unknown_device():
    assert find_source(load_fixture("pw-dump-idle.json"), "no-such-device") is None


def test_default_mic_target_falls_back_when_pw_dump_is_missing(runtime):
    target = default_mic_target(pw_dump="munin-no-such-tool")
    assert target.handle == "default"
    assert target.label == "default source"


def test_default_mic_target_refuses_to_guess_a_configured_device(runtime):
    with pytest.raises(CaptureError, match="cannot resolve microphone"):
        default_mic_target("my-headset", pw_dump="munin-no-such-tool")


def test_default_mic_target_needs_a_runtime_dir(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    with pytest.raises(CaptureError, match="XDG_RUNTIME_DIR"):
        default_mic_target()
