"""The CLI derives the graphical-session variables a bare terminal lacks.

Measured on this machine: a terminal had neither XDG_RUNTIME_DIR nor
HYPRLAND_INSTANCE_SIGNATURE, so `munin doctor` failed five checks that had
nothing to do with the install. These tests pin the two derivations and the
cases where nothing may be guessed.
"""

from __future__ import annotations

from pathlib import Path

from munin.desktop import prepare_session_environment as dispatch
from munin.desktop.linux import DERIVED_MARKER, prepare_session_environment


def _runtime(tmp_path: Path, uid: int = 1000, instances: int = 1) -> Path:
    run = tmp_path / "run" / str(uid)
    for i in range(instances):
        (run / "hypr" / f"sig{i}").mkdir(parents=True)
    return run


def test_both_variables_are_derived_from_the_runtime_root(tmp_path: Path) -> None:
    run = _runtime(tmp_path)
    env: dict[str, str] = {}
    derived = prepare_session_environment(env, run_root=tmp_path / "run", uid=1000)
    assert derived == ["XDG_RUNTIME_DIR", "HYPRLAND_INSTANCE_SIGNATURE"]
    assert env["XDG_RUNTIME_DIR"] == str(run)
    assert env["HYPRLAND_INSTANCE_SIGNATURE"] == "sig0"
    assert env[DERIVED_MARKER] == "XDG_RUNTIME_DIR,HYPRLAND_INSTANCE_SIGNATURE"


def test_inherited_values_are_never_overwritten(tmp_path: Path) -> None:
    _runtime(tmp_path)
    env = {"XDG_RUNTIME_DIR": "/somewhere/else", "HYPRLAND_INSTANCE_SIGNATURE": "mine"}
    assert prepare_session_environment(env, run_root=tmp_path / "run", uid=1000) == []
    assert env["XDG_RUNTIME_DIR"] == "/somewhere/else"
    assert env["HYPRLAND_INSTANCE_SIGNATURE"] == "mine"
    assert DERIVED_MARKER not in env


def test_two_compositor_instances_are_ambiguous_so_none_is_chosen(tmp_path: Path) -> None:
    run = _runtime(tmp_path, instances=2)
    env: dict[str, str] = {}
    derived = prepare_session_environment(env, run_root=tmp_path / "run", uid=1000)
    assert derived == ["XDG_RUNTIME_DIR"]
    assert env["XDG_RUNTIME_DIR"] == str(run)
    assert "HYPRLAND_INSTANCE_SIGNATURE" not in env


def test_no_runtime_directory_means_nothing_is_set(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert prepare_session_environment(env, run_root=tmp_path / "missing", uid=1000) == []
    assert env == {}


def test_other_platforms_are_a_no_op() -> None:
    assert dispatch("darwin") == []
    assert dispatch("win32") == []
