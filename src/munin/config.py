"""``~/munin/config.toml``: schema, defaults, loading.

One hand-editable TOML file (plan section 2). Every key has a default and a
missing file is not an error, so a fresh machine records before anyone writes
config. ``$MUNIN_HOME`` overrides ``[munin] home`` for tests and the installer.
Unknown keys are preserved and reported by ``munin doctor`` rather than dropped.

The full schema is section 9 of the PoC contracts.

Owner: spool workstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from munin.detect.base import AppRule

__all__ = [
    "CaptureConfig",
    "Config",
    "DetectionConfig",
    "IdleConfig",
    "NotificationConfig",
    "PathsConfig",
    "TranscribeConfig",
    "DEFAULT_CONFIG_TOML",
    "load",
]

#: Written verbatim by ``munin setup`` when no config exists.
DEFAULT_CONFIG_TOML = ""


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
class Config:
    """The whole file, defaults applied."""

    home: Path
    paths: PathsConfig = field(default_factory=PathsConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    idle: IdleConfig = field(default_factory=IdleConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    pensieve_raw_dir: Path | None = None
    unknown_keys: tuple[str, ...] = ()
    source_path: Path | None = None

    @property
    def app_rules(self) -> list[AppRule]:
        """The detection table in file order, which is the match order."""
        return list(self.detection.apps)


def load(path: Path | None = None) -> Config:
    """Read the config, applying defaults. A missing file yields all defaults."""
    raise NotImplementedError
