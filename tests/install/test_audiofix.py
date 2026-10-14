"""``bin/munin-audiofix`` (D28) against stand-in tools that log their argv.

Same idea as the installer tests: the script is a sequence of calls to other
people's tools, so the honest test replaces each with a shim that records what
it was asked and answers like the real one would. Nothing here touches the real
WirePlumber, bluetoothctl or munin-rec.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "bin" / "munin-audiofix"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "pw-dump" / "healthy-bluetooth.json"

MAC = "AA:BB:CC:DD:EE:FF"
SINK = "bluez_output.AA_BB_CC_DD_EE_FF.1"

# Paired devices come from SHIM_BT_PAIRED ("ADDR Name" per line); a device is
# connected when its address is in SHIM_BT_CONNECTED.
BLUETOOTHCTL = """#!/usr/bin/env bash
printf 'bluetoothctl %s\\n' "$*" >>"$SHIM_LOG"
case "${1:-}" in
  devices)
    while IFS= read -r line; do
      [[ -n $line ]] && printf 'Device %s\\n' "$line"
    done <<<"${SHIM_BT_PAIRED:-}"
    ;;
  info)
    printf 'Device %s\\n\\tUUID: Audio Sink (0000110b-0000-1000-8000-00805f9b34fb)\\n' "$2"
    if [[ " ${SHIM_BT_CONNECTED:-} " == *" $2 "* ]]; then
      printf '\\tConnected: yes\\n'
    else
      printf '\\tConnected: no\\n'
    fi
    ;;
esac
exit 0
"""

PACTL = """#!/usr/bin/env bash
printf 'pactl %s\\n' "$*" >>"$SHIM_LOG"
case "$*" in
  "list cards short") printf '74\\tbluez_card.AA_BB_CC_DD_EE_FF\\tmodule-bluez5-device.c\\n' ;;
  "list sinks short")
    printf '47\\talsa_output.pci-0000_00_1f.3.analog-stereo\\tPipeWire\\ts32le 2ch 48000Hz\\tSUSPENDED\\n'
    printf '131\\tbluez_output.AA_BB_CC_DD_EE_FF.1\\tPipeWire\\ts16le 2ch 48000Hz\\tSUSPENDED\\n'
    ;;
  "list sources short") printf '175\\tbluez_input.AA:BB:CC:DD:EE:FF\\tPipeWire\\ts16le 1ch 16000Hz\\tSUSPENDED\\n' ;;
  "get-default-sink") printf '%s\\n' "${SHIM_DEFAULT_SINK:-bluez_output.AA_BB_CC_DD_EE_FF.1}" ;;
  "list cards") printf 'Card #74\\n\\tName: bluez_card.AA_BB_CC_DD_EE_FF\\n' ;;
esac
exit 0
"""

WPCTL = """#!/usr/bin/env bash
printf 'wpctl %s\\n' "$*" >>"$SHIM_LOG"
case "${1:-}" in
  status) printf 'Audio\\n Sinks:\\n  *  131. Headset [vol: 0.21]\\n Sources:\\nSettings\\n Default Configured Devices:\\n  0. Audio/Sink    bluez_output.AA_BB_CC_DD_EE_FF.1\\n' ;;
  get-volume) printf 'Volume: %s\\n' "${SHIM_VOLUME:-0.21}" ;;
esac
exit 0
"""

PW_DUMP = """#!/usr/bin/env bash
printf 'pw-dump %s\\n' "$*" >>"$SHIM_LOG"
cat "$SHIM_PW_DUMP"
"""

MUNIN = """#!/usr/bin/env bash
printf 'munin %s\\n' "$*" >>"$SHIM_LOG"
if [[ ${1:-} == status ]]; then exit "${SHIM_MUNIN_STATUS:-0}"; fi
exit 0
"""

LOGGING = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >>"$SHIM_LOG"
exit 0
"""


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass
class Box:
    env: dict[str, str]
    log: Path
    cache: Path

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )

    @property
    def calls(self) -> list[str]:
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def called(self, fragment: str) -> bool:
        return any(fragment in line for line in self.calls)

    @property
    def snapshots(self) -> list[Path]:
        root = self.cache / "munin" / "audiofix"
        return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


@pytest.fixture()
def box(tmp_path: Path) -> Box:
    shim = tmp_path / "shim"
    shim.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    _exe(shim / "bluetoothctl", BLUETOOTHCTL)
    _exe(shim / "pactl", PACTL)
    _exe(shim / "wpctl", WPCTL)
    _exe(shim / "pw-dump", PW_DUMP)
    _exe(shim / "munin", MUNIN)
    for name in ("systemctl", "journalctl", "sudo"):
        _exe(shim / name, LOGGING)
    runtime = tmp_path / "run"
    runtime.mkdir()
    cache = tmp_path / "cache"
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path / "home"),
            "XDG_CACHE_HOME": str(cache),
            "XDG_RUNTIME_DIR": str(runtime),
            "SHIM_LOG": str(log),
            "SHIM_PW_DUMP": str(FIXTURE),
            "SHIM_BT_PAIRED": f"{MAC} Headset",
            "SHIM_BT_CONNECTED": MAC,
        }
    )
    return Box(env=env, log=log, cache=cache)


MUTATIONS = (
    "systemctl --user restart",
    "bluetoothctl connect",
    "bluetoothctl disconnect",
    "pactl set-default",
    "wpctl set-",
    "munin event",
)


def test_bash_syntax_is_clean() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_the_script_is_executable_and_named_for_munin() -> None:
    # D12: munin-, never omarchy-.
    assert SCRIPT.name.startswith("munin-")
    assert os.access(SCRIPT, os.X_OK)


def test_why_comes_before_any_code_and_it_never_sudos() -> None:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    first_code = next(i for i, line in enumerate(lines[1:], 1) if line and not line.startswith("#"))
    assert lines[first_code] == "set -euo pipefail"
    assert first_code > 20, "the why-comment block comes first"
    code = [line for line in lines if not line.lstrip().startswith("#")]
    assert not any("sudo" in line for line in code)


def test_dry_run_snapshots_and_changes_nothing(box: Box) -> None:
    proc = box.run("-n")
    assert proc.returncode == 0, proc.stderr
    assert "Target: Headset (AA:BB:CC:DD:EE:FF)" in proc.stdout
    assert "would run: systemctl --user restart wireplumber" in proc.stdout
    assert f"would run: pactl set-default-sink {SINK}" in proc.stdout
    for fragment in MUTATIONS:
        assert not box.called(fragment), fragment
    # Step 0 is read-only and runs in a dry run too.
    [snap] = box.snapshots
    for name in ("pw-dump.json", "wpctl-status.txt", "pactl-cards.txt",
                 "bluetoothctl-info.txt", "wireplumber.log"):
        assert (snap / name).is_file(), name
    assert (snap / "pw-dump.json").read_text(encoding="utf-8") == FIXTURE.read_text(
        encoding="utf-8"
    )


def test_a_repair_runs_the_steps_in_order_and_tells_the_daemon(box: Box) -> None:
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = box.calls

    def index(fragment: str) -> int:
        return next(i for i, line in enumerate(calls) if fragment in line)

    order = [
        index("pw-dump"),
        index("systemctl --user restart wireplumber"),
        index(f"bluetoothctl disconnect {MAC}"),
        index(f"bluetoothctl connect {MAC}"),
        index(f"pactl set-default-sink {SINK}"),
        index(f"pactl set-default-source bluez_input.{MAC}"),
        index("wpctl set-mute @DEFAULT_AUDIO_SINK@ 0"),
        index("munin event audio-repaired"),
    ]
    assert order == sorted(order)
    assert not box.called("sudo")
    # Volume was not 0, so it is left alone.
    assert not box.called("wpctl set-volume")


def test_a_muted_to_zero_sink_is_raised_to_half(box: Box) -> None:
    box.env["SHIM_VOLUME"] = "0.00"
    assert box.run().returncode == 0
    assert box.called("wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.5")


def test_no_daemon_is_not_a_failure(box: Box) -> None:
    box.env["SHIM_MUNIN_STATUS"] = "3"
    proc = box.run()
    assert proc.returncode == 0, proc.stderr
    assert not box.called("munin event")


def test_a_disconnected_headset_is_connected_not_bounced(box: Box) -> None:
    box.env["SHIM_BT_CONNECTED"] = ""
    assert box.run().returncode == 0
    assert box.called(f"bluetoothctl connect {MAC}")
    assert not box.called("bluetoothctl disconnect")


def test_a_repair_that_did_not_take_exits_one_and_tells_nobody(box: Box) -> None:
    box.env["SHIM_DEFAULT_SINK"] = "alsa_output.pci-0000_00_1f.3.analog-stereo"
    proc = box.run()
    assert proc.returncode == 1
    assert "did not take" in proc.stderr
    assert not box.called("munin event")


def test_an_unknown_device_exits_one_after_the_snapshot(box: Box) -> None:
    proc = box.run("speakerphone")
    assert proc.returncode == 1
    assert "no paired audio device matches 'speakerphone'" in proc.stderr
    assert len(box.snapshots) == 1
    assert not box.called("systemctl --user restart")


def test_a_device_can_be_named_by_part_of_its_name_or_address(box: Box) -> None:
    box.env["SHIM_BT_PAIRED"] = f"{MAC} Headset\n11:22:33:44:55:66 Speaker"
    proc = box.run("-n", "headset")
    assert "Target: Headset (AA:BB:CC:DD:EE:FF)" in proc.stdout
    proc = box.run("-n", "11:22:33:44:55:66")
    assert "Target: Speaker (11:22:33:44:55:66)" in proc.stdout


def test_several_paired_and_none_connected_lists_them(box: Box) -> None:
    box.env["SHIM_BT_PAIRED"] = f"{MAC} Headset\n11:22:33:44:55:66 Speaker"
    box.env["SHIM_BT_CONNECTED"] = ""
    proc = box.run()
    assert proc.returncode == 1
    assert 'munin-audiofix "Headset"' in proc.stderr
    assert 'munin-audiofix "Speaker"' in proc.stderr
    assert not box.called("systemctl --user restart")


def test_only_the_last_ten_snapshots_are_kept(box: Box) -> None:
    root = box.cache / "munin" / "audiofix"
    for day in range(1, 13):
        (root / f"202610{day:02d}T000000").mkdir(parents=True)
    assert box.run("-n").returncode == 0
    kept = [p.name for p in box.snapshots]
    assert len(kept) == 10
    assert "20261001T000000" not in kept and "20261003T000000" not in kept


def test_an_unknown_option_is_a_usage_error(box: Box) -> None:
    assert box.run("--frobnicate").returncode == 2
