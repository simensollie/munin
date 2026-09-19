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


def _keybind_backups(machine: FakeMachine) -> list[Path]:
    """The keybinding backups, which live in munin's state dir, not the repo."""
    state = machine.home / ".local" / "state" / "munin"
    return sorted(state.glob("bindings.lua.*.bak")) if state.is_dir() else []


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
    assert (
        "would run: omarchy plugin enable local.munin --section center --after omarchy.weather"
        in proc.stdout
    )
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

    # 4: enabled and placed on the bar. The placement rides along with `enable`,
    # which already places a bar-widget: a later `bar put` would be too late.
    assert machine.called("omarchy-shell shell rescanPlugins")
    assert machine.called(
        "omarchy plugin enable local.munin --section center --after omarchy.weather"
    )
    layout = json.loads(machine.shell_json.read_text(encoding="utf-8"))["bar"]["layout"]
    center = [w["id"] for w in layout["center"]]
    assert center.index("local.munin") == center.index("omarchy.weather") + 1
    assert "local.munin" not in [w["id"] for w in layout["right"]]

    # 5: keybind, backed up outside the dotfiles repo, appended through the
    # symlink, and said out loud
    backups = _keybind_backups(machine)
    assert len(backups) == 1
    assert MARK_BEGIN not in backups[0].read_text(encoding="utf-8")
    assert not Path(str(machine.bindings_target) + ".munin-bak").exists(), (
        "the backup must not land in the dotfiles repository"
    )
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


def test_a_bar_without_the_anchor_still_gets_the_widget(machine: FakeMachine) -> None:
    """A user who removed the weather widget must not end up with no widget at all.

    PluginRegistry fails the enable outright when the anchor is not on the bar
    ("could not find target widget"), so the installer retries with the section
    alone rather than leaving the plugin enabled but unplaced.
    """
    data = json.loads(machine.shell_json.read_text(encoding="utf-8"))
    data["bar"]["layout"]["center"] = [
        w for w in data["bar"]["layout"]["center"] if w["id"] != "omarchy.weather"
    ]
    machine.shell_json.write_text(json.dumps(data), encoding="utf-8")

    proc = machine.run()

    assert "is not on the bar" in proc.stderr
    assert machine.called("omarchy plugin enable local.munin --section center")
    layout = json.loads(machine.shell_json.read_text(encoding="utf-8"))["bar"]["layout"]
    assert "local.munin" in [w["id"] for w in layout["center"]]


def test_install_is_idempotent(machine: FakeMachine) -> None:
    machine.run()
    config = machine.data_home / "config.toml"
    config.write_text("# hand edited, do not clobber\n", encoding="utf-8")
    backup_before = [(b.name, b.read_text(encoding="utf-8")) for b in _keybind_backups(machine)]
    machine.clear_log()

    second = machine.run()

    text = machine.bindings_target.read_text(encoding="utf-8")
    assert text.count(MARK_BEGIN) == 1
    assert text.count(KEYBIND_LINE) == 1
    assert "already present" in second.stdout
    assert [
        (b.name, b.read_text(encoding="utf-8")) for b in _keybind_backups(machine)
    ] == backup_before
    assert config.read_text(encoding="utf-8") == "# hand edited, do not clobber\n"
    assert not machine.called("munin setup")
    # Already on the bar: left where it is, and never asked for a second place.
    assert machine.called("omarchy plugin enable local.munin")
    assert not machine.called("omarchy plugin enable local.munin --section")
    assert "left where it is" in second.stdout
    layout = json.loads(machine.shell_json.read_text(encoding="utf-8"))["bar"]["layout"]
    assert [w["id"] for w in layout["center"]].count("local.munin") == 1


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


def test_an_unreachable_shell_does_not_strand_the_install(machine: FakeMachine) -> None:
    """ssh, a bare TTY, or a first boot before the shell is up (step 4).

    The plugin cannot be enabled without a running shell, and that is a reason
    to say so, not a reason to leave the machine with a venv and no data root.
    """
    machine.env["SHIM_NO_SHELL"] = "1"

    proc = machine.run()

    assert "not reachable" in proc.stderr
    assert (
        "omarchy plugin enable local.munin --section center --after omarchy.weather"
        in proc.stdout
    )

    # Steps 5-7 still ran: they are filesystem work, not shell work.
    assert MARK_BEGIN in machine.bindings_target.read_text(encoding="utf-8")
    assert machine.unit.is_file()
    assert (machine.data_home / "recordings").is_dir()
    assert (machine.data_home / "config.toml").is_file()


def test_uninstall_removes_the_keybind_backup_and_reloads(machine: FakeMachine) -> None:
    machine.run()
    assert len(_keybind_backups(machine)) == 1
    # A backup an earlier version of the installer left inside the dotfiles repo.
    legacy = Path(str(machine.bindings_target) + ".munin-bak")
    legacy.write_text("old\n", encoding="utf-8")
    machine.clear_log()

    machine.run("--uninstall")

    assert _keybind_backups(machine) == []
    assert not legacy.exists(), "an uninstall must not leave litter in someone's repo"
    # The key still points at a binary this same run deletes, until Hyprland is told.
    assert machine.called("hyprctl reload")


def test_install_derives_the_runtime_dir_for_a_bare_terminal(machine: FakeMachine) -> None:
    """Seen live: the user's terminal had no XDG_RUNTIME_DIR and step 5's reload failed."""
    del machine.env["XDG_RUNTIME_DIR"]
    machine.run()
    assert machine.called("hyprctl reload")


def test_enable_may_not_change_the_bars_transparency(machine: FakeMachine) -> None:
    """Seen live: the shell persisted its in-memory config on enable and flipped
    bar.transparent to true, making the bar unreadable. The installer restores it."""
    import json

    before = json.loads(machine.shell_json.read_text(encoding="utf-8"))
    before["bar"]["transparent"] = False  # as on the machine where this happened
    machine.shell_json.write_text(json.dumps(before), encoding="utf-8")
    machine.env["SHIM_FLIP_TRANSPARENT"] = "1"
    machine.run()
    assert machine.called("omarchy bar transparent false")
    after = json.loads(machine.shell_json.read_text(encoding="utf-8"))
    assert after["bar"]["transparent"] is False
    assert [w["id"] for w in after["bar"]["layout"]["center"]][-1] == "local.munin"


def test_a_bar_that_was_already_transparent_is_left_alone(machine: FakeMachine) -> None:
    import json

    data = json.loads(machine.shell_json.read_text(encoding="utf-8"))
    data["bar"]["transparent"] = True
    machine.shell_json.write_text(json.dumps(data), encoding="utf-8")
    machine.run()
    assert not machine.called("omarchy bar transparent")
    assert json.loads(machine.shell_json.read_text(encoding="utf-8"))["bar"]["transparent"] is True


def test_a_changed_plugin_restarts_the_shell_and_an_unchanged_one_does_not(
    machine: FakeMachine,
) -> None:
    """Measured: a hot-reload keeps the already-evaluated Model.js, so a new
    plugin only shows up after omarchy-restart-shell. A first install and a
    no-op re-run must not restart anything."""
    machine.run()
    assert not machine.called("omarchy-restart-shell")

    machine.clear_log()
    machine.run()
    assert not machine.called("omarchy-restart-shell"), "identical files: nothing to reload"

    installed = machine.home / ".config" / "omarchy" / "plugins" / "local.munin" / "Model.js"
    installed.write_text(installed.read_text(encoding="utf-8") + "\n// stale\n", encoding="utf-8")
    machine.clear_log()
    machine.run()
    assert machine.called("omarchy-restart-shell")
    assert "// stale" not in installed.read_text(encoding="utf-8")
