"""``munin``: the command-line surface, and the only client of the IPC socket.

Every command exits with a code from the contract, because the keybind, the
plugin and the installer all branch on them:

==== ==========================================================
 0    success
 1    generic runtime failure
 2    usage error
 3    daemon unreachable (or already running, when starting one)
 4    precondition failed (already/not recording, disk below min_free_mb)
 5    doctor found a failing check
==== ==========================================================

Human output is one short, locale-neutral line per command; ``--json`` is the
machine-readable form the shell plugin and the installer read. Nothing here
formats a date for a human -- the plugin does that, in the user's locale.

Owner: daemon workstream. Contract: PoC contracts section 11.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from munin.ipc import DaemonUnreachable, IpcError

__all__ = [
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_USAGE",
    "EXIT_NO_DAEMON",
    "EXIT_PRECONDITION",
    "EXIT_CHECK_FAILED",
    "build_parser",
    "main",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_DAEMON = 3
EXIT_PRECONDITION = 4
EXIT_CHECK_FAILED = 5

#: The daemon's error codes, mapped onto the exit codes the keybind branches on.
_EXIT_FOR_CODE: dict[str, int] = {
    "already_recording": EXIT_PRECONDITION,
    "not_recording": EXIT_PRECONDITION,
    "no_space": EXIT_PRECONDITION,
    "capture_failed": EXIT_ERROR,
    "bad_request": EXIT_USAGE,
    "unknown_command": EXIT_USAGE,
    "internal": EXIT_ERROR,
    "no_daemon": EXIT_NO_DAEMON,
}

_START_HINT = (
    "munin-rec is not running. Start it with: "
    "systemctl --user start munin.service"
)


def build_parser() -> argparse.ArgumentParser:
    """start, stop, toggle, status, list, event, doctor, setup, daemon, worker."""
    parser = argparse.ArgumentParser(
        prog="munin",
        description="Munin: record a meeting as two tracks and queue it for transcription.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    start = sub.add_parser("start", help="start (or resume) a recording")
    start.add_argument("title", nargs="?", default=None, help="what to call the meeting")
    start.add_argument(
        "--resume",
        action="store_true",
        help="continue the previous session if it is still inside the resume window",
    )
    start.add_argument(
        "--from-detection",
        action="store_true",
        help="started from a detection prompt; records the detected app on the session",
    )

    sub.add_parser("stop", help="stop and finish the current recording")

    toggle = sub.add_parser("toggle", help="start if idle, stop if recording")
    toggle.add_argument("title", nargs="?", default=None)

    status = sub.add_parser("status", help="what the daemon is doing")
    status.add_argument("--json", action="store_true", help="the raw state view")

    listing = sub.add_parser("list", help="recent sessions")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--json", action="store_true")

    event = sub.add_parser("event", help="feed detection evidence from the plugin")
    event.add_argument("event", choices=("call-started", "call-ended"))
    event.add_argument("--pid", type=int, default=None)
    event.add_argument("--app", default=None, help="application label, e.g. a meeting client")
    event.add_argument("--app-id", default=None, help="matching rule id, e.g. teams-tab")
    event.add_argument("--title", default=None, help="window title")
    event.add_argument("--handle", default=None, help="opaque platform token for the app stream")

    doctor = sub.add_parser("doctor", help="check this machine")
    doctor.add_argument("--json", action="store_true")

    setup = sub.add_parser("setup", help="create the data root and a default config")
    setup.add_argument("--non-interactive", action="store_true")

    daemon = sub.add_parser("daemon", help="run munin-rec")
    daemon.add_argument("--foreground", action="store_true", default=True)
    daemon.add_argument("--verbose", action="store_true")

    worker = sub.add_parser("worker", help="run munin-work")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--interval", type=int, default=None)

    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _clock(seconds: float | None) -> str:
    """``HH:MM:SS``, hours unbounded. Locale-neutral on purpose."""
    total = int(seconds or 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _cmd_start(args: argparse.Namespace) -> int:
    from munin import ipc

    data = ipc.call(
        "start",
        title=args.title,
        resume=bool(args.resume),
        from_detection=bool(args.from_detection),
    )
    if args.resume and not data.get("resumed"):
        print("resume window has closed; started a new session")
    print(
        f"recording: {data.get('session_id')} segment {data.get('segment')}"
        + (" (resumed)" if data.get("resumed") else "")
    )
    return EXIT_OK


def _cmd_stop(args: argparse.Namespace) -> int:
    from munin import ipc

    data = ipc.call("stop")
    print(
        f"captured: {data.get('session_id')} "
        f"{_clock(data.get('duration_seconds'))} -> {data.get('session')}"
    )
    return EXIT_OK


def _cmd_toggle(args: argparse.Namespace) -> int:
    from munin import ipc

    data = ipc.call("toggle", title=args.title)
    if data.get("action") == "stopped":
        print(f"captured: {data.get('session_id')} {_clock(data.get('duration_seconds'))}")
    else:
        print(f"recording: {data.get('session_id')} segment {data.get('segment')}")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    from munin import ipc

    try:
        data = ipc.call("status")
    except DaemonUnreachable:
        if args.json:
            _emit({"schema_version": 1, "state": "unknown", "daemon_running": False})
        else:
            print("state: unknown (daemon not running)")
        return EXIT_NO_DAEMON
    if args.json:
        _emit(data)
        return EXIT_OK
    line = f"state: {data.get('state')}"
    if data.get("session_id"):
        line += f"  session: {data['session_id']}"
    if data.get("title"):
        line += f"  title: {data['title']}"
    if data.get("queue_depth"):
        line += f"  queue: {data['queue_depth']}"
    print(line)
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    from munin import ipc

    if args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    data = ipc.call("list", limit=args.limit)
    sessions = data.get("sessions", [])
    if args.json:
        _emit(data)
        return EXIT_OK
    if not sessions:
        print("no sessions yet")
        return EXIT_OK
    for session in sessions:
        line = (
            f"{session.get('id')}  {session.get('state')}  "
            f"{_clock(session.get('duration_seconds'))}  {session.get('title')}"
        )
        if session.get("pending_reason"):
            line += f"  ({session['pending_reason']})"
        print(line)
    return EXIT_OK


def _cmd_event(args: argparse.Namespace) -> int:
    from munin import ipc

    data = ipc.call(
        "event",
        event=args.event,
        pid=args.pid,
        app=args.app,
        app_id=args.app_id,
        title=args.title,
        handle=args.handle,
    )
    print(f"{args.event}: accepted={str(data.get('accepted')).lower()} state={data.get('state')}")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    from munin import doctor

    config = _config()
    try:
        return int(doctor.main(config, as_json=bool(args.json)))
    except NotImplementedError:
        print(
            "munin doctor is not implemented in this build "
            "(the install workstream owns it)",
            file=sys.stderr,
        )
        return EXIT_ERROR


def _cmd_setup(args: argparse.Namespace) -> int:
    from munin import setup

    config = _config()
    try:
        return int(setup.main(config, non_interactive=bool(args.non_interactive)))
    except NotImplementedError:
        print(
            "munin setup is not implemented in this build "
            "(the install workstream owns it)",
            file=sys.stderr,
        )
        return EXIT_ERROR


def _cmd_daemon(args: argparse.Namespace) -> int:
    from munin import daemon

    passthrough: list[str] = []
    if args.verbose:
        passthrough.append("--verbose")
    return int(daemon.main(passthrough))


def _cmd_worker(args: argparse.Namespace) -> int:
    from munin import worker

    passthrough: list[str] = []
    if args.once:
        passthrough.append("--once")
    if args.interval is not None:
        passthrough += ["--interval", str(args.interval)]
    try:
        return int(worker.main(passthrough))
    except NotImplementedError:
        print(
            "munin worker is not implemented in this build "
            "(the worker workstream owns it)",
            file=sys.stderr,
        )
        return EXIT_ERROR


def _config() -> Any:
    from munin.daemon import load_config

    return load_config()


_DISPATCH = {
    "start": _cmd_start,
    "stop": _cmd_stop,
    "toggle": _cmd_toggle,
    "status": _cmd_status,
    "list": _cmd_list,
    "event": _cmd_event,
    "doctor": _cmd_doctor,
    "setup": _cmd_setup,
    "daemon": _cmd_daemon,
    "worker": _cmd_worker,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    handler = _DISPATCH[args.command]
    try:
        return handler(args)
    except DaemonUnreachable as exc:
        if args.command in ("start", "stop", "toggle", "list", "event"):
            print(_START_HINT, file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_NO_DAEMON
    except IpcError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return _EXIT_FOR_CODE.get(exc.code, EXIT_ERROR)
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - a traceback is not a user interface
        print(f"munin {args.command} failed: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
