"""``install.sh`` against a synthetic machine.

What is asserted is what the installer *touched*, not what it printed, except
where the printing is the point: the keybinding file is a symlink into somebody
else's git repository, and the installer promising out loud that it is about to
edit that file is a requirement, not decoration.

Owner: install workstream.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # the fixture itself comes from tests/install/conftest.py
    from install.conftest import FakeMachine

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"

MARK_BEGIN = "-- >>> munin (managed by munin install.sh) >>>"
MARK_END = "-- <<< munin (managed by munin install.sh) <<<"
KEYBIND_LINE = 'o.bind("SUPER + SHIFT + R", "Record meeting", "munin toggle")'


def test_bash_syntax_is_clean() -> None:
    assert subprocess.run(["bash", "-n", str(INSTALL_SH)]).returncode == 0


def test_shellcheck_if_present() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed on this machine")
    proc = subprocess.run(
        ["shellcheck", "--severity=warning", str(INSTALL_SH)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_usage_error_exits_2(machine: FakeMachine) -> None:
    machine.run("--nonsense", expect_rc=2)


# --- dry run ---------------------------------------------------------------


def test_dry_run_changes_nothing(machine: FakeMachine) -> None:
    proc = machine.run("--dry-run")

    assert "would run:" in proc.stdout
    assert f"would run: python3 -m venv {machine.venv}" in proc.stdout
    assert "would run: cp -R" in proc.stdout
    assert "would run: omarchy plugin enable local.munin" in proc.stdout
    assert "would run: omarchy bar put local.munin --before omarchy.tray" in proc.stdout
    assert f"would append to: {machine.bindings_link}" in proc.stdout
    assert "would run: systemctl --user daemon-reload" in proc.stdout

    assert not (machine.home / ".local").exists()
    assert not machine.plugin_dir.exists()
    assert not machine.unit.exists()
    assert not machine.data_home.exists()
    assert machine.bindings_target.read_text(encoding="utf-8").count("o.bind") == 1
    assert not Path(str(machine.bindings_target) + ".munin-bak").exists()
    # Read-only inspection is still allowed to happen in a dry run.
    assert not machine.called("omarchy plugin enable")


# --- the seven steps -------------------------------------------------------


def test_install_performs_every_step(machine: FakeMachine) -> None:
    proc = machine.run()
    out = proc.stdout

    # 1: tool checks
    assert "Checking required tools" in out

    # 2: venv and entry points
    assert machine.venv.joinpath("bin", "munin").exists()
    assert machine.called("pip install --quiet --editable")
    for script in ("munin", "munin-rec", "munin-work"):
        link = machine.bin_dir / script
        assert link.is_symlink()
        assert os.readlink(link) == str(machine.venv / "bin" / script)

    # 3: plugin copied and validated where it will run
    assert (machine.plugin_dir / "manifest.json").is_file()
    assert machine.called(f"omarchy plugin validate {machine.plugin_dir}")

    # 4: enabled and placed on the bar
    assert machine.called("omarchy-shell shell rescanPlugins")
    assert machine.called("omarchy plugin enable local.munin")
    assert machine.called("omarchy bar put local.munin --before omarchy.tray")
    layout = json.loads(machine.shell_json.read_text(encoding="utf-8"))["bar"]["layout"]
    right = [w["id"] for w in layout["right"]]
    assert right.index("local.munin") < right.index("omarchy.tray")

    # 5: keybind, backed up, appended through the symlink, and said out loud
    backup = Path(str(machine.bindings_target) + ".munin-bak")
    assert backup.is_file()
    assert MARK_BEGIN not in backup.read_text(encoding="utf-8")
    text = machine.bindings_target.read_text(encoding="utf-8")
    assert text.count(MARK_BEGIN) == 1
    assert KEYBIND_LINE in text
    assert "hl.unbind" not in text  # the key is unbound in the Omarchy defaults
    assert str(machine.bindings_link) in out
    assert str(machine.bindings_target) in out
    assert "SYMLINK" in out
    assert "tracked in another repository" in out
    assert machine.called("hyprctl reload")
    assert machine.called("hyprctl configerrors")

    # 6: unit copied, daemon-reloaded, and NOT enabled
    assert machine.unit.is_file()
    assert "ExecStart=%h/.local/bin/munin-rec" in machine.unit.read_text(encoding="utf-8")
    assert machine.called("systemctl --user daemon-reload")
    assert not machine.called("systemctl --user enable")
    assert "systemctl --user enable --now munin.service" in out

    # 7: data root and config
    for name in ("recordings", "inbox", "voices"):
        assert (machine.data_home / name).is_dir()
    assert (machine.data_home / "config.toml").is_file()
    assert machine.called("munin setup --write-default-config")


def test_enable_flag_runs_systemctl_enable(machine: FakeMachine) -> None:
    machine.run("--enable")
    assert machine.called("systemctl --user enable --now munin.service")


def test_unbind_is_emitted_when_omarchy_owns_the_key(machine: FakeMachine, tmp_path: Path) -> None:
    defaults = tmp_path / "omarchy-defaults"
    defaults.mkdir()
    (defaults / "applications.lua").write_text(
        'o.bind("SUPER + SHIFT + R", "Something else", "other")\n', encoding="utf-8"
    )
    machine.env["OMARCHY_DEFAULT_BINDINGS"] = str(defaults)

    machine.run()

    text = machine.bindings_target.read_text(encoding="utf-8")
    assert 'hl.unbind("SUPER + SHIFT + R")' in text
    assert text.index('hl.unbind("SUPER + SHIFT + R")') < text.index(KEYBIND_LINE)


def test_install_is_idempotent(machine: FakeMachine) -> None:
    machine.run()
    config = machine.data_home / "config.toml"
    config.write_text("# hand edited, do not clobber\n", encoding="utf-8")
    backup_before = Path(str(machine.bindings_target) + ".munin-bak").read_text(encoding="utf-8")
    machine.clear_log()

    second = machine.run()

    text = machine.bindings_target.read_text(encoding="utf-8")
    assert text.count(MARK_BEGIN) == 1
    assert text.count(KEYBIND_LINE) == 1
    assert "already present" in second.stdout
    assert Path(str(machine.bindings_target) + ".munin-bak").read_text(encoding="utf-8") == backup_before
    assert config.read_text(encoding="utf-8") == "# hand edited, do not clobber\n"
    assert not machine.called("munin setup")
    # Already on the bar: placed once, not twice.
    assert not machine.called("omarchy bar put")
    layout = json.loads(machine.shell_json.read_text(encoding="utf-8"))["bar"]["layout"]
    assert [w["id"] for w in layout["right"]].count("local.munin") == 1


def test_existing_foreign_binding_is_left_alone(machine: FakeMachine) -> None:
    machine.bindings_target.write_text(
        'o.bind("SUPER + SHIFT + R", "Something else", "other")\n', encoding="utf-8"
    )

    proc = machine.run()

    text = machine.bindings_target.read_text(encoding="utf-8")
    assert MARK_BEGIN not in text
    assert "already bound" in proc.stderr
    # The rest of the install still completed.
    assert machine.unit.is_file()


# --- uninstall -------------------------------------------------------------


def test_uninstall_reverses_everything_but_the_data(machine: FakeMachine) -> None:
    machine.run()
    (machine.data_home / "recordings" / "keepme.txt").write_text("audio", encoding="utf-8")
    machine.clear_log()

    proc = machine.run("--uninstall")

    assert not machine.venv.exists()
    for script in ("munin", "munin-rec", "munin-work"):
        assert not (machine.bin_dir / script).exists()
    assert not machine.plugin_dir.exists()
    assert machine.called("omarchy plugin disable local.munin")
    assert not machine.unit.exists()
    assert machine.called("systemctl --user disable --now munin.service")

    layout = json.loads(machine.shell_json.read_text(encoding="utf-8"))["bar"]["layout"]
    assert "local.munin" not in [w["id"] for w in layout["right"]]
    assert Path(str(machine.shell_json) + ".munin-bak").is_file()

    text = machine.bindings_target.read_text(encoding="utf-8")
    assert MARK_BEGIN not in text
    assert MARK_END not in text
    assert KEYBIND_LINE not in text
    assert 'o.bind("SUPER + SHIFT + I", "Editor", { launch = "editor" })' in text

    assert (machine.data_home / "recordings" / "keepme.txt").read_text(encoding="utf-8") == "audio"
    assert (machine.data_home / "config.toml").is_file()
    assert str(machine.data_home) in proc.stdout


def test_uninstall_dry_run_changes_nothing(machine: FakeMachine) -> None:
    machine.run()
    before = machine.bindings_target.read_text(encoding="utf-8")

    proc = machine.run("--uninstall", "--dry-run")

    assert machine.venv.exists()
    assert machine.plugin_dir.exists()
    assert machine.unit.is_file()
    assert machine.bindings_target.read_text(encoding="utf-8") == before
    assert "would remove the munin block from" in proc.stdout


def test_uninstall_on_a_clean_machine_is_quiet(machine: FakeMachine) -> None:
    proc = machine.run("--uninstall")
    assert "no unit at" in proc.stdout
    assert "no venv at" in proc.stdout


# --- step 1 failure --------------------------------------------------------


def test_missing_tool_stops_at_step_one(machine: FakeMachine, tmp_path: Path) -> None:
    """A PATH with no ffmpeg: the installer must stop and print the pkg line."""
    minbin = tmp_path / "minbin"
    minbin.mkdir()
    # Everything step 1 itself needs, and nothing else: the shims are bash
    # scripts run through `env`, so both have to be reachable.
    for tool in ("bash", "env", "readlink", "sort", "tr", "dirname", "basename", "mkdir"):
        source = shutil.which(tool)
        if source is not None:
            (minbin / tool).symlink_to(source)
    machine.env["PATH"] = f"{machine.shim_bin}{os.pathsep}{minbin}"
    (machine.shim_bin / "ffmpeg").unlink()

    proc = machine.run(expect_rc=1)

    assert "omarchy pkg add" in proc.stdout
    assert "ffmpeg" in proc.stdout
    assert "step 1 failed" in proc.stderr
    assert not machine.venv.exists()
