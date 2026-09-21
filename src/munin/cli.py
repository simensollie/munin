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
from pathlib import Path
from typing import Any

from munin.desktop import prepare_session_environment
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
    "too_soon": EXIT_PRECONDITION,
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
    """start, stop, toggle, status, list, mix, event, doctor, setup, daemon, worker."""
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

    mix = sub.add_parser("mix", help="write mixed.mp3 for manual upload (spec 10)")
    mix.add_argument(
        "session", nargs="?", default=None, help="session id; default is the most recent"
    )
    mix.add_argument("--all", action="store_true", help="every session that has no mix yet")
    mix.add_argument(
        "--format",
        dest="format",
        choices=("opus", "mp3"),
        default=None,
        help="upload format; opus is a third the size of mp3 at better quality (spec 10)",
    )
    mix.add_argument("--force", action="store_true", help="re-encode even if mixed.mp3 exists")
    mix.add_argument(
        "--to",
        default=None,
        metavar="DIR",
        help=(
            "also copy the mix there, named after the session title; "
            "defaults to [export] directory when that section is enabled"
        ),
    )

    split_cmd = sub.add_parser(
        "split", help="cut a recording that holds two meetings into two sessions"
    )
    split_cmd.add_argument(
        "session",
        nargs="?",
        default=None,
        help="session id; default is the most recent. A time here means the cut point",
    )
    split_cmd.add_argument(
        "at",
        nargs="?",
        default=None,
        help="cut point on the audio timeline: HH:MM:SS, MM:SS or seconds",
    )
    split_cmd.add_argument(
        "--now",
        action="store_true",
        help="split the running recording here: finish it and start the next one",
    )
    split_cmd.add_argument(
        "--clock",
        default=None,
        metavar="HH:MM",
        help="cut at this time of day instead of an offset into the audio",
    )
    split_cmd.add_argument(
        "--title",
        default=None,
        help="title for the second meeting (the new recording, with --now)",
    )
    split_cmd.add_argument(
        "--title-first",
        dest="title_first",
        default=None,
        metavar="TEXT",
        help="rename the first half; it keeps the original title otherwise",
    )

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
    # install.sh step 7 calls this: create the root and write config.toml, then
    # stop -- no microphone question and no test recording during an install.
    setup.add_argument(
        "--write-default-config",
        action="store_true",
        help="create the data root and config.toml, then exit (used by install.sh)",
    )

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


def _megabytes(path: Path) -> str:
    """Size in MB, one decimal, locale-neutral like every other line here."""
    return f"{path.stat().st_size / 1_000_000:.1f}"


def _cmd_mix(args: argparse.Namespace) -> int:
    """Sum the two tracks into ``mixed.mp3`` for a manual upload (spec 10).

    Reads the spool directly rather than going through the socket: a mix is a
    file operation on a finished session, so it must work while ``munin-rec``
    is down, and a 17-minute meeting holds the daemon's event loop for seconds
    if it does not.
    """
    import shutil as shutil_module

    from munin import mixdown as mixdown_module
    from munin.spool import Spool

    if args.all and args.session:
        print("give a session id or --all, not both", file=sys.stderr)
        return EXIT_USAGE

    config = _config()
    # Flag beats config beats built-in default. With [export] enabled, a bare
    # `munin mix` therefore refills the upload folder the same way the worker
    # does, which is what makes running it by hand and letting it happen
    # automatically produce the same files.
    fmt = args.format or config.export.format
    spool = Spool(config)
    if args.all:
        sessions = [
            session
            for session in spool.iter_sessions()
            if session.state not in mixdown_module.BUSY_STATES
            # A split parent holds both meetings; its halves hold one each, and
            # they are what a manual upload wants (D26). Mixing all three would
            # put the merged recording in the upload folder beside them.
            and session.state != "split"
        ]
        if not sessions:
            print("no sessions to mix")
            return EXIT_OK
    elif args.session:
        found = spool.find(args.session)
        if found is None:
            print(f"no session {args.session}", file=sys.stderr)
            return EXIT_USAGE
        sessions = [found]
    else:
        latest = spool.latest()
        if latest is None:
            print("no sessions yet", file=sys.stderr)
            return EXIT_PRECONDITION
        sessions = [latest]

    if args.to:
        destination = Path(args.to).expanduser()
    elif config.export.enabled:
        destination = config.export_dir
    else:
        destination = None
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)

    failed = 0
    for session in sessions:
        already = (
            mixdown_module.mixed_path(session, fmt).exists() and not args.force
        )
        try:
            path = mixdown_module.mixdown(session, fmt=fmt, force=bool(args.force))
        except mixdown_module.MixdownError as exc:
            print(str(exc), file=sys.stderr)
            failed += 1
            continue
        line = f"{session.id}  {path}  {_megabytes(path)} MB"
        if already:
            line += "  (already mixed)"
        if destination is not None:
            copy = destination / mixdown_module.export_filename(session.id, fmt)
            shutil_module.copy2(path, copy)
            line += f"  -> {copy}"
        print(line)
        if (session.duration_seconds or 0) > mixdown_module.PLAUD_MAX_SECONDS:
            print(
                f"{session.id}: longer than Plaud's 5-hour limit; split it before uploading",
                file=sys.stderr,
            )
    return EXIT_ERROR if failed else EXIT_OK


def _cmd_split(args: argparse.Namespace) -> int:
    """Cut a session in two (D26), live or after the fact.

    ``--now`` is the daemon's job and goes through the socket; a cut of a
    captured session is a file operation and reads the spool directly, like
    ``mix`` -- so it works with ``munin-rec`` down, and a two-hour session does
    not hold the daemon's event loop while ffmpeg runs.
    """
    from munin import ipc
    from munin import split as split_module
    from munin.spool import NoSpaceError, Spool

    if args.now:
        for flag, name in (
            (args.session, "a session id"),
            (args.at, "a cut point"),
            (args.clock, "--clock"),
            (args.title_first, "--title-first"),
        ):
            if flag:
                print(f"--now splits the running recording; drop {name}", file=sys.stderr)
                return EXIT_USAGE
        data = ipc.call("split", title=args.title)
        print(
            f"captured: {data.get('closed_id')} "
            f"{_clock(data.get('closed_duration_seconds'))}"
        )
        print(f"recording: {data.get('session_id')} segment {data.get('segment')}")
        return EXIT_OK

    session_id, at = args.session, args.at
    if at is None and session_id is not None and args.clock is None:
        # One positional and no --clock: "munin split 27:32" is the common case,
        # and it means the most recent session, the way "munin mix" does.
        try:
            split_module.parse_offset(session_id)
        except split_module.SplitError:
            pass
        else:
            session_id, at = None, session_id
    if at is not None and args.clock is not None:
        print("give a cut point or --clock, not both", file=sys.stderr)
        return EXIT_USAGE
    if at is None and args.clock is None:
        print(
            "give a cut point: munin split [session] HH:MM:SS, or --clock HH:MM",
            file=sys.stderr,
        )
        return EXIT_USAGE

    spool = Spool(_config())
    if session_id:
        session = spool.find(session_id)
        if session is None:
            print(f"no session {session_id}", file=sys.stderr)
            return EXIT_USAGE
    else:
        session = spool.latest()
        if session is None:
            print("no sessions yet", file=sys.stderr)
            return EXIT_PRECONDITION

    try:
        if args.clock is not None:
            at_seconds = split_module.clock_to_offset(session, args.clock)
        else:
            at_seconds = split_module.parse_offset(at)
        first, second = split_module.split(
            session,
            spool=spool,
            at_seconds=at_seconds,
            titles=(args.title_first, args.title),
        )
    except split_module.SplitUsage as exc:
        # What was typed is not a time: a bad argument, and exit 2 like any
        # other. Checked first -- it is a SplitError too.
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except split_module.SplitFailed as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except NoSpaceError as exc:
        # A split writes a second copy of the audio, so it can run the disk
        # down. Section 11 gives disk-below-minimum its own exit code, and the
        # keybind branches on it.
        print(str(exc), file=sys.stderr)
        return EXIT_PRECONDITION
    except split_module.SplitError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PRECONDITION

    # Numbered explicitly: part 1 keeps the parent's title and its minute, so
    # its id takes the collision suffix of contracts section 2 -- and a trailing
    # "-2" on the *first* half would otherwise read as a part number.
    for number, part in enumerate((first, second), start=1):
        print(
            f"part {number}  {part.id}  {part.state}  "
            f"{_clock(part.duration_seconds)}  {part.title}"
        )
    print(
        f"{session.id} is now split; its audio is untouched and it is out of the queue"
    )
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

    return int(doctor.main(_config(), as_json=bool(args.json)))


def _cmd_setup(args: argparse.Namespace) -> int:
    from munin import setup

    return int(
        setup.main(
            _config(),
            non_interactive=bool(args.non_interactive),
            write_config_only=bool(args.write_default_config),
        )
    )


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
    return int(worker.main(passthrough))


def _config() -> Any:
    """The config for ``doctor`` and ``setup``, or defaults if it will not parse.

    These are the two commands a user reaches for *because* the config is
    broken, so neither may refuse to run over it. ``doctor.check_config``
    re-reads the file itself and reports the parse error as a failing check
    (exit 5, which the plugin and the installer branch on); ``setup
    --write-default-config`` can then replace it. Refusing here turned both into
    a bare "failed: ... is not valid TOML" and exit 1, with no check output at
    all -- the one diagnosis the user needed, withheld.
    """
    from munin.config import Config, ConfigError
    from munin.daemon import load_config
    from munin.paths import munin_home

    try:
        return load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        print("continuing with defaults; the check below has the detail", file=sys.stderr)
        return Config(home=munin_home())


_DISPATCH = {
    "start": _cmd_start,
    "stop": _cmd_stop,
    "split": _cmd_split,
    "toggle": _cmd_toggle,
    "status": _cmd_status,
    "list": _cmd_list,
    "mix": _cmd_mix,
    "event": _cmd_event,
    "doctor": _cmd_doctor,
    "setup": _cmd_setup,
    "daemon": _cmd_daemon,
    "worker": _cmd_worker,
}


def main(argv: list[str] | None = None) -> int:
    # A terminal that did not inherit the graphical session has no runtime dir
    # and no compositor signature; derive them, or every check below "finds
    # nothing" for a reason that has nothing to do with the install.
    prepare_session_environment()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    handler = _DISPATCH[args.command]
    try:
        return handler(args)
    except DaemonUnreachable as exc:
        if args.command in ("start", "stop", "split", "toggle", "list", "event"):
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
