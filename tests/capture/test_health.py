"""Microphone health (D28): the ghost-device signature, against real dumps.

The fixtures are a real healthy ``pw-dump`` from 2026-10-14 and two ghosts
derived from it (``tests/fixtures/pw-dump/README.md``). The healthy one matters
as much as the ghosts: its card's ``bluez5.profile`` *property* reads ``off``
while it plays A2DP, so a check that read the property would mark every
healthy headset as broken.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from munin.capture import health, linux
from munin.capture.health import MicHealth, assess

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pw-dump"
NOW = datetime(2026, 10, 14, 13, 1, 23).astimezone()


def dump(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_healthy_bluetooth_is_ok_and_names_the_mic() -> None:
    verdict = assess(dump("healthy-bluetooth"), now=NOW)
    assert verdict.state == "ok"
    assert verdict.reason is None
    assert verdict.label == "Headset"
    assert verdict.to_dict() == {
        "state": "ok",
        "reason": None,
        "label": "Headset",
        "checked_at": NOW.isoformat(timespec="seconds"),
    }


def test_the_bluez5_profile_property_is_not_the_active_profile() -> None:
    # The measured fact the S2 check is built around.
    card = next(o for o in dump("healthy-bluetooth") if o["type"].endswith(":Device"))
    assert card["info"]["props"]["bluez5.profile"] == "off"
    assert card["info"]["params"]["Profile"][0]["name"] == "a2dp-sink"
    assert health.bluez_cards([card])[0]["profile"] == "a2dp-sink"


def test_s3_alone_default_sink_missing() -> None:
    verdict = assess(dump("ghost-s3"), now=NOW)
    assert verdict.state == "degraded"
    assert verdict.reason == "default-sink-missing"
    # The node is gone; the card is not, and names the headset.
    assert verdict.label == "Headset"
    assert verdict.sentence() == "Headset is set as default but no longer exists"


def test_s2_reported_first_when_both_hold() -> None:
    objects = dump("ghost-s2")
    assert health.configured_default_sink(objects) == "bluez_output.AA_BB_CC_DD_EE_FF.1"
    verdict = assess(objects, now=NOW)
    assert verdict.state == "degraded"
    assert verdict.reason == "profile-off"
    assert verdict.sentence() == "Headset is connected but has no audio profile"


def test_s2_alone() -> None:
    objects = dump("ghost-s2")
    for obj in objects:
        if obj["type"].endswith(":Metadata"):
            for row in obj["metadata"]:
                if row["key"] == "default.configured.audio.sink":
                    row["value"] = {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo"}
    verdict = assess(objects, now=NOW)
    assert verdict.reason == "profile-off"


def test_a_disconnected_card_at_off_is_not_a_signal() -> None:
    objects = dump("ghost-s2")
    for obj in objects:
        if obj["type"].endswith(":Device"):
            obj["info"]["props"]["api.bluez5.connection"] = "disconnected"
        if obj["type"].endswith(":Metadata"):
            obj["metadata"] = [r for r in obj["metadata"] if "sink" not in r["key"]]
    assert assess(objects).state == "ok"


def test_without_a_profile_param_the_property_stands_in() -> None:
    objects = copy.deepcopy(dump("healthy-bluetooth"))
    for obj in objects:
        if obj["type"].endswith(":Device"):
            del obj["info"]["params"]
    assert assess(objects).reason == "profile-off"


def test_no_dump_is_unknown() -> None:
    verdict = assess(None, now=NOW)
    assert verdict == MicHealth(state="unknown", checked_at=NOW)
    assert verdict.to_dict()["state"] == "unknown"


def test_no_bluetooth_at_all_is_ok() -> None:
    objects = [o for o in dump("healthy-bluetooth") if not o["type"].endswith(":Device")]
    for obj in objects:
        if obj["type"].endswith(":Metadata"):
            obj["metadata"] = [
                {"key": "default.configured.audio.sink",
                 "value": {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo"}},
                {"key": "default.audio.source",
                 "value": {"name": "alsa_input.pci-0000_00_1f.3.analog-stereo"}},
            ]
    verdict = assess(objects)
    assert verdict.state == "ok"
    assert verdict.label == "Built-in Audio Analog Stereo"
    assert health.bluez_cards(objects) == []


def test_reason_text_is_the_spec_wording() -> None:
    assert health.REASON_TEXT == {
        "node-error": "in an error state",
        "profile-off": "connected but has no audio profile",
        "default-sink-missing": "set as default but no longer exists",
    }


def test_the_plugin_carries_the_same_wording() -> None:
    # The panel cannot import Python; its copy of the strings is pinned here.
    model = (Path(__file__).resolve().parents[2] / "plugin" / "local.munin" / "Model.js")
    text = model.read_text(encoding="utf-8")
    for reason, wording in health.REASON_TEXT.items():
        assert f'"{reason}": "{wording}"' in text, reason


def test_a_garbage_dump_does_not_raise() -> None:
    assert assess([None, 3, "x", {"type": "PipeWire:Interface:Node"}]).state == "ok"


# --- the seams -------------------------------------------------------------


def _fake_pw_dump(tmp_path: Path, payload: Any, rc: int = 0) -> str:
    data = tmp_path / "dump.json"
    data.write_text(json.dumps(payload), encoding="utf-8")
    tool = tmp_path / "pw-dump"
    tool.write_text(f"#!/usr/bin/env bash\ncat {data}\nexit {rc}\n", encoding="utf-8")
    tool.chmod(0o755)
    return str(tool)


def test_linux_mic_health_reads_one_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    tool = _fake_pw_dump(tmp_path, dump("ghost-s3"))
    assert linux.mic_health(pw_dump=tool).reason == "default-sink-missing"


def test_linux_mic_health_without_a_runtime_dir_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert linux.mic_health(pw_dump="pw-dump").state == "unknown"


def test_linux_mic_health_with_a_failing_dump_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    tool = _fake_pw_dump(tmp_path, [], rc=1)
    assert linux.mic_health(pw_dump=tool).state == "unknown"
    assert linux.mic_health(pw_dump=str(tmp_path / "absent")).state == "unknown"


def test_check_on_a_platform_without_capture_is_unknown() -> None:
    assert health.check(platform="plan9").state == "unknown"


def test_check_dispatches_to_the_platform_module(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def probe(mic_source: str, *, now: datetime) -> MicHealth:
        seen.append(mic_source)
        return MicHealth(state="degraded", reason="profile-off", label="Headset", checked_at=now)

    monkeypatch.setattr(linux, "mic_health", probe)
    assert health.check("default", platform="linux", now=NOW).reason == "profile-off"
    assert seen == ["default"]


def test_check_swallows_a_probe_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> MicHealth:
        raise RuntimeError("no")

    monkeypatch.setattr(linux, "mic_health", boom)
    assert health.check(platform="linux").state == "unknown"


def test_default_mic_target_warns_and_still_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    tool = _fake_pw_dump(tmp_path, dump("ghost-s3"))
    warnings: list[str] = []
    target = linux.default_mic_target(pw_dump=tool, warnings=warnings)
    # D4: the stale-but-present mic still resolves; the record says why it is
    # suspect.
    assert target.label == "Headset"
    assert warnings == [
        "microphone degraded: Headset is set as default but no longer exists "
        "(default-sink-missing)"
    ]
    # Healthy: nothing appended.
    healthy = _fake_pw_dump(tmp_path, dump("healthy-bluetooth"))
    clean: list[str] = []
    linux.default_mic_target(pw_dump=healthy, warnings=clean)
    assert clean == []
