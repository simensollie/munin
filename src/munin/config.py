"""``~/munin/config.toml``: schema, defaults, loading.

One hand-editable TOML file (plan section 2). Every key has a default and a
missing file is not an error, so a fresh machine records before anyone writes
config. ``$MUNIN_HOME`` overrides ``[munin] home`` for tests and the installer.
Unknown keys are preserved and reported by ``munin doctor`` rather than dropped.

The full schema is section 9 of the PoC contracts.

Owner: spool workstream.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from munin.detect.base import AppRule
from munin.paths import config_file, munin_home

__all__ = [
    "CaptureConfig",
    "Config",
    "ConfigError",
    "DetectionConfig",
    "ExportConfig",
    "IdleConfig",
    "NotificationConfig",
    "PathsConfig",
    "TranscribeConfig",
    "DEFAULT_CONFIG_TOML",
    "DEFAULT_APP_RULES",
    "defaults",
    "load",
    "with_home",
    "write_default_config",
]


class ConfigError(RuntimeError):
    """The config file exists but cannot be believed (bad TOML, wrong type)."""


#: The detection table of the contracts, section 9. Three shapes of the same
#: application: the native client, the installed PWA, and a browser tab. Adding
#: another meeting application is a row here or in the user's file, never code
#: (D13).
DEFAULT_APP_RULES: tuple[AppRule, ...] = (
    # Measured 2026-09-15: teams-for-linux publishes application.name "Chromium"
    # and application.process.binary "teams-for-linux"; the binary is what
    # identifies it, the client name is kept for a build that says "Teams".
    AppRule(
        app_id="teams-native",
        label="Microsoft Teams",
        client_name="Teams",
        binary="teams-for-linux",
    ),
    AppRule(
        app_id="teams-pwa",
        label="Microsoft Teams",
        window_class="chrome-teams.microsoft.com__-Default",
    ),
    AppRule(
        app_id="teams-tab",
        label="Microsoft Teams",
        window_title_contains="Microsoft Teams",
    ),
)

#: Written verbatim by ``munin setup`` when no config exists.
DEFAULT_CONFIG_TOML = """\
# Munin configuration. Every key below is the default -- deleting a key or the
# whole file changes nothing. `munin doctor` reports keys it does not know
# rather than dropping them.
#
# Times are seconds, sizes are MiB, 24-hour clock, ISO 8601 dates.

[munin]
# The data root. $MUNIN_HOME overrides this (the installer and the tests use it).
home = "~/munin"

[paths]
# All relative to `home`.
recordings = "recordings"
inbox      = "inbox"
voices     = "voices"
log        = "munin.log"


[capture]
# "default" follows the PipeWire default source; otherwise a node name or an
# object.serial.
mic_source   = "default"
bitrate_kbps = 24
channels     = 1
sample_rate  = 48000
# Refuse to start a recording below this much free space rather than truncating
# one halfway through (spec 11).
min_free_mb  = 2048
# `munin start` (or the keybind) with no detected call first looks for a live
# call to bind to. If there is none: "system-output" records everything this
# machine plays as the meeting track (and says so in a notification, since that
# can include audio from outside the meeting); "silent" records only you.
adhoc_app_source = "system-output"

[detection]
enabled = true
# plugin | daemon | off.  "plugin" means the Omarchy shell plugin watches
# PipeWire and calls `munin event`; "daemon" polls pw-dump itself.
source                = "plugin"
poll_seconds          = 3
# Auto-stop this long after the meeting application's audio disappears, warning
# at `warn_seconds` into that window.
grace_seconds         = 120
warn_seconds          = 60
# `munin start --resume` reopens the previous session inside this window (D15).
resume_window_seconds = 600

# Detection needs two things at once: one process holding both a playback and a
# capture stream (that is a call), and a match below (that is which application).
[[detection.apps]]
app_id      = "teams-native"
label       = "Microsoft Teams"
client_name = "Teams"
binary      = "teams-for-linux"   # what the native client actually reports

[[detection.apps]]
app_id       = "teams-pwa"
label        = "Microsoft Teams"
window_class = "chrome-teams.microsoft.com__-Default"

[[detection.apps]]
app_id                = "teams-tab"
label                 = "Microsoft Teams"
window_title_contains = "Microsoft Teams"

[transcribe]
# "none" in the proof of concept: sessions are captured and left in the spool as
# pending, with the reason recorded in session.json.
backend  = "none"
fallback = []

[idle]
# Hold off the screensaver and the lock screen while recording.
inhibit = true
method  = "omarchy-stay-awake"

[notifications]
enabled = true
glyph   = "\\U000f0ec2"

[export]
# A copy of the mix outside the session directory, for the manual Plaud upload
# (spec 10). Off by default: that route is opt-in (D11), and the copy leaves
# meeting audio in a folder staged for a third party, under its retention rather
# than yours. Turned on, every finished recording is mixed and copied here
# without being asked -- which is the only thing that makes the folder current.
# `directory` is absolute or ~-relative, unlike [paths], which is under `home`.
# The whole section goes when the local pipeline transcribes (D25).
enabled   = false
directory = "~/plaud-upload"
format    = "opus"
"""


@dataclass(frozen=True)
class PathsConfig:
    recordings: str = "recordings"
    inbox: str = "inbox"
    voices: str = "voices"
    log: str = "munin.log"


@dataclass(frozen=True)
class CaptureConfig:
    mic_source: str = "default"
    bitrate_kbps: int = 24
    channels: int = 1
    sample_rate: int = 48000
    min_free_mb: int = 2048
    # What the app track holds for an ad-hoc start when no call was detected:
    # "system-output" (the machine's whole output mix) or "silent".
    adhoc_app_source: str = "system-output"


@dataclass(frozen=True)
class DetectionConfig:
    enabled: bool = True
    source: str = "plugin"  # plugin | daemon | off
    poll_seconds: int = 3
    grace_seconds: int = 120
    warn_seconds: int = 60
    resume_window_seconds: int = 600
    apps: tuple[AppRule, ...] = ()


@dataclass(frozen=True)
class TranscribeConfig:
    backend: str = "none"
    fallback: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdleConfig:
    inhibit: bool = True
    method: str = "omarchy-stay-awake"


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool = True
    glyph: str = "\U000f0ec2"


@dataclass(frozen=True)
class ExportConfig:
    enabled: bool = False
    directory: str = "~/plaud-upload"
    format: str = "opus"


@dataclass(frozen=True)
class Config:
    """The whole file, defaults applied."""

    home: Path
    paths: PathsConfig = field(default_factory=PathsConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    idle: IdleConfig = field(default_factory=IdleConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    unknown_keys: tuple[str, ...] = ()
    source_path: Path | None = None

    @property
    def app_rules(self) -> list[AppRule]:
        """The detection table in file order, which is the match order."""
        return list(self.detection.apps)

    @property
    def recordings_dir(self) -> Path:
        return self.home / self.paths.recordings

    @property
    def inbox_path(self) -> Path:
        return self.home / self.paths.inbox

    @property
    def voices_path(self) -> Path:
        return self.home / self.paths.voices

    @property
    def log_path(self) -> Path:
        return self.home / self.paths.log

    @property
    def export_dir(self) -> Path:
        """Outside ``home``, unlike every path above: the upload folder is a
        staging area the user shares with a third party, not munin's own store.
        """
        return Path(self.export.directory).expanduser()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

_SCALAR_FIELDS: dict[str, dict[str, type]] = {
    "paths": {"recordings": str, "inbox": str, "voices": str, "log": str},
    "capture": {
        "mic_source": str,
        "bitrate_kbps": int,
        "channels": int,
        "sample_rate": int,
        "min_free_mb": int,
        "adhoc_app_source": str,
    },
    "detection": {
        "enabled": bool,
        "source": str,
        "poll_seconds": int,
        "grace_seconds": int,
        "warn_seconds": int,
        "resume_window_seconds": int,
    },
    "transcribe": {"backend": str},
    "idle": {"inhibit": bool, "method": str},
    "notifications": {"enabled": bool, "glyph": str},
    "export": {"enabled": bool, "directory": str, "format": str},
}

_DETECTION_SOURCES = ("plugin", "daemon", "off")

# For an ad-hoc `munin start` with no detected call: record the whole output
# mix, or leave the app track silent.
_ADHOC_APP_SOURCES = ("system-output", "silent")

# Both are what Plaud's importer accepts (spec 10, Appendix A). Duplicated from
# mixdown.FORMATS rather than imported, to keep config free of pipeline imports;
# a test pins the two together.
_EXPORT_FORMATS = ("opus", "mp3")

_APP_RULE_KEYS = (
    "app_id",
    "label",
    "client_name",
    "binary",
    "window_class",
    "window_title_contains",
)


def _typed(section: str, key: str, value: Any, expected: type) -> Any:
    """One value, checked. ``bool`` is a subclass of ``int``, so order matters."""
    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"[{section}] {key} must be true or false, got {value!r}")
        return value
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"[{section}] {key} must be a whole number, got {value!r}")
        return value
    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"[{section}] {key} must be a string, got {value!r}")
        return value
    raise AssertionError(f"unhandled expected type {expected!r}")


def _section(
    raw: dict[str, Any],
    name: str,
    unknown: list[str],
    *,
    ignore: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Pull one table's known scalars out, recording the rest as unknown keys."""
    table = raw.get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}] must be a table")
    schema = _SCALAR_FIELDS[name]
    known: dict[str, Any] = {}
    for key, value in table.items():
        if key in schema:
            known[key] = _typed(name, key, value, schema[key])
        elif key not in ignore:
            unknown.append(f"{name}.{key}")
    return known


def _app_rules(raw: dict[str, Any], unknown: list[str]) -> tuple[AppRule, ...]:
    """``[[detection.apps]]`` in file order, which is the match order."""
    detection = raw.get("detection", {})
    rows = detection.get("apps") if isinstance(detection, dict) else None
    if rows is None:
        return DEFAULT_APP_RULES
    if not isinstance(rows, list):
        raise ConfigError("[[detection.apps]] must be an array of tables")
    rules: list[AppRule] = []
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ConfigError(f"[[detection.apps]] row {position} must be a table")
        for key in row:
            if key not in _APP_RULE_KEYS:
                unknown.append(f"detection.apps[{position}].{key}")
        for required in ("app_id", "label"):
            if not isinstance(row.get(required), str) or not row.get(required):
                raise ConfigError(
                    f"[[detection.apps]] row {position} needs a {required} string"
                )
        fields: dict[str, Any] = {}
        for key in _APP_RULE_KEYS[2:]:
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                raise ConfigError(
                    f"[[detection.apps]] row {position}: {key} must be a string"
                )
            fields[key] = value
        rules.append(AppRule(app_id=row["app_id"], label=row["label"], **fields))
    return tuple(rules)


def _fallback_chain(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("transcribe", {}).get("fallback", [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError("[transcribe] fallback must be a list of backend names")
    return tuple(value)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path} could not be read: {exc}") from exc


def load(path: Path | None = None) -> Config:
    """Read the config, applying defaults. A missing file yields all defaults.

    Home is resolved before the file is read, because the file lives under it:
    ``$MUNIN_HOME`` first, then ``[munin] home`` from whatever file was found,
    then ``~/munin``.
    """
    probe_home = munin_home()
    path = Path(path).expanduser() if path is not None else config_file(probe_home)

    raw: dict[str, Any] = {}
    source_path: Path | None = None
    if path.exists():
        raw = _read_toml(path)
        source_path = path

    unknown: list[str] = []

    munin_table = raw.get("munin", {})
    if not isinstance(munin_table, dict):
        raise ConfigError("[munin] must be a table")
    configured_home = munin_table.get("home")
    if configured_home is not None and not isinstance(configured_home, str):
        raise ConfigError("[munin] home must be a string")
    for key in munin_table:
        if key != "home":
            unknown.append(f"munin.{key}")
    home = munin_home(configured_home)

    paths_known = _section(raw, "paths", unknown)
    capture_known = _section(raw, "capture", unknown)
    # `apps` is the table array below, not an unknown scalar.
    detection_known = _section(
        raw, "detection", unknown, ignore=frozenset({"apps"})
    )
    transcribe_known = _section(
        raw, "transcribe", unknown, ignore=frozenset({"fallback"})
    )
    idle_known = _section(raw, "idle", unknown)
    notifications_known = _section(raw, "notifications", unknown)
    export_known = _section(raw, "export", unknown)

    for name in raw:
        if name not in {
            "munin",
            "paths",
            "capture",
            "detection",
            "transcribe",
            "idle",
            "notifications",
            "export",
        }:
            unknown.append(name)

    detection = DetectionConfig(**detection_known, apps=_app_rules(raw, unknown))
    if detection.source not in _DETECTION_SOURCES:
        raise ConfigError(
            "[detection] source must be one of "
            + ", ".join(_DETECTION_SOURCES)
            + f", got {detection.source!r}"
        )
    if detection.warn_seconds > detection.grace_seconds:
        raise ConfigError(
            "[detection] warn_seconds must fall inside grace_seconds "
            f"({detection.warn_seconds} > {detection.grace_seconds})"
        )

    capture = CaptureConfig(**capture_known)
    for name, value in (
        ("bitrate_kbps", capture.bitrate_kbps),
        ("channels", capture.channels),
        ("sample_rate", capture.sample_rate),
    ):
        if value <= 0:
            raise ConfigError(f"[capture] {name} must be positive, got {value}")
    if capture.min_free_mb < 0:
        raise ConfigError("[capture] min_free_mb must not be negative")
    if capture.adhoc_app_source not in _ADHOC_APP_SOURCES:
        raise ConfigError(
            "[capture] adhoc_app_source must be one of "
            + ", ".join(_ADHOC_APP_SOURCES)
            + f", got {capture.adhoc_app_source!r}"
        )

    export = ExportConfig(**export_known)
    if export.format not in _EXPORT_FORMATS:
        raise ConfigError(
            "[export] format must be one of "
            + ", ".join(_EXPORT_FORMATS)
            + f", got {export.format!r}"
        )
    if export.enabled and not export.directory.strip():
        raise ConfigError("[export] directory must not be empty when enabled")

    return Config(
        home=home,
        paths=PathsConfig(**paths_known),
        capture=capture,
        detection=detection,
        transcribe=TranscribeConfig(
            **transcribe_known, fallback=_fallback_chain(raw)
        ),
        idle=IdleConfig(**idle_known),
        notifications=NotificationConfig(**notifications_known),
        export=export,
        unknown_keys=tuple(unknown),
        source_path=source_path,
    )


def defaults(home: Path | None = None) -> Config:
    """A Config with nothing read from disk. Useful in tests and in ``setup``."""
    return Config(
        home=home if home is not None else munin_home(),
        detection=DetectionConfig(apps=DEFAULT_APP_RULES),
    )


def write_default_config(path: Path | None = None, *, overwrite: bool = False) -> Path:
    """Write the commented template. Refuses to clobber an existing file.

    An existing file is the user's, not ours: ``munin setup`` runs again on every
    reinstall and must not quietly replace hand-edited settings.
    """
    target = Path(path).expanduser() if path is not None else config_file(munin_home())
    if target.exists() and not overwrite:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    tmp.replace(target)
    return target


def with_home(config: Config, home: Path) -> Config:
    """A copy rooted somewhere else. The installer points at a staging root."""
    return replace(config, home=home)
