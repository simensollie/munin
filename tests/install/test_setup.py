"""``munin setup``: the data root, the config file and the PipeWire questions.

The ten-second test recording is not tested here -- it needs a microphone, a
loudspeaker and a human ear, and faking those would prove nothing. It is a live
check, described as such. What *is* tested is everything that decides what the
recording will be: the config the installer writes, and the source list the
question is built from.

Owner: install workstream.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from munin.config import Config
from munin.setup import (
    DEFAULT_CONFIG_TOML,
    audio_sources,
    default_source_name,
    ensure_home,
    main,
    playback_streams,
    set_config_value,
    write_default_config,
)

PW_DUMP_FIXTURE_PATH = "SHIM_PW_DUMP"


def _load(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


# --- the data root ---------------------------------------------------------


def test_ensure_home_creates_the_layout(tmp_path: Path) -> None:
    home = ensure_home(tmp_path / "munin")
    for name in ("recordings", "inbox", "voices"):
        assert (home / name).is_dir()


def test_ensure_home_is_idempotent(tmp_path: Path) -> None:
    home = ensure_home(tmp_path / "munin")
    (home / "recordings" / "keepme").mkdir()
    ensure_home(home)
    assert (home / "recordings" / "keepme").is_dir()


# --- the config file -------------------------------------------------------


def test_default_config_is_parseable_and_complete(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "munin")
    assert path.name == "config.toml"
    data = _load(path)

    assert data["capture"]["mic_source"] == "default"
    assert data["capture"]["bitrate_kbps"] == 24
    assert data["capture"]["channels"] == 1
    assert data["capture"]["sample_rate"] == 48000
    assert data["capture"]["min_free_mb"] == 2048
    assert data["detection"]["source"] == "plugin"
    assert data["detection"]["grace_seconds"] == 120
    assert data["detection"]["warn_seconds"] == 60
    assert data["detection"]["resume_window_seconds"] == 600
    assert [app["app_id"] for app in data["detection"]["apps"]] == [
        "teams-native",
        "teams-pwa",
        "teams-tab",
    ]
    assert data["transcribe"]["backend"] == "none"
    assert data["idle"]["method"] == "omarchy-stay-awake"
    assert data["notifications"]["glyph"] == "\U000f0ec2"
    # Off in the template: the upload folder stages meeting audio for a third
    # party, so the amendment of 2026-09-18 made it opt-in once, not opt-out.
    assert data["export"]["enabled"] is False
    assert data["export"]["format"] == "opus"
    # Off in the template too: it reads the calendar and chats (spec 7.6, 12).
    assert data["m365"]["enabled"] is False
    assert data["m365"]["tenant_id"] == ""
    # D24: no second-brain copy, so no section configuring where it goes.
    assert set(data) == {
        "munin",
        "paths",
        "capture",
        "detection",
        "transcribe",
        "idle",
        "notifications",
        "export",
        "m365",
    }


def test_default_config_matches_the_dataclass_defaults(tmp_path: Path) -> None:
    """A default that disagrees with :mod:`munin.config` is a trap, not a config."""
    data = _load(write_default_config(tmp_path / "munin"))
    defaults = Config(home=tmp_path)
    assert data["capture"]["bitrate_kbps"] == defaults.capture.bitrate_kbps
    assert data["capture"]["sample_rate"] == defaults.capture.sample_rate
    assert data["capture"]["min_free_mb"] == defaults.capture.min_free_mb
    assert data["detection"]["poll_seconds"] == defaults.detection.poll_seconds
    assert data["detection"]["grace_seconds"] == defaults.detection.grace_seconds
    assert data["transcribe"]["backend"] == defaults.transcribe.backend
    assert data["idle"]["inhibit"] == defaults.idle.inhibit
    assert data["notifications"]["glyph"] == defaults.notifications.glyph
    assert data["paths"]["recordings"] == defaults.paths.recordings


def test_an_existing_config_is_never_clobbered(tmp_path: Path) -> None:
    home = tmp_path / "munin"
    path = write_default_config(home)
    path.write_text('# hand edited\n[capture]\nmic_source = "mine"\n', encoding="utf-8")

    again = write_default_config(home)

    assert again == path
    assert "hand edited" in path.read_text(encoding="utf-8")


def test_overwrite_is_possible_when_asked(tmp_path: Path) -> None:
    home = tmp_path / "munin"
    path = write_default_config(home)
    path.write_text("# hand edited\n", encoding="utf-8")
    write_default_config(home, overwrite=True)
    assert "hand edited" not in path.read_text(encoding="utf-8")


# --- editing one value in place -------------------------------------------


def test_set_config_value_keeps_the_comments(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "munin")
    before = path.read_text(encoding="utf-8")

    changed = set_config_value(path, "capture", "mic_source", "alsa_input.synthetic-mic")

    assert changed is True
    text = path.read_text(encoding="utf-8")
    assert 'mic_source   = "alsa_input.synthetic-mic"' in text
    # Every comment line of the canonical template survives a rewrite: the file
    # is hand-edited, so setup must not reduce it to bare keys. Asserted against
    # the template itself rather than a quoted line, so the two cannot drift.
    comments = [
        line for line in DEFAULT_CONFIG_TOML.splitlines() if line.startswith("#")
    ]
    assert comments, "the canonical template is expected to carry comments"
    for line in comments:
        assert line in text
    assert text.count("[capture]") == before.count("[capture]")
    assert _load(path)["capture"]["mic_source"] == "alsa_input.synthetic-mic"


def test_set_config_value_is_a_no_op_when_unchanged(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "munin")
    assert set_config_value(path, "capture", "mic_source", "default") is False


def test_set_config_value_only_touches_its_own_section(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "munin")
    set_config_value(path, "capture", "mic_source", "alsa_input.synthetic-mic")
    data = _load(path)
    assert data["detection"]["source"] == "plugin"
    assert data["munin"]["home"] == "~/munin"


def test_set_config_value_adds_a_missing_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[capture]\nchannels = 1\n", encoding="utf-8")
    assert set_config_value(path, "capture", "mic_source", "x") is True
    assert _load(path)["capture"]["mic_source"] == "x"


def test_set_config_value_adds_a_missing_section(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[munin]\nhome = \"~/munin\"\n", encoding="utf-8")
    assert set_config_value(path, "capture", "mic_source", "x") is True
    assert _load(path)["capture"]["mic_source"] == "x"


# --- reading PipeWire ------------------------------------------------------


@pytest.fixture()
def pw_objects() -> list[dict]:
    from install.conftest import PW_DUMP_FIXTURE  # type: ignore[import-not-found]

    return json.loads(json.dumps(PW_DUMP_FIXTURE))


def test_audio_sources_puts_the_default_first(pw_objects: list[dict]) -> None:
    sources = audio_sources(pw_objects)
    assert len(sources) == 1
    source = sources[0]
    assert source.name == "alsa_input.synthetic-mic"
    assert source.description == "Synthetic Mono Microphone"
    assert source.serial == "56"
    assert "16000" in source.fmt
    assert source.is_default is True
    assert "(default)" in source.label


def test_default_source_name_reads_the_metadata(pw_objects: list[dict]) -> None:
    assert default_source_name(pw_objects) == "alsa_input.synthetic-mic"


def test_playback_streams_take_their_label_from_the_client(pw_objects: list[dict]) -> None:
    streams = playback_streams(pw_objects)
    assert streams == [("71", "Beacon 365")]


def test_no_sources_is_not_an_error() -> None:
    assert audio_sources([]) == []
    assert playback_streams([]) == []
    assert default_source_name([]) is None


# --- the non-interactive entry point --------------------------------------


def test_write_default_config_mode_asks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "munin"
    monkeypatch.setenv("MUNIN_HOME", str(home))
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("setup asked a question"))

    code = main(Config(home=home), write_config_only=True)

    assert code == 0
    assert (home / "config.toml").is_file()
    assert (home / "recordings").is_dir()
    assert str(home) in capsys.readouterr().out
    _load(home / "config.toml")


def test_non_interactive_mode_asks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "munin"
    monkeypatch.setenv("MUNIN_HOME", str(home))
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("setup asked a question"))

    assert main(Config(home=home), non_interactive=True) == 0
    capsys.readouterr()
    assert (home / "config.toml").is_file()
