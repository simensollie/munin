"""``config.toml``: defaults, merging, unknown keys, the detection table.

Every key has a default and a missing file is not an error, so a fresh machine
records before anyone writes config. A key we do not recognise is kept and
reported, never dropped -- a silently ignored setting is worse than a rejected
one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from munin import config as config_module
from munin.config import Config, ConfigError, load, write_default_config
from munin.detect.base import AppRule


def write(home: Path, text: str) -> Path:
    path = home / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


# -- defaults ---------------------------------------------------------------


def test_a_missing_file_is_not_an_error(munin_home: Path) -> None:
    cfg = load()
    assert isinstance(cfg, Config)
    assert cfg.source_path is None
    assert cfg.home == munin_home
    assert cfg.capture.mic_source == "default"
    assert cfg.capture.bitrate_kbps == 24
    assert cfg.capture.channels == 1
    assert cfg.capture.sample_rate == 48000
    assert cfg.capture.min_free_mb == 2048
    assert cfg.detection.enabled is True
    assert cfg.detection.source == "plugin"
    assert cfg.detection.poll_seconds == 3
    assert cfg.detection.grace_seconds == 120
    assert cfg.detection.warn_seconds == 60
    assert cfg.detection.resume_window_seconds == 600
    assert cfg.transcribe.backend == "none"
    assert cfg.transcribe.fallback == ()
    assert cfg.idle.inhibit is True
    assert cfg.idle.method == "omarchy-stay-awake"
    assert cfg.notifications.enabled is True
    assert cfg.notifications.glyph == "\U000f0ec2"
    assert cfg.pensieve_raw_dir == Path("~/pensieve/raw").expanduser()
    assert cfg.unknown_keys == ()


def test_the_default_detection_table_is_the_three_teams_shapes(
    munin_home: Path,
) -> None:
    """Native client, installed PWA, browser tab -- in match order (spec 6.3)."""
    rules = load().app_rules
    assert [r.app_id for r in rules] == ["teams-native", "teams-pwa", "teams-tab"]
    assert rules[0].client_name == "Teams"
    assert rules[1].window_class == "chrome-teams.microsoft.com__-Default"
    assert rules[2].window_title_contains == "Microsoft Teams"
    assert all(isinstance(rule, AppRule) for rule in rules)


def test_paths_are_relative_to_home(munin_home: Path) -> None:
    cfg = load()
    assert cfg.recordings_dir == munin_home / "recordings"
    assert cfg.inbox_path == munin_home / "inbox"
    assert cfg.voices_path == munin_home / "voices"
    assert cfg.log_path == munin_home / "munin.log"


# -- merging ----------------------------------------------------------------


def test_a_partial_file_changes_only_what_it_names(munin_home: Path) -> None:
    write(
        munin_home,
        """
        [capture]
        bitrate_kbps = 32

        [detection]
        source = "daemon"
        """,
    )
    cfg = load()
    assert cfg.capture.bitrate_kbps == 32
    # Untouched keys keep their documented defaults.
    assert cfg.capture.channels == 1
    assert cfg.capture.min_free_mb == 2048
    assert cfg.detection.source == "daemon"
    assert cfg.detection.grace_seconds == 120
    assert cfg.source_path == munin_home / "config.toml"


def test_the_detection_table_replaces_the_default_in_file_order(
    munin_home: Path,
) -> None:
    write(
        munin_home,
        """
        [[detection.apps]]
        app_id = "beacon-365"
        label = "Beacon 365"
        client_name = "Beacon"

        [[detection.apps]]
        app_id = "beacon-tab"
        label = "Beacon 365"
        window_title_contains = "Beacon 365"
        """,
    )
    rules = load().app_rules
    assert [r.app_id for r in rules] == ["beacon-365", "beacon-tab"]
    assert rules[0].window_class is None


def test_munin_home_beats_the_configured_home(
    munin_home: Path, tmp_path: Path
) -> None:
    write(munin_home, '[munin]\nhome = "/definitely/not/here"\n')
    assert load().home == munin_home


def test_the_configured_home_is_used_when_the_variable_is_unset(
    munin_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    path = write(munin_home, f'[munin]\nhome = "{elsewhere}"\n')
    monkeypatch.delenv("MUNIN_HOME", raising=False)
    assert load(path).home == elsewhere


def test_pensieve_raw_dir_expands(munin_home: Path) -> None:
    write(munin_home, '[pensieve]\nraw_dir = "~/pensieve/raw"\n')
    cfg = load()
    assert cfg.pensieve_raw_dir == Path("~/pensieve/raw").expanduser()
    assert "~" not in str(cfg.pensieve_raw_dir)


def test_a_fallback_chain_is_read_in_order(munin_home: Path) -> None:
    write(munin_home, '[transcribe]\nbackend = "local"\nfallback = ["ssh", "api"]\n')
    cfg = load()
    assert cfg.transcribe.backend == "local"
    assert cfg.transcribe.fallback == ("ssh", "api")


# -- unknown keys -----------------------------------------------------------


def test_unknown_keys_are_reported_not_dropped(munin_home: Path) -> None:
    write(
        munin_home,
        """
        [capture]
        bitrate_kbps = 24
        mic_gain = 3

        [telemetry]
        enabled = true

        [[detection.apps]]
        app_id = "teams-tab"
        label = "Microsoft Teams"
        window_title_contains = "Microsoft Teams"
        colour = "blue"
        """,
    )
    unknown = load().unknown_keys
    assert "capture.mic_gain" in unknown
    assert "telemetry" in unknown
    assert "detection.apps[1].colour" in unknown
    # Known structure is never reported.
    assert not any(key.startswith("detection.apps") and key.endswith("app_id")
                   for key in unknown)
    assert "detection.apps" not in unknown
    assert "transcribe.fallback" not in unknown


# -- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[capture]\nbitrate_kbps = "loud"\n', "whole number"),
        ("[capture]\nbitrate_kbps = true\n", "whole number"),
        ("[capture]\nmic_source = 3\n", "string"),
        ('[detection]\nenabled = "yes"\n', "true or false"),
        ('[detection]\nsource = "telepathy"\n', "source must be one of"),
        ("[detection]\nwarn_seconds = 300\n", "inside grace_seconds"),
        ("[capture]\nsample_rate = 0\n", "must be positive"),
        ('[capture]\nadhoc_app_source = "everything"\n', "adhoc_app_source must be one of"),
        ('[transcribe]\nfallback = "ssh"\n', "list of backend names"),
        ("[[detection.apps]]\nlabel = \"x\"\n", "needs a app_id string"),
    ],
)
def test_a_wrong_value_is_refused_with_the_key_named(
    munin_home: Path, body: str, message: str
) -> None:
    write(munin_home, body)
    with pytest.raises(ConfigError, match=message):
        load()


def test_broken_toml_names_the_file(munin_home: Path) -> None:
    write(munin_home, "[capture\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load()


# -- the template -----------------------------------------------------------


def test_the_written_template_parses_back_to_the_defaults(munin_home: Path) -> None:
    path = write_default_config()
    assert path == munin_home / "config.toml"
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["munin"]["home"] == "~/munin"
    assert parsed["notifications"]["glyph"] == "\U000f0ec2"

    cfg = load()
    fresh = config_module.defaults(munin_home)
    assert cfg.capture == fresh.capture
    assert cfg.detection == fresh.detection
    assert cfg.transcribe == fresh.transcribe
    assert cfg.idle == fresh.idle
    assert cfg.notifications == fresh.notifications
    assert cfg.paths == fresh.paths
    assert cfg.app_rules == fresh.app_rules
    assert cfg.unknown_keys == ()


def test_the_template_never_clobbers_a_hand_edited_file(munin_home: Path) -> None:
    path = write(munin_home, "[capture]\nbitrate_kbps = 64\n")
    write_default_config()
    assert path.read_text(encoding="utf-8") == "[capture]\nbitrate_kbps = 64\n"
    assert load().capture.bitrate_kbps == 64

    write_default_config(overwrite=True)
    assert load().capture.bitrate_kbps == 24


def test_the_template_is_commented(munin_home: Path) -> None:
    """A user opening this file should not need the spec to change one setting."""
    text = config_module.DEFAULT_CONFIG_TOML
    assert text.count("#") > 10
    assert "min_free_mb" in text
    assert "resume_window_seconds" in text


def test_adhoc_app_source_defaults_to_the_output_mix(munin_home: Path) -> None:
    write(munin_home, "")
    assert load().capture.adhoc_app_source == "system-output"
    write(munin_home, '[capture]\nadhoc_app_source = "silent"\n')
    assert load().capture.adhoc_app_source == "silent"
