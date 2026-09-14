"""PipeWire two-track capture (spec 6.1).

Track 1 is the default source; track 2 is bound to the meeting application's own
``Stream/Output/Audio`` node, so music and notification sounds stay out of the
mix. Verified on this machine: a plain ``pw-record --target <object.serial>``
isolates one application at roughly 52 dB rejection of a second concurrent
stream, and ``stream.capture.sink`` is neither needed nor wanted for that.

Every PipeWire tool needs ``XDG_RUNTIME_DIR`` in its environment or it fails
without saying why; the systemd unit supplies it and this module asserts it.

Shape of one track: ``pw-record ... -`` writes raw s16 to a pipe that ``ffmpeg``
encodes to Opus. Two tracks means four processes and two pipes, deliberately
independent -- one dying must not truncate the other, and ``stop()`` succeeds
when either file is playable.

Three things this module is careful about:

- **Stopping in the right order.** SIGINT goes to ``pw-record`` first so it
  closes the pipe cleanly; ``ffmpeg`` then sees EOF and writes the Ogg trailer
  by itself. Signalling ffmpeg first would risk a container with no trailer.
  Every wait is bounded and escalates SIGINT -> SIGTERM -> SIGKILL, and every
  process is reaped, so no zombies are left behind.
- **Saying so when the app stream vanishes.** Measured here, and the single
  most surprising thing in this module: ``pw-record`` does *not* fail when its
  ``--target`` is missing or disappears. It silently records the default source
  instead -- which would fill the app track with your own microphone. So the
  app track is bound with ``node.dont-reconnect`` (the stream then goes quiet
  rather than wrong), the target is looked up before binding, and the honest
  answer to "has the meeting ended" is
  :meth:`PipewireCapturer.app_stream_present`, which asks PipeWire rather than
  guessing from a process that will not exit or a file that does not grow.
- **Falling back without failing.** If the app's stream node cannot be bound,
  the track is retried against the default sink monitor, and if that fails too
  the app track becomes a valid silent file. Either way the fallback is
  recorded in :attr:`PipewireCapturer.app_source` and in :meth:`describe`, and
  the microphone keeps recording.

Owner: capture workstream.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any, ClassVar, Literal, Sequence

from munin.capture.base import (
    SYSTEM_OUTPUT_HANDLE,
    CaptureError,
    CaptureResult,
    CaptureTarget,
    Capturer,
    SessionRef,
    segment_filenames,
)

__all__ = [
    "AppSource",
    "PipewireCapturer",
    "TrackHealth",
    "default_mic_target",
    "encode_command",
    "find_source",
    "is_playable",
    "record_command",
    "shutdown_chain",
    "silence_command",
    "stream_exists",
]

log = logging.getLogger("munin.capture")

#: ``handle`` value meaning "whatever PipeWire considers default right now".
DEFAULT_HANDLE = "default"

#: Where the app track's audio actually came from, once capture has started.
AppSource = Literal["stream", "sink-monitor", "silent"]


def initial_app_source(app: CaptureTarget | None) -> AppSource:
    """What the app track will be before anything has been probed.

    No target means silence; :data:`SYSTEM_OUTPUT_HANDLE` asks for the sink
    monitor outright (so no stream lookup is attempted and none can fail);
    anything else names one application's stream.
    """
    if app is None:
        return "silent"
    if app.handle == SYSTEM_OUTPUT_HANDLE:
        return "sink-monitor"
    return "stream"

#: A bare Ogg-Opus header pair is about 150 bytes; anything smaller than this
#: carries no audio at all and is not worth calling playable.
MIN_PLAYABLE_BYTES = 128

#: How long to let a recorder prove it could bind its target before deciding it
#: could not. ``pw-record`` exits within milliseconds on an unknown target.
START_PROBE_SECONDS = 0.7

#: Bounded waits, in seconds, for the stop sequence.
RECORDER_STOP_TIMEOUT = 5.0
ENCODER_STOP_TIMEOUT = 10.0
TERM_GRACE = 2.0
KILL_GRACE = 2.0

_PW_DUMP_TIMEOUT = 5.0


# --------------------------------------------------------------------------
# Command construction: pure, so the argument shape is a unit test.
# --------------------------------------------------------------------------


def record_command(
    target: str | None,
    *,
    rate: int,
    channels: int,
    capture_sink: bool = False,
    dont_reconnect: bool = False,
    pw_record: str = "pw-record",
) -> list[str]:
    """``pw-record`` argv writing raw s16 to stdout.

    ``target`` is a PipeWire ``object.serial`` or node name. ``None`` (or the
    literal ``"default"``) means "do not pass ``--target`` at all": pw-record
    has no ``default`` keyword, and omitting the flag is how you ask for the
    default source.

    ``capture_sink`` is the sink-monitor fallback -- it taps the mix going to
    the default sink, which is *not* isolated to one application and is only
    used when binding the application's own stream node failed.

    ``dont_reconnect`` matters more than it looks. Measured on this machine:
    given a ``--target`` that does not exist, or one that disappears mid
    recording, ``pw-record`` **silently records the default source instead** --
    which for the app track means quietly filling it with your own microphone.
    ``node.dont-reconnect`` stops that: the stream goes quiet rather than
    wrong. It does not make pw-record exit, so it is a correctness guard and
    not a signal; the signal is :meth:`PipewireCapturer.app_stream_present`.
    """
    argv = [pw_record]
    if target and target != DEFAULT_HANDLE:
        argv += ["--target", str(target)]
    properties = []
    if capture_sink:
        properties.append("stream.capture.sink = true")
    if dont_reconnect:
        properties.append("node.dont-reconnect = true")
    if properties:
        argv += ["-P", "{ " + ", ".join(properties) + " }"]
    argv += ["--format", "s16", "--rate", str(rate), "--channels", str(channels), "-"]
    return argv


def encode_command(
    out: Path,
    *,
    rate: int,
    channels: int,
    bitrate_kbps: int,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """``ffmpeg`` argv encoding raw s16 from stdin to Opus (spec 6.1: 24 kbps mono)."""
    return [
        ffmpeg,
        "-nostdin",
        "-loglevel", "error",
        "-y",
        "-f", "s16le",
        "-ar", str(rate),
        "-ac", str(channels),
        "-i", "pipe:0",
        "-c:a", "libopus",
        "-b:a", f"{bitrate_kbps}k",
        str(out),
    ]


def silence_command(
    out: Path,
    *,
    seconds: float,
    rate: int,
    channels: int,
    bitrate_kbps: int,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """``ffmpeg`` argv writing exactly ``seconds`` of silence to Opus.

    Used when there is no application to record: an ad-hoc session, or an app
    track that could bind nothing. It runs once, at stop, with the segment's
    measured duration, rather than pacing a null source for the length of the
    meeting -- ``-re`` on ``anullsrc`` drifts (0.8 s long over 3.7 s when
    measured here), and a drifting silent track would put the pair in
    ``segments[]`` out of step with each other and with ``session.json``.

    *Counterargument:* the file then only appears when the segment ends, so a
    daemon killed mid-recording leaves an ad-hoc session with no app track at
    all. Accepted: that track carries no information, the microphone track
    survives, and a length that matches is worth more to the worker than a
    placeholder that is there early.
    """
    layout = "mono" if channels == 1 else "stereo"
    return [
        ffmpeg,
        "-nostdin",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r={rate}:cl={layout}",
        "-t", f"{max(seconds, 0.0):.3f}",
        "-c:a", "libopus",
        "-b:a", f"{bitrate_kbps}k",
        str(out),
    ]


# --------------------------------------------------------------------------
# Source resolution.
# --------------------------------------------------------------------------


def stream_exists(dump: Sequence[Any], handle: str) -> bool:
    """Whether ``handle`` is still a live application playback stream.

    The pre-flight and the liveness check both rest on this. ``pw-record``
    cannot be asked to fail when its target is missing (measured: it records
    the default source instead), so the only way to know a stream is bindable
    is to look for it.
    """
    wanted = str(handle)
    for obj in dump:
        if not isinstance(obj, dict) or not str(obj.get("type", "")).endswith(":Node"):
            continue
        info = obj.get("info") or {}
        props = info.get("props") if isinstance(info, dict) else None
        props = props if isinstance(props, dict) else (obj.get("props") or {})
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        if str(props.get("object.serial")) == wanted or str(props.get("object.id")) == wanted:
            return True
    return False


def find_source(dump: Sequence[Any], mic_source: str = DEFAULT_HANDLE) -> CaptureTarget | None:
    """Resolve ``mic_source`` against a decoded ``pw-dump``.

    ``"default"`` follows the ``default`` metadata object (``pw-cli info
    @DEFAULT_SOURCE@`` does not work here); anything else is matched against
    ``node.name`` first and then ``object.serial``, so a config file can pin a
    device either way.

    Returns ``None`` when the dump holds no matching source, which the caller
    turns into the unpinned ``"default"`` target rather than an error.
    """
    from munin.detect.linux import default_source_name  # local: pure helper, no hardware

    sources: list[dict[str, Any]] = []
    for obj in dump:
        if not isinstance(obj, dict) or not str(obj.get("type", "")).endswith(":Node"):
            continue
        info = obj.get("info") or {}
        props = info.get("props") if isinstance(info, dict) else None
        props = props if isinstance(props, dict) else (obj.get("props") or {})
        if not str(props.get("media.class", "")).startswith("Audio/Source"):
            continue
        sources.append(props)

    wanted = mic_source
    if mic_source == DEFAULT_HANDLE:
        wanted = default_source_name(dump) or ""
        if not wanted:
            return None

    for props in sources:
        if props.get("node.name") == wanted or str(props.get("object.serial")) == str(wanted):
            serial = props.get("object.serial")
            if serial is None:
                serial = props.get("object.id")
            if serial is None:
                return None
            label = str(props.get("node.description") or props.get("node.name") or serial)
            return CaptureTarget(kind="mic", handle=str(serial), label=label)
    return None


def default_mic_target(
    mic_source: str = DEFAULT_HANDLE,
    *,
    pw_dump: str = "pw-dump",
) -> CaptureTarget:
    """Resolve the configured microphone to a :class:`CaptureTarget`.

    ``mic_source`` is either ``"default"``, a PipeWire node name, or an
    ``object.serial``. Resolving to a serial pins the device for the whole
    segment, so a default that changes mid-meeting cannot move the recording.

    A configured source that cannot be found is an error worth hearing about.
    The *default* source failing to resolve is not: we fall back to the
    unpinned target, which is exactly what ``pw-record`` with no ``--target``
    does anyway.
    """
    dump = _pw_dump(pw_dump)
    if dump is None:
        if mic_source != DEFAULT_HANDLE:
            raise CaptureError(
                f"cannot resolve microphone {mic_source!r}: {pw_dump} produced no usable output"
            )
        return CaptureTarget(kind="mic", handle=DEFAULT_HANDLE, label="default source")

    target = find_source(dump, mic_source)
    if target is not None:
        return target
    if mic_source != DEFAULT_HANDLE:
        raise CaptureError(
            f"microphone {mic_source!r} is not a PipeWire audio source on this machine"
        )
    return CaptureTarget(kind="mic", handle=DEFAULT_HANDLE, label="default source")


def _pw_dump(pw_dump: str) -> list[Any] | None:
    try:
        proc = subprocess.run(
            [pw_dump],
            capture_output=True,
            timeout=_PW_DUMP_TIMEOUT,
            env=_pipewire_env(),
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace") or "null")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, list) else None


def _pipewire_env() -> dict[str, str]:
    """Child environment for every PipeWire tool.

    ``XDG_RUNTIME_DIR`` is the whole point: without it the socket cannot be
    found and pw-record fails in a way that looks like silence.
    """
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime:
        raise CaptureError(
            "XDG_RUNTIME_DIR is not set; PipeWire tools cannot find the session "
            "socket. The systemd unit sets it with Environment=XDG_RUNTIME_DIR=%t."
        )
    env["XDG_RUNTIME_DIR"] = runtime
    return env


# --------------------------------------------------------------------------
# Tracks.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackHealth:
    """Non-blocking status of one track, for the daemon to act on.

    ``alive`` is process liveness and nothing more, which is all this layer can
    honestly offer. Two things measured on this machine say why:

    - ``pw-record`` neither exits nor errors when the node it was bound to
      disappears, so a vanished app stream is invisible from the process table.
    - ffmpeg's Ogg-Opus muxer writes **nothing** to disk until it closes the
      file (0 bytes through a 30 s capture that ended as a valid 38 KB file,
      unchanged by ``-flush_packets``), so a growing file is not a heartbeat
      either. ``bytes_written`` is therefore 0 for the whole segment and only
      meaningful afterwards.

    The question "has the meeting ended" belongs to PipeWire, and
    :meth:`PipewireCapturer.app_stream_present` is where it is asked.
    """

    kind: Literal["mic", "app"]
    alive: bool
    source: str
    recorder_returncode: int | None
    encoder_returncode: int | None
    bytes_written: int
    detail: str


@dataclass
class _Track:
    """One kind + one output file + the one or two processes writing it."""

    kind: Literal["mic", "app"]
    path: Path
    source: AppSource
    #: ``None`` for the silent track, which is written once, at stop.
    encoder: subprocess.Popen[bytes] | None = None
    encoder_argv: list[str] | None = None
    recorder: subprocess.Popen[bytes] | None = None
    recorder_argv: list[str] | None = None
    recorder_err: IO[bytes] | None = None
    encoder_err: IO[bytes] | None = None
    stopped: bool = False

    def bytes_written(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def alive(self) -> bool:
        """True while everything that should still be running is running.

        A track with no processes is the silent one: nothing can fail, so it is
        alive until the segment is stopped.
        """
        if self.encoder is None and self.recorder is None:
            return not self.stopped
        if self.encoder is not None and self.encoder.poll() is not None:
            return False
        if self.recorder is not None and self.recorder.poll() is not None:
            return False
        return True

    def health(self) -> TrackHealth:
        recorder_rc = self.recorder.poll() if self.recorder is not None else None
        encoder_rc = self.encoder.poll() if self.encoder is not None else None
        alive = self.alive()

        if not alive and recorder_rc not in (None, 0):
            detail = _tail(self.recorder_err) or f"recorder exited {recorder_rc}"
        elif not alive and encoder_rc not in (None, 0):
            detail = _tail(self.encoder_err) or f"encoder exited {encoder_rc}"
        elif not alive and self.stopped:
            detail = "stopped"
        elif not alive:
            detail = "the recorder went away"
        elif self.encoder is None:
            detail = "silence, written at stop"
        else:
            detail = "running"

        return TrackHealth(
            kind=self.kind,
            alive=alive,
            source=self.source,
            recorder_returncode=recorder_rc,
            encoder_returncode=encoder_rc,
            bytes_written=self.bytes_written(),
            detail=detail,
        )


def _tail(handle: IO[bytes] | None, limit: int = 400) -> str:
    """Last line of a captured stderr stream, for an error a human can read."""
    if handle is None:
        return ""
    try:
        handle.seek(0)
        text = handle.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return text.splitlines()[-1][:limit]


def _signal(proc: subprocess.Popen[bytes] | None, sig: int) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


def _wait(proc: subprocess.Popen[bytes] | None, timeout: float) -> int | None:
    if proc is None:
        return None
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def shutdown_chain(
    recorder: subprocess.Popen[bytes] | None,
    encoder: subprocess.Popen[bytes] | None,
    *,
    recorder_timeout: float = RECORDER_STOP_TIMEOUT,
    encoder_timeout: float = ENCODER_STOP_TIMEOUT,
) -> tuple[int | None, int | None]:
    """Stop one ``recorder | encoder`` pair in the only order that is safe.

    The recorder is interrupted first so it closes its end of the pipe; the
    encoder then reaches end of input on its own and writes the container
    trailer, which is what makes the Opus file playable. Only if it overstays
    its welcome is it signalled, and every process is waited on afterwards so
    nothing is left as a zombie.

    A recorder with no encoder (or the reverse) is fine; an encoder with no
    recorder -- the silence generator -- is interrupted directly, because
    nothing is going to close its input for it.
    """
    if recorder is not None:
        _signal(recorder, signal.SIGINT)
        if _wait(recorder, recorder_timeout) is None:
            _signal(recorder, signal.SIGTERM)
            if _wait(recorder, TERM_GRACE) is None:
                _signal(recorder, signal.SIGKILL)
                _wait(recorder, KILL_GRACE)
    else:
        _signal(encoder, signal.SIGINT)

    if encoder is not None and _wait(encoder, encoder_timeout) is None:
        _signal(encoder, signal.SIGINT)
        if _wait(encoder, TERM_GRACE) is None:
            _signal(encoder, signal.SIGTERM)
            if _wait(encoder, TERM_GRACE) is None:
                _signal(encoder, signal.SIGKILL)
                _wait(encoder, KILL_GRACE)

    recorder_rc = recorder.poll() if recorder is not None else None
    encoder_rc = encoder.poll() if encoder is not None else None
    return recorder_rc, encoder_rc


def is_playable(path: Path, *, minimum: int = MIN_PLAYABLE_BYTES) -> bool:
    """Whether a written track is worth keeping.

    Deliberately a size check and not an ``ffprobe`` call: ``stop()`` runs on
    the daemon's critical path at the end of a meeting, and a file that has a
    container header and some frames is what "playable" has to mean when the
    alternative is spawning a process to find out.
    """
    try:
        return path.stat().st_size >= minimum
    except OSError:
        return False


# --------------------------------------------------------------------------
# The capturer.
# --------------------------------------------------------------------------


class PipewireCapturer(Capturer):
    """``pw-record`` raw s16 piped into ``ffmpeg -c:a libopus``, one pair per segment."""

    method: ClassVar[str] = "pipewire-pw-record+ffmpeg-libopus"

    def __init__(
        self,
        mic: CaptureTarget,
        app: CaptureTarget | None,
        *,
        bitrate_kbps: int = 24,
        channels: int = 1,
        sample_rate: int = 48000,
        pw_record: str = "pw-record",
        ffmpeg: str = "ffmpeg",
        pw_dump: str = "pw-dump",
    ) -> None:
        super().__init__(
            mic,
            app,
            bitrate_kbps=bitrate_kbps,
            channels=channels,
            sample_rate=sample_rate,
        )
        self._pw_record = pw_record
        self._ffmpeg = ffmpeg
        self._pw_dump_tool = pw_dump
        self._tracks: dict[str, _Track] = {}
        self._running = False
        self._segment_index: int | None = None
        self._started_at: datetime | None = None
        #: Where the app track's audio came from once started (contracts 5).
        self.app_source: AppSource = initial_app_source(app)
        #: Non-fatal things the daemon should surface: a fallback, a dead track.
        self.warnings: list[str] = []

    # -- lifecycle --------------------------------------------------------

    def start(self, session: SessionRef, segment_index: int) -> None:
        if self._running:
            raise CaptureError("capture is already running; stop it before starting again")

        env = _pipewire_env()
        for tool in (self._pw_record, self._ffmpeg):
            if shutil.which(tool) is None:
                raise CaptureError(f"{tool} is not installed; capture cannot start")

        mic_name, app_name = segment_filenames(segment_index)
        directory = Path(session.directory)
        directory.mkdir(parents=True, exist_ok=True)
        mic_path = directory / mic_name
        app_path = directory / app_name

        self._tracks = {}
        self.warnings = []
        self.app_source = initial_app_source(self.app)

        # Pre-flight, and the reason it exists: pw-record does not fail when
        # --target names a node that is not there. It records the default
        # source instead -- which would fill the app track with the
        # microphone. Measured on this machine; there is no pw-record flag
        # that changes it. So the only safe binding is one we looked up first.
        if self.app_source == "stream" and not self._app_stream_bindable():
            self.warnings.append(
                f"{self.app.label} stream {self.app.handle} is gone; "
                "falling back to the sink monitor"
            )
            log.warning("%s", self.warnings[-1])
            self.app_source = "sink-monitor"

        # Audio starts flowing the moment the recorders are up, not after the
        # probe window below, so this is the timestamp the segment carries --
        # otherwise duration_seconds would undercount every segment by the
        # probe and the .opus files would be longer than session.json claims.
        spawned_at = datetime.now().astimezone()
        mic_track = self._spawn_recorded_track(
            "mic", mic_path, self.mic.handle, env=env, capture_sink=False
        )
        app_track = self._spawn_app_track(app_path, env=env)

        # One probe window covers both tracks: a recorder that cannot start at
        # all (missing socket, bad arguments) is gone within milliseconds, so
        # this is a bounded wait and not a poll loop.
        time.sleep(START_PROBE_SECONDS)

        if not mic_track.alive():
            detail = mic_track.health().detail
            self._teardown([mic_track, app_track])
            raise CaptureError(
                f"microphone capture failed to start ({self.mic.label}): {detail}"
            )

        if not app_track.alive():
            detail = app_track.health().detail
            log.warning("app track (%s) failed to start: %s", self.app_source, detail)
            self.warnings.append(f"app track ({self.app_source}) failed to start: {detail}")
            self._teardown([app_track])
            if self.app_source == "stream":
                self.app_source = "sink-monitor"
                app_track = self._spawn_app_track(app_path, env=env)
                time.sleep(START_PROBE_SECONDS)
            if not app_track.alive():
                detail = app_track.health().detail
                log.warning("sink monitor fallback also failed: %s", detail)
                self.warnings.append(f"sink monitor fallback failed: {detail}")
                self._teardown([app_track])
                self.app_source = "silent"
                app_track = self._spawn_app_track(app_path, env=env)

        app_track.source = self.app_source
        self._tracks = {"mic": mic_track, "app": app_track}
        self._segment_index = segment_index
        self._started_at = spawned_at
        self._running = True
        log.info(
            "capture started: segment %d, mic=%s, app=%s (%s)",
            segment_index,
            self.mic.label,
            self.app.label if self.app else "none",
            self.app_source,
        )

    def stop(self) -> CaptureResult:
        if not self._running:
            raise CaptureError("capture is not running")

        # The moment we interrupt the recorders is the end of the audio, so
        # that is the timestamp the segment carries.
        stopped_at = datetime.now().astimezone()
        self._running = False

        for track in self._tracks.values():
            track.stopped = True
            shutdown_chain(track.recorder, track.encoder)

        started_at = self._started_at or stopped_at
        segment_index = self._segment_index or 1
        duration = max(0.0, (stopped_at - started_at).total_seconds())

        mic_track = self._tracks["mic"]
        app_track = self._tracks["app"]
        if app_track.encoder is None and app_track.recorder is None:
            # The silent track: written now, at exactly the segment's length.
            self._write_silence(app_track, duration, env=dict(os.environ))

        mic_ok = is_playable(mic_track.path)
        app_ok = is_playable(app_track.path)

        for track, ok in ((mic_track, mic_ok), (app_track, app_ok)):
            if not ok:
                detail = track.health().detail
                log.error("%s track is not playable: %s", track.kind, detail)
                self.warnings.append(f"{track.kind} track is not playable: {detail}")

        self._close_stderr()

        if not mic_ok and not app_ok:
            raise CaptureError(
                "capture produced nothing playable: "
                + "; ".join(self.warnings[-2:] or ["no output was written"])
            )

        log.info(
            "capture stopped: segment %d, %.1f s, mic=%s, app=%s",
            segment_index,
            duration,
            "ok" if mic_ok else "unplayable",
            "ok" if app_ok else "unplayable",
        )
        return CaptureResult(
            segment_index=segment_index,
            mic_path=mic_track.path,
            app_path=app_track.path,
            started_at=started_at,
            stopped_at=stopped_at,
            duration_seconds=duration,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    # -- status -----------------------------------------------------------

    def health(self) -> list[TrackHealth]:
        """Cheap, non-blocking status of both tracks, in mic-then-app order.

        No subprocess is spawned -- it reads exit codes and one file size -- so
        the daemon can call it on every tick. Safe at any time, including
        before :meth:`start` (an empty list) and after :meth:`stop`. See
        :class:`TrackHealth` for what it cannot tell you.
        """
        return [self._tracks[kind].health() for kind in ("mic", "app") if kind in self._tracks]

    def failed_tracks(self) -> list[TrackHealth]:
        """Tracks whose processes have died while capture is still running.

        A real failure -- the encoder crashed, the recorder was killed -- and
        not the same question as "has the meeting ended", which
        :meth:`app_stream_present` answers.
        """
        if not self._running:
            return []
        return [track for track in self.health() if not track.alive]

    def app_stream_present(self) -> bool | None:
        """Whether the application stream this capture is bound to still exists.

        The authoritative answer to "has the meeting ended", and the one the
        daemon's grace period must rest on, because neither the process table
        nor the output file will tell you (see :class:`TrackHealth`). It costs
        one ``pw-dump``, so it belongs on the detection interval -- every few
        seconds -- rather than on every tick.

        ``None`` means the question does not apply (no application was bound,
        or the track is the sink monitor or silence) or that PipeWire could not
        be asked, which must never be read as "the meeting ended".
        """
        if self.app is None or self.app_source != "stream":
            return None
        dump = _pw_dump(self._pw_dump_tool)
        if dump is None:
            return None
        return stream_exists(dump, self.app.handle)

    def describe(self) -> dict[str, str]:
        return {
            "method": self.method,
            "mic": self.mic.label,
            "mic_handle": self.mic.handle,
            "app": self.app.label if self.app else "(none)",
            "app_handle": self.app.handle if self.app else "(none)",
            "app_source": self.app_source,
            "encoder": f"ffmpeg -c:a libopus -b:a {self.bitrate_kbps}k",
            "sample_rate": str(self.sample_rate),
            "channels": str(self.channels),
            "state": "running" if self._running else "idle",
            "warnings": "; ".join(self.warnings) or "(none)",
        }

    # -- plumbing ---------------------------------------------------------

    def _spawn(self, argv: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        """Single seam for spawning, so tests can stand in a fake process."""
        return subprocess.Popen(list(argv), **kwargs)

    def _app_stream_bindable(self) -> bool:
        """Is the app's stream node there to bind, right now?

        A dump we cannot read is not evidence that the stream is gone, so an
        unreachable ``pw-dump`` answers yes and lets the probe decide.
        """
        if self.app is None:
            return False
        dump = _pw_dump(self._pw_dump_tool)
        if dump is None:
            return True
        return stream_exists(dump, self.app.handle)

    def _spawn_app_track(self, path: Path, *, env: dict[str, str]) -> _Track:
        """Start the app track in whichever mode :attr:`app_source` currently names."""
        if self.app_source == "silent":
            return self._silent_track("app", path)
        if self.app_source == "sink-monitor":
            return self._spawn_recorded_track(
                "app", path, None, env=env, capture_sink=True
            )
        assert self.app is not None  # "stream" is only reachable with an app
        return self._spawn_recorded_track(
            "app", path, self.app.handle, env=env, capture_sink=False
        )

    def _spawn_recorded_track(
        self,
        kind: Literal["mic", "app"],
        path: Path,
        handle: str | None,
        *,
        env: dict[str, str],
        capture_sink: bool,
    ) -> _Track:
        recorder_argv = record_command(
            handle,
            rate=self.sample_rate,
            channels=self.channels,
            capture_sink=capture_sink,
            # The app track must never silently re-link to the default source:
            # that would record your own microphone as the other participants.
            # The microphone track is left free to reconnect, because a USB
            # device that blips should resume rather than end the meeting.
            dont_reconnect=(kind == "app"),
            pw_record=self._pw_record,
        )
        encoder_argv = encode_command(
            path,
            rate=self.sample_rate,
            channels=self.channels,
            bitrate_kbps=self.bitrate_kbps,
            ffmpeg=self._ffmpeg,
        )
        recorder_err = tempfile.TemporaryFile()
        encoder_err = tempfile.TemporaryFile()
        recorder = self._spawn(
            recorder_argv, stdout=subprocess.PIPE, stderr=recorder_err, env=env
        )
        try:
            encoder = self._spawn(
                encoder_argv,
                stdin=recorder.stdout,
                stdout=subprocess.DEVNULL,
                stderr=encoder_err,
                env=env,
            )
        finally:
            # The parent must not keep the read end open, or ffmpeg never sees
            # EOF when pw-record exits and stop() would hang on the encoder.
            if recorder.stdout is not None:
                recorder.stdout.close()
        return _Track(
            kind=kind,
            path=path,
            source="sink-monitor" if capture_sink else "stream",
            encoder=encoder,
            encoder_argv=encoder_argv,
            recorder=recorder,
            recorder_argv=recorder_argv,
            recorder_err=recorder_err,
            encoder_err=encoder_err,
        )

    def _silent_track(self, kind: Literal["mic", "app"], path: Path) -> _Track:
        """A placeholder for the track that will be silence (contracts 5).

        Nothing runs while the segment does; the file is written once, at stop,
        with the segment's measured duration. The pair in ``segments[]`` is
        still always complete, so the worker never has to special-case an
        ad-hoc session with no application.
        """
        return _Track(kind=kind, path=path, source="silent")

    def _write_silence(self, track: _Track, seconds: float, *, env: dict[str, str]) -> None:
        argv = silence_command(
            track.path,
            seconds=seconds,
            rate=self.sample_rate,
            channels=self.channels,
            bitrate_kbps=self.bitrate_kbps,
            ffmpeg=self._ffmpeg,
        )
        track.encoder_argv = argv
        try:
            proc = self._spawn(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
            )
            _, stderr = proc.communicate(timeout=ENCODER_STOP_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("could not write the silent app track: %s", exc)
            self.warnings.append(f"could not write the silent app track: {exc}")
            return
        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
            message = detail[-1] if detail else f"ffmpeg exited {proc.returncode}"
            log.error("could not write the silent app track: %s", message)
            self.warnings.append(f"could not write the silent app track: {message}")

    def _teardown(self, tracks: Sequence[_Track]) -> None:
        """Stop tracks that will not be part of the segment, reaping everything."""
        for track in tracks:
            track.stopped = True
            shutdown_chain(track.recorder, track.encoder)

    def _close_stderr(self) -> None:
        for track in self._tracks.values():
            for handle in (track.recorder_err, track.encoder_err):
                if handle is None:
                    continue
                try:
                    handle.close()
                except OSError:
                    pass
            track.recorder_err = None
            track.encoder_err = None
