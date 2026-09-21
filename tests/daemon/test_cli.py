"""The CLI: argument shapes, exit codes and the JSON the plugin reads.

The exit codes are the contract the keybind, the plugin and the installer branch
on, so every one of them is pinned here. The socket is never opened: ``ipc.call``
is replaced, because what the CLI must get right is the mapping from a daemon
reply to an exit code and one short line of output.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from munin import cli
from munin.ipc import DaemonUnreachable, IpcError


class FakeCall:
    """Stands in for :func:`munin.ipc.call`, recording what the CLI asked for."""

    def __init__(self, result: Any = None) -> None:
        self.result = result if result is not None else {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, cmd: str, path=None, **args: Any) -> dict[str, Any]:
        self.calls.append((cmd, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture()
def fake_ipc(monkeypatch):
    def install(result: Any = None) -> FakeCall:
        fake = FakeCall(result)
        monkeypatch.setattr("munin.ipc.call", fake)
        return fake

    return install


# --- parsing ---------------------------------------------------------
def test_the_parser_covers_every_command_in_the_contract() -> None:
    parser = cli.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions, "there must be a subcommand slot"
    assert set(actions[0].choices) == {
        "start", "stop", "split", "toggle", "status", "list", "mix", "event",
        "doctor", "setup", "daemon", "worker",
    }


def test_start_parses_a_title_and_both_flags() -> None:
    args = cli.build_parser().parse_args(
        ["start", "Weekly quality sync", "--resume", "--from-detection"]
    )
    assert args.title == "Weekly quality sync"
    assert args.resume is True
    assert args.from_detection is True


def test_event_only_accepts_the_two_events() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["event", "call-started", "--pid", "4242", "--app", "Beacon 365"])
    assert (args.event, args.pid, args.app) == ("call-started", 4242, "Beacon 365")
    with pytest.raises(SystemExit):
        parser.parse_args(["event", "call-paused"])


def test_no_command_prints_help_and_exits_two(capsys) -> None:
    assert cli.main([]) == cli.EXIT_USAGE
    assert "usage" in capsys.readouterr().out.lower()


# --- exit codes ------------------------------------------------------
def test_start_reports_the_session(fake_ipc, capsys) -> None:
    fake = fake_ipc({"session_id": "2026-09-14T1325-weekly-quality-sync", "segment": 1,
                     "resumed": False, "state": "recording", "session": "/tmp/x"})
    assert cli.main(["start", "Weekly quality sync"]) == cli.EXIT_OK
    assert fake.calls[0][0] == "start"
    assert fake.calls[0][1]["title"] == "Weekly quality sync"
    assert "2026-09-14T1325-weekly-quality-sync" in capsys.readouterr().out


def test_a_resume_that_could_not_resume_says_so(fake_ipc, capsys) -> None:
    fake_ipc({"session_id": "s2", "segment": 1, "resumed": False,
              "state": "recording", "session": "/tmp/x"})
    assert cli.main(["start", "--resume"]) == cli.EXIT_OK
    assert "resume window has closed" in capsys.readouterr().out


def test_stop_without_a_recording_exits_four(fake_ipc, capsys) -> None:
    fake_ipc(IpcError("not_recording", "nothing is being recorded"))
    assert cli.main(["stop"]) == cli.EXIT_PRECONDITION
    assert "not_recording" in capsys.readouterr().err


def test_a_full_disk_exits_four(fake_ipc) -> None:
    fake_ipc(IpcError("no_space", "only 10 MB free"))
    assert cli.main(["start"]) == cli.EXIT_PRECONDITION


def test_a_capture_failure_exits_one(fake_ipc) -> None:
    fake_ipc(IpcError("capture_failed", "no PipeWire"))
    assert cli.main(["start"]) == cli.EXIT_ERROR


def test_status_without_a_daemon_says_unknown_and_exits_three(fake_ipc, capsys) -> None:
    fake_ipc(DaemonUnreachable("no_daemon", "nothing listening"))
    assert cli.main(["status"]) == cli.EXIT_NO_DAEMON
    assert capsys.readouterr().out.strip() == "state: unknown (daemon not running)"


def test_status_json_without_a_daemon_is_still_json(fake_ipc, capsys) -> None:
    fake_ipc(DaemonUnreachable("no_daemon", "nothing listening"))
    assert cli.main(["status", "--json"]) == cli.EXIT_NO_DAEMON
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "unknown"
    assert payload["daemon_running"] is False


def test_start_without_a_daemon_says_how_to_start_it(fake_ipc, capsys) -> None:
    fake_ipc(DaemonUnreachable("no_daemon", "nothing listening"))
    assert cli.main(["start"]) == cli.EXIT_NO_DAEMON
    assert "systemctl --user start munin.service" in capsys.readouterr().err


# --- machine-readable output -----------------------------------------
def test_status_json_is_the_state_view_verbatim(fake_ipc, capsys) -> None:
    view = {
        "schema_version": 1, "state": "recording", "since": "2026-09-14T13:25:08+02:00",
        "started_at": "2026-09-14T13:25:08+02:00", "title": "Weekly quality sync",
        "session": "/home/user/munin/recordings/2026/09/x", "session_id": "x",
        "segment": 1, "detected_app": None, "grace_deadline": None, "queue_depth": 2,
        "last_error": None, "idle_was_inhibited": False,
        "updated_at": "2026-09-14T13:25:08+02:00", "daemon_pid": 4211,
    }
    fake_ipc(view)
    assert cli.main(["status", "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == view


def test_status_human_line_is_one_line(fake_ipc, capsys) -> None:
    fake_ipc({"state": "recording", "session_id": "x", "title": "Weekly quality sync",
              "queue_depth": 2})
    cli.main(["status"])
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0
    assert out.startswith("state: recording")


def test_list_json_passes_the_limit_through(fake_ipc, capsys) -> None:
    fake = fake_ipc({"sessions": [
        {"id": "x", "state": "pending", "title": "Weekly quality sync",
         "started_at": None, "duration_seconds": 61.0, "path": "/tmp/x",
         "pending_reason": "no transcription backend configured"}
    ]})
    assert cli.main(["list", "--limit", "5", "--json"]) == cli.EXIT_OK
    assert fake.calls[0][1]["limit"] == 5
    assert json.loads(capsys.readouterr().out)["sessions"][0]["state"] == "pending"


def test_list_human_shows_the_pending_reason(fake_ipc, capsys) -> None:
    fake_ipc({"sessions": [
        {"id": "x", "state": "pending", "title": "Weekly quality sync",
         "started_at": None, "duration_seconds": 61.0, "path": "/tmp/x",
         "pending_reason": "no transcription backend configured"}
    ]})
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "00:01:01" in out
    assert "no transcription backend configured" in out


def test_list_with_a_silly_limit_is_a_usage_error(fake_ipc) -> None:
    fake_ipc({"sessions": []})
    assert cli.main(["list", "--limit", "0"]) == cli.EXIT_USAGE


def test_event_forwards_every_field(fake_ipc, capsys) -> None:
    fake = fake_ipc({"accepted": True, "state": "detected"})
    assert cli.main([
        "event", "call-started", "--pid", "4242", "--app", "Beacon 365",
        "--app-id", "beacon-tab", "--title", "Weekly quality sync | Beacon 365",
    ]) == cli.EXIT_OK
    args = fake.calls[0][1]
    assert args["event"] == "call-started"
    assert args["pid"] == 4242
    assert args["app_id"] == "beacon-tab"
    assert "accepted=true" in capsys.readouterr().out


def test_toggle_prints_whichever_happened(fake_ipc, capsys) -> None:
    fake_ipc({"action": "stopped", "session_id": "x", "duration_seconds": 3723.0,
              "state": "captured", "session": "/tmp/x"})
    assert cli.main(["toggle"]) == cli.EXIT_OK
    assert "01:02:03" in capsys.readouterr().out


# --- delegation ------------------------------------------------------
def test_doctor_delegates_and_survives_a_stub(capsys) -> None:
    """The install workstream owns doctor; a missing one must not traceback."""
    code = cli.main(["doctor"])
    assert code in (cli.EXIT_OK, cli.EXIT_ERROR, cli.EXIT_CHECK_FAILED)
    if code == cli.EXIT_ERROR:
        assert "not implemented" in capsys.readouterr().err


def test_doctor_still_runs_when_the_config_will_not_parse(
    tmp_path, monkeypatch, capsys
) -> None:
    """The one check that names the problem must not be refused because of it."""
    home = tmp_path / "munin"
    (home / "recordings").mkdir(parents=True)
    (home / "inbox").mkdir()
    (home / "voices").mkdir()
    (home / "config.toml").write_text('[capture\nmic_source = "default"\n', encoding="utf-8")
    monkeypatch.setenv("MUNIN_HOME", str(home))

    code = cli.main(["doctor"])
    out = capsys.readouterr()

    assert code == cli.EXIT_CHECK_FAILED, "a bad config is a failing check, not a crash"
    assert "config" in out.out
    assert "is not valid TOML" in out.out + out.err


def test_setup_still_runs_when_the_config_will_not_parse(
    tmp_path, monkeypatch, capsys
) -> None:
    """`munin setup --write-default-config` is how the user repairs it."""
    home = tmp_path / "munin"
    home.mkdir(parents=True)
    (home / "config.toml").write_text("this is not = = toml\n", encoding="utf-8")
    monkeypatch.setenv("MUNIN_HOME", str(home))

    code = cli.main(["setup", "--write-default-config"])
    out = capsys.readouterr()

    assert code == cli.EXIT_OK, "setup must not refuse to run over the file it repairs"
    assert "is not valid TOML" in out.err


def test_worker_delegates_and_survives_a_stub(capsys) -> None:
    code = cli.main(["worker", "--once"])
    assert code in (cli.EXIT_OK, cli.EXIT_ERROR)
    if code == cli.EXIT_ERROR:
        assert "not implemented" in capsys.readouterr().err


def test_clock_is_hours_unbounded() -> None:
    assert cli._clock(0) == "00:00:00"
    assert cli._clock(61) == "00:01:01"
    assert cli._clock(360000) == "100:00:00"


# --- split (D26) -----------------------------------------------------
def test_split_now_goes_through_the_socket(fake_ipc, capsys) -> None:
    fake = fake_ipc(
        {
            "session_id": "2026-09-14T1402-meeting-14-02",
            "segment": 1,
            "closed_id": "2026-09-14T1325-weekly-quality-sync",
            "closed_duration_seconds": 2223.0,
        }
    )

    code = cli.main(["split", "--now", "--title", "Supplier audit follow-up"])
    out = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert fake.calls == [("split", {"title": "Supplier audit follow-up"})]
    assert "captured: 2026-09-14T1325-weekly-quality-sync 00:37:03" in out
    assert "recording: 2026-09-14T1402-meeting-14-02 segment 1" in out


def test_split_now_maps_not_recording_to_the_precondition_code(fake_ipc) -> None:
    fake_ipc(IpcError("not_recording", "nothing is being recorded"))

    assert cli.main(["split", "--now"]) == cli.EXIT_PRECONDITION


def test_split_now_says_how_to_start_the_daemon(fake_ipc, capsys) -> None:
    fake_ipc(DaemonUnreachable("internal", "no socket"))

    code = cli.main(["split", "--now"])

    assert code == cli.EXIT_NO_DAEMON
    assert "systemctl --user start munin.service" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["split", "--now", "2026-09-14T1325-weekly-quality-sync"],
        ["split", "--now", "--clock", "14:02"],
        ["split", "--now", "--title-first", "Weekly quality sync"],
    ],
)
def test_split_now_refuses_the_arguments_of_a_cut(argv, capsys) -> None:
    """--now splits where the recording is, so a cut point is a contradiction."""
    assert cli.main(argv) == cli.EXIT_USAGE
    assert "--now" in capsys.readouterr().err


def test_split_needs_a_cut_point(munin_home, capsys) -> None:
    code = cli.main(["split", "2026-09-14T1325-weekly-quality-sync"])

    assert code == cli.EXIT_USAGE
    assert "give a cut point" in capsys.readouterr().err


def test_split_refuses_a_cut_point_and_a_clock_together(munin_home, capsys) -> None:
    code = cli.main(
        ["split", "2026-09-14T1325-weekly-quality-sync", "27:32", "--clock", "14:02"]
    )

    assert code == cli.EXIT_USAGE
    assert "not both" in capsys.readouterr().err


def test_split_names_a_session_it_cannot_find(munin_home, capsys) -> None:
    code = cli.main(["split", "2026-09-14T1325-no-such-session", "27:32"])

    assert code == cli.EXIT_USAGE
    assert "no session" in capsys.readouterr().err


def test_split_with_only_a_time_reaches_for_the_most_recent_session(
    munin_home, capsys
) -> None:
    """``munin split 27:32`` is the common case, and it means the latest
    session -- the same default ``munin mix`` has. With an empty spool that is a
    precondition failure, not a usage error."""
    code = cli.main(["split", "27:32"])

    assert code == cli.EXIT_PRECONDITION
    assert "no sessions yet" in capsys.readouterr().err


def test_split_parses_both_titles_and_a_clock() -> None:
    args = cli.build_parser().parse_args(
        [
            "split",
            "2026-09-14T1325-weekly-quality-sync",
            "--clock",
            "14:02",
            "--title",
            "Supplier audit follow-up",
            "--title-first",
            "Weekly quality sync",
        ]
    )

    assert args.session == "2026-09-14T1325-weekly-quality-sync"
    assert args.at is None
    assert args.clock == "14:02"
    assert args.title == "Supplier audit follow-up"
    assert args.title_first == "Weekly quality sync"
    assert args.now is False
