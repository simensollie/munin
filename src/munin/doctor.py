"""``munin doctor``: every open question becomes a line of output.

Worth building first (spec 15). It turns "is this set up correctly" into one
command and makes a broken install self-describing six months later. The check
table is per-platform from the start, even while only one column is populated
(spec 16.5).

``doctor`` always drives ``detect/linux.py`` directly, whatever
``detection.source`` says -- that is how a broken plugin is told apart from a
broken detector.

Design rules this module keeps to:

- **No check raises.** A check that blows up returns a ``fail`` line saying so.
  A doctor that crashes on the one broken thing it exists to find is useless.
- **No check mutates.** Every subprocess here is read-only, every file is
  opened for reading. ``doctor`` is safe to run mid-recording.
- **Nothing is imported that might not be implemented yet.** The stubbed
  modules of a half-built tree (``paths``, ``spool``, ``config.load``) are
  reached through ``try``/``except``, so ``doctor`` still reports on the parts
  that do work.

Owner: install workstream.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from munin import __version__
from munin import mixdown
from munin.config import Config
from munin.paths import SOCKET_NAME

__all__ = ["Check", "CheckResult", "DoctorEnv", "run_checks", "main"]

Status = Literal["ok", "warn", "fail", "skip"]

#: The platforms the check table has a column for (spec 16.5). Only the running
#: one is ever exercised; the others are printed as ``skip`` so a transcript
#: from another machine still shows what was not checked.
PLATFORMS: tuple[str, ...] = ("linux", "darwin", "win32")

PLUGIN_ID = "local.munin"
UNIT_NAME = "munin.service"
WORKER_UNIT_NAME = "munin-work.service"
KEYBIND_KEYS = "SUPER + SHIFT + R"
SPLIT_KEYBIND_KEYS = "SUPER + CTRL + SHIFT + R"
#: What install.sh writes into the managed block, in its order. Keep the two in
#: step: a binding here that install.sh does not write reads as a broken install
#: on every machine.
KEYBINDS: tuple[tuple[str, str], ...] = (
    (KEYBIND_KEYS, "munin toggle"),
    (SPLIT_KEYBIND_KEYS, "munin split --now"),
)
KEYBIND_MARK = "-- >>> munin (managed by munin install.sh) >>>"

_TIMEOUT = 5.0


@dataclass(frozen=True)
class CheckResult:
    """One line of ``munin doctor`` output."""

    name: str
    status: Status
    detail: str
    platform: str = "all"


@dataclass(frozen=True)
class Check:
    name: str
    platform: str  # "all" or a sys.platform value
    run: object  # Callable[[DoctorEnv], CheckResult | list[CheckResult]]


@dataclass(frozen=True)
class DoctorEnv:
    """Everything a check is allowed to look at.

    ``home`` and ``env`` are parameters rather than globals so the whole table
    can be pointed at a synthetic machine in a test without monkeypatching the
    process.
    """

    config: Config
    home: Path
    env: Mapping[str, str]
    platform: str = sys.platform

    @property
    def data_home(self) -> Path:
        override = self.env.get("MUNIN_HOME")
        if override:
            return Path(override)
        return Path(self.config.home).expanduser()

    def path(self, *parts: str) -> Path:
        return self.home.joinpath(*parts)

    def which(self, name: str) -> str | None:
        return shutil.which(name, path=self.env.get("PATH", os.defpath))


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _run(env: DoctorEnv, argv: Sequence[str]) -> tuple[int, str, str]:
    """Run a read-only command. Never raises; a missing binary is rc 127."""
    exe = env.which(argv[0])
    if exe is None:
        return 127, "", f"{argv[0]} not found"
    try:
        proc = subprocess.run(
            [exe, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env=dict(env.env),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out after {_TIMEOUT:g} s"
    except OSError as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _tool_check(env: DoctorEnv, name: str, tool: str, package: str) -> CheckResult:
    found = env.which(tool)
    if found is None:
        return CheckResult(name, "fail", f"{tool} not on PATH (omarchy pkg add {package})")
    return CheckResult(name, "ok", found)


def _pw_dump(env: DoctorEnv) -> tuple[list[dict], str | None]:
    """``pw-dump`` parsed, or an explanation of why not."""
    if not env.env.get("XDG_RUNTIME_DIR"):
        return [], "XDG_RUNTIME_DIR is unset; every PipeWire tool fails silently without it"
    rc, out, err = _run(env, ["pw-dump"])
    if rc != 0:
        return [], (err.strip() or f"pw-dump exited {rc}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return [], f"pw-dump output is not JSON: {exc}"
    if not isinstance(data, list):
        return [], "pw-dump did not return a list of objects"
    return [obj for obj in data if isinstance(obj, dict)], None


def _props(obj: Mapping) -> Mapping:
    """Properties of a ``pw-dump`` object.

    Nodes and clients carry them under ``info.props``; Metadata objects carry
    them at the top level with no ``info`` at all. Verified against ``pw-dump``
    on this machine -- reading only one of the two places silently loses the
    default-source metadata.
    """
    info = obj.get("info")
    if isinstance(info, Mapping):
        props = info.get("props")
        if isinstance(props, Mapping):
            return props
    props = obj.get("props")
    if isinstance(props, Mapping):
        return props
    return {}


# ---------------------------------------------------------------------------
# Checks: tools and runtime
# ---------------------------------------------------------------------------


def check_python(env: DoctorEnv) -> CheckResult:
    version = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info >= (3, 12):
        return CheckResult("python", "ok", f"{version} (munin {__version__})")
    return CheckResult("python", "fail", f"{version}; munin needs 3.12 or newer")


def check_ffmpeg(env: DoctorEnv) -> CheckResult:
    return _tool_check(env, "ffmpeg", "ffmpeg", "ffmpeg")


def check_pw_record(env: DoctorEnv) -> CheckResult:
    return _tool_check(env, "pw-record", "pw-record", "pipewire")


def check_pw_dump_tool(env: DoctorEnv) -> CheckResult:
    return _tool_check(env, "pw-dump", "pw-dump", "pipewire")


def check_runtime_dir(env: DoctorEnv) -> CheckResult:
    value = env.env.get("XDG_RUNTIME_DIR")
    if not value:
        return CheckResult(
            "XDG_RUNTIME_DIR",
            "fail",
            "unset; PipeWire capture and the daemon socket both need it",
        )
    path = Path(value)
    if not path.is_dir():
        return CheckResult("XDG_RUNTIME_DIR", "fail", f"{path} is not a directory")
    derived = (env.env.get("MUNIN_ENV_DERIVED") or "").split(",")
    if "XDG_RUNTIME_DIR" in derived:
        return CheckResult(
            "XDG_RUNTIME_DIR",
            "warn",
            f"{path} (derived: this shell does not export it; the systemd unit sets its own)",
        )
    return CheckResult("XDG_RUNTIME_DIR", "ok", str(path))


def check_pipewire(env: DoctorEnv) -> CheckResult:
    objects, problem = _pw_dump(env)
    if problem:
        return CheckResult("pipewire", "fail", problem)
    nodes = [o for o in objects if o.get("type") == "PipeWire:Interface:Node"]
    return CheckResult("pipewire", "ok", f"{len(objects)} objects, {len(nodes)} nodes")


def _default_source_name(objects: Iterable[Mapping]) -> str | None:
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        props = _props(obj)
        if props.get("metadata.name") != "default":
            continue
        metadata = obj.get("metadata")
        if not isinstance(metadata, list):
            continue
        for entry in metadata:
            if not isinstance(entry, Mapping):
                continue
            if entry.get("key") in ("default.audio.source", "default.configured.audio.source"):
                value = entry.get("value")
                if isinstance(value, Mapping):
                    name = value.get("name")
                    if isinstance(name, str):
                        return name
                elif isinstance(value, str):
                    return value
    return None


def _node_format(node: Mapping) -> str | None:
    info = node.get("info")
    if not isinstance(info, Mapping):
        return None
    params = info.get("params")
    if not isinstance(params, Mapping):
        return None
    formats = params.get("EnumFormat")
    if not isinstance(formats, list):
        return None
    for fmt in formats:
        if not isinstance(fmt, Mapping):
            continue
        rate = fmt.get("rate")
        if isinstance(rate, Mapping):
            rate = rate.get("default")
        channels = fmt.get("channels")
        if isinstance(channels, Mapping):
            channels = channels.get("default")
        sample = fmt.get("format")
        if isinstance(sample, Mapping):
            sample = sample.get("default")
        parts = [str(p) for p in (sample, rate, channels) if p is not None]
        if parts:
            return " / ".join(parts)
    return None


def check_default_source(env: DoctorEnv) -> CheckResult:
    objects, problem = _pw_dump(env)
    if problem:
        return CheckResult("default source", "fail", problem)
    wanted = env.config.capture.mic_source
    sources = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = _props(obj)
        if props.get("media.class") != "Audio/Source":
            continue
        sources.append((props, obj))
    if not sources:
        return CheckResult("default source", "fail", "no Audio/Source node in pw-dump")

    if wanted and wanted != "default":
        for props, obj in sources:
            if wanted in (props.get("node.name"), str(props.get("object.serial"))):
                fmt = _node_format(obj) or "format unknown"
                return CheckResult(
                    "default source", "ok", f"{props.get('node.name')} ({fmt}) [configured]"
                )
        return CheckResult(
            "default source",
            "fail",
            f"configured capture.mic_source {wanted!r} is not among {len(sources)} sources",
        )

    name = _default_source_name(objects)
    for props, obj in sources:
        if name is None or props.get("node.name") == name:
            fmt = _node_format(obj) or "format unknown"
            label = props.get("node.description") or props.get("node.name")
            return CheckResult("default source", "ok", f"{label} ({fmt})")
    return CheckResult(
        "default source",
        "warn",
        f"default source {name!r} is not among the {len(sources)} visible sources",
    )


def check_detection(env: DoctorEnv) -> CheckResult:
    """What the detector can see right now, whatever ``detection.source`` says."""
    try:
        from munin.detect import get_detector

        detector_cls = get_detector(env.platform)
        detector = detector_cls(env.config.app_rules)
        calls = detector.scan()
    except NotImplementedError as exc:
        reason = str(exc) or f"munin.detect.{env.platform} is not implemented yet"
        return CheckResult("detection", "warn", f"detector not available: {reason}")
    except Exception as exc:  # noqa: BLE001 - a doctor never crashes
        return CheckResult("detection", "warn", f"detector raised {type(exc).__name__}: {exc}")

    # "Nothing is happening" and "I could not look" are the same empty list.
    # scan() swallows a pw-dump failure by design -- a broken probe must not
    # take the daemon's poll loop down -- and records it for describe() to
    # report. Detection is the whole basis of D4, so a check that certifies a
    # dead detector as healthy is worse than no check.
    probe_error = ""
    try:
        probe_error = str(detector.describe().get("pw_error") or "")
    except Exception:  # noqa: BLE001 - a describe() that fails is not the diagnosis
        probe_error = ""
    if probe_error:
        return CheckResult(
            "detection", "fail", f"the detector could not read PipeWire: {probe_error}"
        )

    if not calls:
        return CheckResult(
            "detection",
            "ok",
            f"no call in progress ({len(env.config.app_rules)} app rules loaded)",
        )
    described = ", ".join(
        f"pid {c.evidence.pid} {(c.identity.label if c.identity else 'unidentified')}"
        for c in calls
    )
    return CheckResult("detection", "ok", f"{len(calls)} live: {described}")


# ---------------------------------------------------------------------------
# Checks: the Omarchy surface
# ---------------------------------------------------------------------------


def check_omarchy(env: DoctorEnv) -> CheckResult:
    found = env.which("omarchy")
    if found is None:
        return CheckResult("omarchy cli", "fail", "omarchy not on PATH")
    return CheckResult("omarchy cli", "ok", found)


def check_plugin_installed(env: DoctorEnv) -> CheckResult:
    directory = env.path(".config", "omarchy", "plugins", PLUGIN_ID)
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        return CheckResult("plugin installed", "fail", f"no manifest at {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("plugin installed", "fail", f"{manifest}: {exc}")
    if env.which("omarchy"):
        rc, out, err = _run(env, ["omarchy", "plugin", "validate", str(directory)])
        if rc != 0:
            return CheckResult(
                "plugin installed", "fail", (err.strip() or out.strip() or f"validate exited {rc}")
            )
    kinds = data.get("kinds")
    kinds_text = ",".join(kinds) if isinstance(kinds, list) else "?"
    return CheckResult(
        "plugin installed", "ok", f"{directory} (v{data.get('version', '?')}, kinds {kinds_text})"
    )


def check_plugin_on_bar(env: DoctorEnv) -> CheckResult:
    shell_json = env.path(".config", "omarchy", "shell.json")
    if not shell_json.is_file():
        return CheckResult("plugin on bar", "warn", f"no {shell_json}")
    try:
        data = json.loads(shell_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("plugin on bar", "fail", f"{shell_json}: {exc}")
    layout = (data.get("bar") or {}).get("layout") or {}
    for section, widgets in layout.items():
        if not isinstance(widgets, list):
            continue
        for index, widget in enumerate(widgets):
            if isinstance(widget, Mapping) and widget.get("id") == PLUGIN_ID:
                return CheckResult(
                    "plugin on bar", "ok", f"{PLUGIN_ID} in bar.layout.{section}[{index}]"
                )
    return CheckResult(
        "plugin on bar",
        "fail",
        f"{PLUGIN_ID} is not in bar.layout (omarchy bar put {PLUGIN_ID} --section center --after omarchy.weather)",
    )


def check_keybind(env: DoctorEnv) -> CheckResult:
    link = env.path(".config", "hypr", "bindings.lua")
    if not link.exists():
        return CheckResult("keybind", "fail", f"no {link}")
    target = link.resolve()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult("keybind", "fail", f"{target}: {exc}")
    # Two bindings since the split (D26). The quoted form is what makes this
    # safe to test by substring: `"SUPER + SHIFT + R"` does not occur inside
    # `"SUPER + CTRL + SHIFT + R"`, so neither binding counts the other's line.
    lines = [line.strip() for line in text.splitlines() if line.lstrip().startswith("o.bind(")]
    found = {
        keys: [line for line in lines if f'"{keys}"' in line]
        for keys, _ in KEYBINDS
    }
    where = str(target) if target != link else str(link)
    for keys, _ in KEYBINDS:
        if len(found[keys]) > 1:
            return CheckResult(
                "keybind", "fail", f"{keys} is bound {len(found[keys])} times in {where}"
            )
    bound = [keys for keys, _ in KEYBINDS if found[keys]]
    if KEYBIND_MARK not in text:
        if bound:
            return CheckResult(
                "keybind",
                "warn",
                f"{', '.join(bound)} bound in {where} but not by munin",
            )
        return CheckResult("keybind", "fail", f"no munin block in {where}")
    missing = [keys for keys, _ in KEYBINDS if not found[keys]]
    if missing == [keys for keys, _ in KEYBINDS]:
        return CheckResult("keybind", "fail", f"munin block in {where} has no o.bind line")
    if missing:
        # An install that predates the second binding. Not a failure: what is
        # bound works, and re-running install.sh rewrites the block.
        return CheckResult(
            "keybind",
            "warn",
            f"{', '.join(missing)} missing from the munin block in {where}"
            " -- re-run install.sh",
        )
    detail = ", ".join(f"{keys} -> {command}" for keys, command in KEYBINDS)
    return CheckResult("keybind", "ok", f"{detail} ({where})")


def _unit_result(
    env: DoctorEnv, *, name: str, unit_name: str, missing: Status
) -> CheckResult:
    """One user unit's file, enablement and liveness.

    ``missing`` is the caller's call: a recorder with no unit cannot record, but
    a worker with no unit only means transcripts wait -- unless something is
    configured that needs it.
    """
    unit = env.path(".config", "systemd", "user", unit_name)
    if not unit.is_file():
        return CheckResult(name, missing, f"no {unit}")
    enabled = _run(env, ["systemctl", "--user", "is-enabled", unit_name])[1].strip() or "unknown"
    active = _run(env, ["systemctl", "--user", "is-active", unit_name])[1].strip() or "unknown"
    detail = f"{unit} (is-enabled: {enabled}, is-active: {active})"
    if active == "active":
        return CheckResult(name, "ok", detail)
    if enabled in ("enabled", "enabled-runtime"):
        return CheckResult(name, "warn", detail)
    return CheckResult(
        name,
        "warn",
        detail + f" -- enable it with: systemctl --user enable --now {unit_name}",
    )


def check_unit(env: DoctorEnv) -> CheckResult:
    return _unit_result(
        env, name="systemd unit", unit_name=UNIT_NAME, missing="fail"
    )


def check_worker_unit(env: DoctorEnv) -> CheckResult:
    """The worker's unit. Only a failure when something depends on it running.

    With `backend = "none"` and no export, a worker that never runs costs
    nothing: every session's designed end state is `pending`. With
    `[export] enabled`, the worker is the only thing that refills the upload
    folder, so its absence is the whole feature silently not happening -- which
    is the failure this check exists to name.
    """
    result = _unit_result(
        env,
        name="worker unit",
        unit_name=WORKER_UNIT_NAME,
        missing="fail" if env.config.export.enabled else "warn",
    )
    if env.config.export.enabled and result.status != "ok":
        return CheckResult(
            result.name,
            "fail" if result.status == "fail" else "warn",
            result.detail + " -- [export] is enabled and needs this unit running",
        )
    return result


def check_export(env: DoctorEnv) -> CheckResult:
    """The upload folder: configured, present, writable (spec 10, D25)."""
    export = env.config.export
    if not export.enabled:
        return CheckResult(
            "export", "ok", "[export] disabled; mixes are written by `munin mix` only"
        )
    directory = env.config.export_dir
    detail = f"{directory} (format: {export.format})"
    if not directory.exists():
        return CheckResult(
            "export", "warn", detail + " -- does not exist yet; the worker creates it"
        )
    if not directory.is_dir():
        return CheckResult("export", "fail", detail + " -- exists and is not a directory")
    if not os.access(directory, os.W_OK):
        return CheckResult("export", "fail", detail + " -- not writable")
    # Waiting vs. carried: the two numbers a person actually wants from this
    # folder. "Waiting" is what is still to be dragged into the importer;
    # "carried" is the ledger, and the reason the waiting list does not grow
    # back after an upload (spec 10).
    suffixes = {f".{fmt.extension}" for fmt in mixdown.FORMATS.values()}
    try:
        waiting = sum(
            1 for path in directory.iterdir() if path.is_file() and path.suffix in suffixes
        )
        carried = len(mixdown.exported_ids(directory))
    except OSError as exc:  # a check never raises
        return CheckResult("export", "warn", f"{detail} -- cannot be listed: {exc}")
    return CheckResult(
        "export",
        "ok",
        f"{detail} -- {waiting} waiting to upload, {carried} already exported",
    )


def check_daemon(env: DoctorEnv) -> CheckResult:
    runtime = env.env.get("XDG_RUNTIME_DIR")
    if not runtime:
        return CheckResult("daemon", "fail", "XDG_RUNTIME_DIR is unset; no socket path to try")
    sock_path = Path(runtime) / "munin" / SOCKET_NAME
    if not sock_path.exists():
        return CheckResult("daemon", "warn", f"no socket at {sock_path}; munin-rec is not running")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_TIMEOUT)
            sock.connect(str(sock_path))
            sock.sendall(b'{"cmd": "ping"}\n')
            raw = sock.makefile("rb").readline()
        reply = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return CheckResult("daemon", "fail", f"{sock_path}: {exc}")
    if not reply.get("ok"):
        error = reply.get("error") or {}
        return CheckResult("daemon", "fail", f"ping refused: {error.get('message', reply)}")
    data = reply.get("data") or {}
    return CheckResult(
        "daemon", "ok", f"pid {data.get('pid', '?')}, version {data.get('version', '?')}"
    )


# ---------------------------------------------------------------------------
# Checks: the data root
# ---------------------------------------------------------------------------


def check_data_root(env: DoctorEnv) -> CheckResult:
    home = env.data_home
    if not home.is_dir():
        return CheckResult("data root", "fail", f"{home} does not exist (run: munin setup)")
    missing = [
        name
        for name in (
            env.config.paths.recordings,
            env.config.paths.inbox,
            env.config.paths.voices,
        )
        if not (home / name).is_dir()
    ]
    if missing:
        return CheckResult("data root", "fail", f"{home} is missing {', '.join(missing)}")
    return CheckResult("data root", "ok", str(home))


def check_config(env: DoctorEnv) -> CheckResult:
    path = env.data_home / "config.toml"
    if not path.is_file():
        return CheckResult("config", "warn", f"no {path}; every key is defaulted")
    try:
        with path.open("rb") as handle:
            tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return CheckResult("config", "fail", f"{path}: {exc}")
    if env.config.unknown_keys:
        return CheckResult(
            "config",
            "warn",
            f"{path}: unknown keys kept but ignored: {', '.join(env.config.unknown_keys)}",
        )
    return CheckResult("config", "ok", str(path))


def check_disk(env: DoctorEnv) -> CheckResult:
    home = env.data_home
    probe = home if home.is_dir() else home.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return CheckResult("disk space", "fail", f"{probe}: {exc}")
    free_mb = usage.free // (1024 * 1024)
    minimum = env.config.capture.min_free_mb
    detail = f"{free_mb} MB free at {probe} (minimum {minimum} MB)"
    if free_mb < minimum:
        return CheckResult("disk space", "fail", detail)
    if free_mb < minimum * 2:
        return CheckResult("disk space", "warn", detail)
    return CheckResult("disk space", "ok", detail)


def _session_states(home: Path, inbox_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    inbox = home / inbox_name
    if not inbox.is_dir():
        return counts
    for entry in sorted(inbox.iterdir()):
        session_json = entry / "session.json"
        try:
            data = json.loads(session_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            counts["unreadable"] = counts.get("unreadable", 0) + 1
            continue
        state = str(data.get("state", "unknown"))
        counts[state] = counts.get(state, 0) + 1
    return counts


def check_spool(env: DoctorEnv) -> CheckResult:
    home = env.data_home
    if not home.is_dir():
        return CheckResult("spool", "warn", f"{home} does not exist yet")
    counts = _session_states(home, env.config.paths.inbox)
    if not counts:
        return CheckResult("spool", "ok", "inbox is empty")
    summary = ", ".join(f"{state}: {count}" for state, count in sorted(counts.items()))
    status: Status = "fail" if counts.get("failed") or counts.get("unreadable") else "warn"
    return CheckResult("spool", status, summary)


def check_orphans(env: DoctorEnv) -> CheckResult:
    """Session directories holding audio with no ``session.json`` (D26).

    Nothing else in munin can see one. The spool finds sessions by scanning for
    ``session.json``, so a directory without one is invisible to ``munin list``,
    to the worker, to the export and to whatever retention eventually becomes:
    it is a real meeting's audio that no record describes, and it will sit there
    until somebody looks with ``ls``.

    They are produced by exactly one shape of failure. A split fills each half's
    directory *before* writing its record, precisely so a half-copied session is
    never claimed mid-cut, and rolls the directories back when the cut fails --
    but a crash, a kill, or a full disk between those two steps leaves the
    directory behind. This is the check that says so out loud.

    A warning, not a failure: nothing is broken, and the audio is still there.
    """
    recordings = env.data_home / env.config.paths.recordings
    if not recordings.is_dir():
        return CheckResult("orphan sessions", "ok", f"{recordings} does not exist yet")
    try:
        # recordings/YYYY/MM/<session>/ -- spec 7.5.
        orphans = [
            directory
            for directory in sorted(recordings.glob("*/*/*"))
            if directory.is_dir()
            and not (directory / "session.json").exists()
            and any(directory.glob("*.opus"))
        ]
    except OSError as exc:  # a check never raises
        return CheckResult("orphan sessions", "warn", f"{recordings} cannot be walked: {exc}")
    if not orphans:
        return CheckResult("orphan sessions", "ok", "none")
    listed = ", ".join(directory.name for directory in orphans[:3])
    if len(orphans) > 3:
        listed += f", and {len(orphans) - 3} more"
    return CheckResult(
        "orphan sessions",
        "warn",
        f"{len(orphans)} with audio and no session.json ({listed}) under {recordings}"
        " -- no munin command can see them; delete them or write the record by hand",
    )


def check_backend(env: DoctorEnv) -> CheckResult:
    backend = env.config.transcribe.backend
    if backend == "none":
        return CheckResult(
            "transcription",
            "warn",
            "backend 'none': transcription is deferred, sessions stay pending by design",
        )
    return CheckResult("transcription", "ok", f"backend {backend!r}")



def check_platform_support(env: DoctorEnv) -> list[CheckResult]:
    """One row per platform column (spec 16.5). Only this one is populated."""
    rows: list[CheckResult] = []
    for platform in PLATFORMS:
        if platform != env.platform:
            rows.append(
                CheckResult(
                    f"platform {platform}", "skip", "not this machine", platform=platform
                )
            )
            continue
        parts: list[str] = []
        status: Status = "ok"
        for layer, factory in (("capture", "munin.capture"), ("detect", "munin.detect")):
            try:
                module = __import__(factory, fromlist=["get"])
                getter = getattr(module, f"get_{'capturer' if layer == 'capture' else 'detector'}")
                cls = getter(platform)
                parts.append(f"{layer}: {cls.__name__}")
            except Exception as exc:  # noqa: BLE001 - a doctor never crashes
                parts.append(f"{layer}: unavailable ({exc})")
                status = "fail"
        rows.append(CheckResult(f"platform {platform}", status, "; ".join(parts), platform=platform))
    return rows


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def check_m365(env: DoctorEnv) -> CheckResult:
    """Microsoft 365 enrichment (spec 7.6): signed in, calendar copy fresh.

    A warning at worst, never a failure: without it sessions are named from the
    clock, which is how every session was named before M9.
    """
    from datetime import datetime, timedelta

    from munin import m365

    settings = env.config.m365
    if not settings.enabled:
        return CheckResult("m365", "ok", "[m365] disabled; sessions are named from the clock")
    if env.which("secret-tool") is None:
        return CheckResult(
            "m365", "warn", "secret-tool not found; install libsecret to keep the sign-in"
        )
    try:
        graph = m365.Graph.from_settings(settings)
        signed_in = graph.signed_in()
    except Exception as exc:  # noqa: BLE001 - a check never raises
        return CheckResult("m365", "warn", f"keyring unavailable: {exc}")
    if not signed_in:
        return CheckResult("m365", "warn", "not signed in; run `munin m365 login`")
    path = m365.calendar_path(env.data_home)
    events = m365.read_calendar(env.data_home, max_age=timedelta(days=3650))
    try:
        age = datetime.now().timestamp() - path.stat().st_mtime
    except OSError:
        return CheckResult(
            "m365", "warn", "signed in; no calendar copy yet (munin-work writes it)"
        )
    limit = max(900, 3 * settings.calendar_refresh_seconds)
    detail = f"signed in; calendar copy {int(age // 60)} min old, {len(events)} events"
    if age > limit:
        return CheckResult(
            "m365",
            "warn",
            detail + " -- stale, so sessions fall back to the clock; see munin.log",
        )
    return CheckResult("m365", "ok", detail)


CHECKS: tuple[Check, ...] = (
    Check("python", "all", check_python),
    Check("ffmpeg", "all", check_ffmpeg),
    Check("pw-record", "linux", check_pw_record),
    Check("pw-dump", "linux", check_pw_dump_tool),
    Check("XDG_RUNTIME_DIR", "all", check_runtime_dir),
    Check("pipewire", "linux", check_pipewire),
    Check("default source", "linux", check_default_source),
    Check("detection", "linux", check_detection),
    Check("omarchy cli", "linux", check_omarchy),
    Check("plugin installed", "linux", check_plugin_installed),
    Check("plugin on bar", "linux", check_plugin_on_bar),
    Check("keybind", "linux", check_keybind),
    Check("systemd unit", "linux", check_unit),
    Check("worker unit", "linux", check_worker_unit),
    Check("daemon", "all", check_daemon),
    Check("data root", "all", check_data_root),
    Check("config", "all", check_config),
    Check("disk space", "all", check_disk),
    Check("spool", "all", check_spool),
    Check("orphan sessions", "all", check_orphans),
    Check("transcription", "all", check_backend),
    Check("export", "all", check_export),
    Check("m365", "all", check_m365),
    Check("platform support", "all", check_platform_support),
)


def run_checks(
    config: Config,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> list[CheckResult]:
    """Tools, Python version, data root, config, disk, PipeWire, detection, plugin,
    units, backends, export. Every check returns a line; none raises."""
    context = DoctorEnv(
        config=config,
        home=home or Path.home(),
        env=dict(env if env is not None else os.environ),
        platform=platform or sys.platform,
    )
    results: list[CheckResult] = []
    for check in CHECKS:
        if check.platform not in ("all", context.platform):
            results.append(
                CheckResult(check.name, "skip", f"{check.platform} only", platform=check.platform)
            )
            continue
        runner: Callable[[DoctorEnv], CheckResult | list[CheckResult]] = check.run  # type: ignore[assignment]
        try:
            outcome = runner(context)
        except Exception as exc:  # noqa: BLE001 - the whole point of a doctor
            results.append(
                CheckResult(check.name, "fail", f"check raised {type(exc).__name__}: {exc}")
            )
            continue
        if isinstance(outcome, list):
            results.extend(outcome)
        else:
            results.append(outcome)
    return results


_MARKS: dict[str, str] = {"ok": "ok", "warn": "warn", "fail": "FAIL", "skip": "skip"}


def format_table(results: Sequence[CheckResult]) -> str:
    name_width = max((len(r.name) for r in results), default=4)
    status_width = max(len(_MARKS[r.status]) for r in results) if results else 4
    lines = [
        f"{r.name.ljust(name_width)}  {_MARKS[r.status].ljust(status_width)}  {r.detail}"
        for r in results
    ]
    failed = sum(1 for r in results if r.status == "fail")
    warned = sum(1 for r in results if r.status == "warn")
    lines.append("")
    lines.append(f"{len(results)} checks, {failed} failed, {warned} warnings")
    return "\n".join(lines)


def main(config: Config, *, as_json: bool = False, **kwargs: object) -> int:
    """Print the table. Exits 5 if any check failed (PoC contracts section 11)."""
    results = run_checks(config, **kwargs)  # type: ignore[arg-type]
    if as_json:
        payload = {
            "munin_version": __version__,
            "platform": sys.platform,
            "checks": [
                {
                    "name": r.name,
                    "status": r.status,
                    "detail": r.detail,
                    "platform": r.platform,
                }
                for r in results
            ],
            "failed": sum(1 for r in results if r.status == "fail"),
            "warned": sum(1 for r in results if r.status == "warn"),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_table(results))
    return 5 if any(r.status == "fail" for r in results) else 0
