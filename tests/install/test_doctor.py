"""``munin doctor`` over the synthetic machine ``install.sh`` just built.

The interesting assertion is not that a green machine is green -- it is that a
machine missing one piece says *which* piece, and that no check ever raises.
A doctor that crashes on the one broken thing it exists to find is useless, so
there is a test for exactly that.

Owner: install workstream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from munin.config import Config
from munin.doctor import CheckResult, format_table, main, run_checks

if TYPE_CHECKING:  # the fixture comes from tests/install/conftest.py
    from install.conftest import FakeMachine


def _config(machine: FakeMachine) -> Config:
    return Config(home=machine.data_home)


def _by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    out: dict[str, CheckResult] = {}
    for result in results:
        out.setdefault(result.name, result)
    return out


def _run(machine: FakeMachine) -> dict[str, CheckResult]:
    return _by_name(
        run_checks(_config(machine), home=machine.home, env=machine.env, platform="linux")
    )


@pytest.fixture()
def installed(machine: FakeMachine, monkeypatch: pytest.MonkeyPatch) -> FakeMachine:
    machine.run()
    machine.env["HOME"] = str(machine.home)
    # The detector spawns pw-dump itself, so it reads the *process* environment
    # rather than the env dict handed to run_checks. Point it at the shim, or a
    # healthy machine reports a detector that could not reach PipeWire -- which
    # is now, correctly, a failing row.
    for key in ("PATH", "SHIM_LOG", "SHIM_PW_DUMP", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(key, machine.env[key])
    return machine


def test_installed_machine_has_no_failures(installed: FakeMachine) -> None:
    results = run_checks(
        _config(installed), home=installed.home, env=installed.env, platform="linux"
    )
    failed = [r for r in results if r.status == "fail"]
    assert failed == [], format_table(results)


def test_each_check_reports_what_it_found(installed: FakeMachine) -> None:
    checks = _run(installed)

    assert checks["python"].status == "ok"
    assert checks["ffmpeg"].status == "ok"
    assert checks["pw-record"].status == "ok"
    assert checks["XDG_RUNTIME_DIR"].status == "ok"
    assert checks["pipewire"].status == "ok"

    assert checks["default source"].status == "ok"
    assert "Synthetic Mono Microphone" in checks["default source"].detail
    assert "16000" in checks["default source"].detail

    # The detector is driven directly, whatever detection.source says.
    assert checks["detection"].status == "ok"
    assert "no call in progress" in checks["detection"].detail

    assert checks["omarchy cli"].status == "ok"
    assert checks["plugin installed"].status == "ok"
    assert checks["plugin on bar"].status == "ok"
    assert "local.munin" in checks["plugin on bar"].detail
    assert checks["keybind"].status == "ok"
    assert "SUPER + SHIFT + R" in checks["keybind"].detail

    # Installed but deliberately not enabled: a warning, not a failure.
    assert checks["systemd unit"].status == "warn"
    assert "is-enabled: disabled" in checks["systemd unit"].detail

    assert checks["daemon"].status == "warn"
    assert "not running" in checks["daemon"].detail

    assert checks["data root"].status == "ok"
    assert checks["config"].status == "ok"
    assert checks["disk space"].status in ("ok", "warn")
    assert checks["spool"].status == "ok"

    # The PoC's visible end state: no backend, and doctor says so out loud.
    assert checks["transcription"].status == "warn"
    assert "deferred" in checks["transcription"].detail
    assert "pensieve" not in checks


def test_platform_table_has_one_populated_column(installed: FakeMachine) -> None:
    checks = _run(installed)
    assert checks["platform linux"].status == "ok"
    assert "capture:" in checks["platform linux"].detail
    for platform in ("darwin", "win32"):
        assert checks[f"platform {platform}"].status == "skip"


def test_a_bare_machine_fails_loudly(machine: FakeMachine) -> None:
    checks = _run(machine)
    assert checks["plugin installed"].status == "fail"
    assert checks["plugin on bar"].status == "fail"
    assert checks["keybind"].status == "fail"
    assert checks["systemd unit"].status == "fail"  # not even copied into place
    assert checks["data root"].status == "fail"
    assert checks["config"].status == "warn"


def test_missing_runtime_dir_is_a_failure(machine: FakeMachine) -> None:
    machine.env["XDG_RUNTIME_DIR"] = ""
    checks = _run(machine)
    assert checks["XDG_RUNTIME_DIR"].status == "fail"
    assert checks["pipewire"].status == "fail"
    assert "XDG_RUNTIME_DIR" in checks["pipewire"].detail
    assert checks["daemon"].status == "fail"


def test_a_double_binding_is_a_failure(installed: FakeMachine) -> None:
    with installed.bindings_target.open("a", encoding="utf-8") as handle:
        handle.write('o.bind("SUPER + SHIFT + R", "Something else", "other")\n')
    checks = _run(installed)
    assert checks["keybind"].status == "fail"
    assert "bound 2 times" in checks["keybind"].detail


def test_broken_config_is_a_failure(installed: FakeMachine) -> None:
    (installed.data_home / "config.toml").write_text("this is not = = toml\n", encoding="utf-8")
    checks = _run(installed)
    assert checks["config"].status == "fail"


def test_failed_sessions_are_counted(installed: FakeMachine) -> None:
    session = installed.data_home / "recordings" / "2026" / "09" / "2026-09-14T1325-weekly-sync"
    session.mkdir(parents=True)
    (session / "session.json").write_text(
        json.dumps({"schema_version": 1, "id": session.name, "state": "pending"}),
        encoding="utf-8",
    )
    link = installed.data_home / "inbox" / session.name
    link.symlink_to(Path("..") / "recordings" / "2026" / "09" / session.name)

    checks = _run(installed)
    assert checks["spool"].status == "warn"
    assert "pending: 1" in checks["spool"].detail


def test_a_raising_check_becomes_a_failure_line(
    installed: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import munin.doctor as doctor_module

    def explode(env: object) -> CheckResult:
        raise RuntimeError("synthetic explosion")

    monkeypatch.setattr(doctor_module, "CHECKS", (doctor_module.Check("boom", "all", explode),))
    results = run_checks(_config(installed), home=installed.home, env=installed.env)
    assert results[0].status == "fail"
    assert "RuntimeError" in results[0].detail


def test_exit_code_is_five_when_something_failed(
    machine: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(_config(machine), home=machine.home, env=machine.env, platform="linux")
    captured = capsys.readouterr()
    assert code == 5
    assert "FAIL" in captured.out
    assert "checks," in captured.out


def test_exit_code_is_zero_on_a_healthy_machine(
    installed: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(_config(installed), home=installed.home, env=installed.env, platform="linux")
    capsys.readouterr()
    assert code == 0


def test_json_output_is_machine_readable(
    installed: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        _config(installed),
        as_json=True,
        home=installed.home,
        env=installed.env,
        platform="linux",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 0
    names = {check["name"] for check in payload["checks"]}
    assert {"python", "keybind", "plugin on bar", "transcription", "platform linux"} <= names
    for check in payload["checks"]:
        assert check["status"] in ("ok", "warn", "fail", "skip")


def test_a_detector_that_cannot_reach_pipewire_is_a_failure(
    installed: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty scan and a dead probe are the same empty list; the row must differ.

    D4 rests entirely on detection, so a check that certifies a detector it
    could not even reach is worse than no check at all.
    """
    import munin.detect.linux as linux

    def unreachable(self: object, argv: object, timeout: object, which: object) -> None:
        self._record(which, "pw-dump exited 255: can't connect: Host is down")  # type: ignore[attr-defined]
        return None

    monkeypatch.setattr(linux.PipewireDetector, "_run_json", unreachable, raising=True)

    checks = _run(installed)
    assert checks["detection"].status == "fail"
    assert "could not read PipeWire" in checks["detection"].detail


def test_a_derived_runtime_dir_is_a_warning_not_a_failure(machine: FakeMachine) -> None:
    """The CLI derives it for a bare terminal; doctor must say so, not fail."""
    machine.env["MUNIN_ENV_DERIVED"] = "XDG_RUNTIME_DIR,HYPRLAND_INSTANCE_SIGNATURE"
    checks = _run(machine)
    assert checks["XDG_RUNTIME_DIR"].status == "warn"
    assert "derived" in checks["XDG_RUNTIME_DIR"].detail
