"""``munin setup``: the four questions that have no sensible default.

Backend, Microsoft 365 sign-in, which microphone, and a ten-second test
recording played back to confirm track separation (spec 15). The PoC asks only
the microphone and runs the test recording; the other two are deferred with the
features behind them.

``--non-interactive`` is what ``install.sh`` calls: create the data root and
write a default ``config.toml``, ask nothing.

Two deliberate choices here. The config is edited **line by line** rather than
re-serialised, because ``tomllib`` reads and does not write, and because a
config full of explanatory comments is worth more than a tidy writer. And the
test recording goes through :mod:`munin.capture`, not through a hand-rolled
``pw-record`` call, so what setup proves is the path a real recording takes.

Owner: install workstream.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from munin import config as config_module
from munin.config import Config

__all__ = [
    "AudioSource",
    "DEFAULT_CONFIG_TOML",
    "audio_sources",
    "default_source_name",
    "ensure_home",
    "set_config_value",
    "write_default_config",
    "cli_main",
    "main",
]

TEST_SECONDS = 10

#: Re-exported from :mod:`munin.config`, which owns the canonical template.
#: ``munin setup`` and ``install.sh`` write exactly what ``config.load`` parses,
#: so the two can never drift (PoC contracts section 9).
DEFAULT_CONFIG_TOML = config_module.DEFAULT_CONFIG_TOML


def _default_config_text() -> str:
    """The canonical template, read at call time so a test can monkeypatch it."""
    return config_module.DEFAULT_CONFIG_TOML


# ---------------------------------------------------------------------------
# The data root and the config file
# ---------------------------------------------------------------------------


def ensure_home(home: Path) -> Path:
    """Create ``~/munin`` and its subdirectories. Idempotent, never destructive."""
    home = Path(home).expanduser()
    for name in ("recordings", "inbox", "voices"):
        (home / name).mkdir(parents=True, exist_ok=True)
    return home


def write_default_config(home: Path, *, overwrite: bool = False) -> Path:
    """Write ``config.toml`` from ``config.DEFAULT_CONFIG_TOML``.

    Refuses to overwrite an existing file unless asked: a config is hand-edited
    and an installer that clobbers it is one people stop running.
    """
    home = ensure_home(home)
    path = home / "config.toml"
    if path.exists() and not overwrite:
        return path
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_default_config_text(), encoding="utf-8")
    os.replace(tmp, path)
    return path


def set_config_value(path: Path, section: str, key: str, value: str) -> bool:
    """Set ``key`` inside ``[section]`` to a quoted string, in place.

    Line-based on purpose: it keeps every comment in the file. Returns ``True``
    when the file changed. A missing section or key is appended rather than
    silently dropped.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else _default_config_text()
    lines = text.splitlines()
    header = f"[{section}]"
    new_line = f'{key} = "{value}"'

    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == header
            continue
        if not in_section:
            continue
        # Keep the author's alignment and trailing comment; only the value moves.
        match = re.match(
            rf"^(?P<indent>\s*)(?P<key>{re.escape(key)})(?P<gap>\s*=\s*)"
            r"(?P<value>.*?)(?P<comment>\s+#.*)?$",
            line,
        )
        if match:
            candidate = (
                f"{match.group('indent')}{match.group('key')}{match.group('gap')}"
                f'"{value}"{match.group("comment") or ""}'
            )
            if candidate == line:
                return False
            lines[index] = candidate
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True

    stripped_lines = [ln.strip() for ln in lines]
    if header in stripped_lines:
        # The section exists but the key does not: insert under the header.
        lines.insert(stripped_lines.index(header) + 1, new_line)
    else:
        lines.extend(["", header, new_line])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# PipeWire inspection. Read-only; setup never reconfigures the audio graph.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioSource:
    name: str
    description: str
    serial: str
    fmt: str
    is_default: bool = False

    @property
    def label(self) -> str:
        mark = " (default)" if self.is_default else ""
        return f"{self.description or self.name} [{self.fmt}]{mark}"


def _pw_dump() -> list[dict]:
    if not os.environ.get("XDG_RUNTIME_DIR"):
        raise RuntimeError("XDG_RUNTIME_DIR is unset; every PipeWire tool fails without it")
    if shutil.which("pw-dump") is None:
        raise RuntimeError("pw-dump is not installed")
    proc = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"pw-dump exited {proc.returncode}")
    data = json.loads(proc.stdout)
    return [obj for obj in data if isinstance(obj, dict)]


def _props(obj: dict) -> dict:
    """Nodes and clients keep their properties under ``info.props``; Metadata
    objects keep them at the top level with no ``info`` at all (verified against
    ``pw-dump`` on this machine)."""
    info = obj.get("info")
    if isinstance(info, dict) and isinstance(info.get("props"), dict):
        return info["props"]
    if isinstance(obj.get("props"), dict):
        return obj["props"]
    return {}


def _format_of(node: dict) -> str:
    info = node.get("info")
    params = info.get("params") if isinstance(info, dict) else None
    formats = params.get("EnumFormat") if isinstance(params, dict) else None
    if isinstance(formats, list):
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            def pick(key: str) -> object:
                value = fmt.get(key)
                return value.get("default") if isinstance(value, dict) else value

            parts = [str(p) for p in (pick("format"), pick("rate"), pick("channels")) if p]
            if parts:
                return " ".join(parts)
    props = _props(node)
    rate = props.get("audio.rate")
    channels = props.get("audio.channels")
    if rate or channels:
        return f"{rate or '?'} Hz {channels or '?'} ch"
    return "format unknown"


def default_source_name(objects: list[dict] | None = None) -> str | None:
    """The node name PipeWire currently treats as the default source."""
    objects = objects if objects is not None else _pw_dump()
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        if _props(obj).get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("key") != "default.audio.source":
                continue
            value = entry.get("value")
            if isinstance(value, dict):
                name = value.get("name")
                if isinstance(name, str):
                    return name
            if isinstance(value, str):
                return value
    return None


def audio_sources(objects: list[dict] | None = None) -> list[AudioSource]:
    """Every physical or virtual microphone PipeWire can see, default first."""
    objects = objects if objects is not None else _pw_dump()
    default = default_source_name(objects)
    sources: list[AudioSource] = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = _props(obj)
        if props.get("media.class") != "Audio/Source":
            continue
        name = str(props.get("node.name", ""))
        if not name or name == "quickshell":
            continue
        sources.append(
            AudioSource(
                name=name,
                description=str(props.get("node.description", "")),
                serial=str(props.get("object.serial", "")),
                fmt=_format_of(obj),
                is_default=(name == default),
            )
        )
    sources.sort(key=lambda s: (not s.is_default, s.description or s.name))
    return sources


def playback_streams(objects: list[dict] | None = None) -> list[tuple[str, str]]:
    """``(object.serial, label)`` for every application currently playing audio."""
    objects = objects if objects is not None else _pw_dump()
    clients: dict[int, str] = {}
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Client":
            props = _props(obj)
            client_id = obj.get("id")
            if isinstance(client_id, int):
                clients[client_id] = str(props.get("application.name", "")) or "unknown"
    streams: list[tuple[str, str]] = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = _props(obj)
        if str(props.get("media.class", "")) != "Stream/Output/Audio":
            continue
        serial = str(props.get("object.serial", ""))
        if not serial:
            continue
        client_id = props.get("client.id")
        label = clients.get(client_id, "") if isinstance(client_id, int) else ""
        streams.append((serial, label or str(props.get("node.name", "stream"))))
    return streams


# ---------------------------------------------------------------------------
# The ten-second test recording
# ---------------------------------------------------------------------------


def _tone_file(directory: Path, seconds: int) -> Path | None:
    """A synthetic tone to play as the 'app' track when nothing else is."""
    if shutil.which("ffmpeg") is None:
        return None
    out = directory / "tone.wav"
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000",
            "-ac", "1", str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return out if proc.returncode == 0 else None


def _play(path: Path) -> None:
    if shutil.which("pw-play") is None:
        print(f"  pw-play is not installed; play it yourself: {path}")
        return
    subprocess.run(["pw-play", str(path)], check=False)


def _test_recording(config: Config, mic: AudioSource, directory: Path) -> int:
    """Record both tracks for ten seconds and play them back. Returns an exit code."""
    from munin.capture import CaptureError, CaptureTarget, get_capturer

    directory.mkdir(parents=True, exist_ok=True)

    tone_proc: subprocess.Popen[bytes] | None = None
    app_target: CaptureTarget | None = None
    streams = playback_streams()
    if streams:
        serial, label = streams[0]
        app_target = CaptureTarget(kind="app", handle=serial, label=label)
        print(f"  app track: {label} (the application already playing)")
    else:
        tone = _tone_file(directory, TEST_SECONDS + 2)
        if tone is not None and shutil.which("pw-play"):
            tone_proc = subprocess.Popen(
                ["pw-play", str(tone)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.0)
            for serial, label in playback_streams():
                app_target = CaptureTarget(kind="app", handle=serial, label=label)
                print(f"  app track: a generated 440 Hz tone ({label})")
                break
        if app_target is None:
            print("  app track: nothing is playing; recording a silent app track")

    mic_target = CaptureTarget(
        kind="mic",
        handle=mic.serial or mic.name or "default",
        label=mic.description or mic.name,
    )

    class _Ref:
        id = "setup-test"
        directory = directory

    try:
        capturer_cls = get_capturer()
        capturer = capturer_cls(
            mic_target,
            app_target,
            bitrate_kbps=config.capture.bitrate_kbps,
            channels=config.capture.channels,
            sample_rate=config.capture.sample_rate,
        )
    except Exception as exc:  # noqa: BLE001 - setup must survive a missing layer
        print(f"  the capture layer is not available here: {exc}")
        if tone_proc is not None:
            tone_proc.terminate()
        return 1

    print(f"  recording {TEST_SECONDS} seconds -- say something.")
    try:
        capturer.start(_Ref(), 1)  # type: ignore[arg-type]
        time.sleep(TEST_SECONDS)
        result = capturer.stop()
    except CaptureError as exc:
        print(f"  capture failed: {exc}")
        return 1
    finally:
        if tone_proc is not None:
            tone_proc.terminate()

    print(f"  wrote {result.mic_path} and {result.app_path}")
    print("  playing the microphone track ...")
    _play(result.mic_path)
    print("  playing the application track ...")
    _play(result.app_path)
    print("  If you hear yourself on one track and the application on the other,")
    print("  the two-track capture works.")
    return 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _ask_microphone(config: Config, config_path: Path) -> None:
    try:
        sources = audio_sources()
    except (RuntimeError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"  could not list microphones: {exc}")
        return
    if not sources:
        print("  PipeWire reports no audio source; leaving capture.mic_source alone")
        return

    print("\nWhich microphone should munin record?")
    for index, source in enumerate(sources, start=1):
        print(f"  {index}) {source.label}")
    print(f"  {len(sources) + 1}) whatever PipeWire calls the default (recommended)")

    choice = input(f"Choice [1-{len(sources) + 1}, blank to keep current]: ").strip()
    if not choice:
        return
    try:
        number = int(choice)
    except ValueError:
        print("  not a number; leaving capture.mic_source alone")
        return
    if number == len(sources) + 1:
        value = "default"
        label = "the PipeWire default"
    elif 1 <= number <= len(sources):
        value = sources[number - 1].name
        label = sources[number - 1].label
    else:
        print("  out of range; leaving capture.mic_source alone")
        return
    if set_config_value(config_path, "capture", "mic_source", value):
        print(f"  capture.mic_source = {value!r} ({label}) in {config_path}")
    else:
        print(f"  capture.mic_source was already {value!r}")


def main(
    config: Config,
    *,
    non_interactive: bool = False,
    write_config_only: bool = False,
) -> int:
    home = Path(os.environ.get("MUNIN_HOME") or config.home).expanduser()
    ensure_home(home)
    config_path = write_default_config(home)
    print(f"data root: {home}")
    print(f"config:    {config_path}")

    if write_config_only or non_interactive:
        return 0

    _ask_microphone(config, config_path)

    answer = input(f"\nRun a {TEST_SECONDS}-second test recording? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        print("Skipped. Run `munin setup` again when you want to test the microphone.")
        return 0

    try:
        sources = audio_sources()
    except Exception as exc:  # noqa: BLE001
        print(f"  could not list microphones: {exc}")
        return 1
    wanted = config.capture.mic_source
    chosen = next(
        (s for s in sources if wanted in (s.name, s.serial)),
        next((s for s in sources if s.is_default), sources[0] if sources else None),
    )
    if chosen is None:
        print("  no microphone to test with")
        return 1

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    scratch = Path(runtime) / "munin-setup" if runtime else home / ".setup-test"
    return _test_recording(config, chosen, scratch)


def cli_main(argv: list[str] | None = None) -> int:
    """``munin setup`` argument handling, so ``cli.py`` can delegate verbatim."""
    import argparse

    parser = argparse.ArgumentParser(prog="munin setup")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--write-default-config", action="store_true")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        config = config_module.load()
    except NotImplementedError:
        home = Path(os.environ.get("MUNIN_HOME") or "~/munin").expanduser()
        config = Config(home=home)
    return main(
        config,
        non_interactive=args.non_interactive,
        write_config_only=args.write_default_config,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli_main())
