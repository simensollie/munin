"""The daemon's wire protocol: a Unix socket, one JSON object per line.

One client of this protocol exists -- the CLI. The shell plugin deliberately
does not speak it (D20 in spirit): it reads ``state.json`` and runs ``munin``,
so a shell reload can never hold the daemon's socket open.

Owner: daemon workstream. Protocol: PoC contracts section 7.
"""

from __future__ import annotations

import json
import logging
import os
import selectors
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "MAX_LINE_BYTES",
    "PROTOCOL_VERSION",
    "DEFAULT_TIMEOUT",
    "COMMANDS",
    "Request",
    "Reply",
    "IpcError",
    "DaemonUnreachable",
    "Server",
    "call",
    "socket_path",
]

log = logging.getLogger("munin.ipc")

MAX_LINE_BYTES = 64 * 1024

#: Bumped when the request or reply shape changes incompatibly. Every reply
#: carries it so a mismatched CLI and daemon (a half-finished upgrade, the most
#: likely cause) is a legible error rather than a KeyError.
PROTOCOL_VERSION = 1

#: Generous: ``stop`` flushes two encoders before it replies.
DEFAULT_TIMEOUT = 30.0

#: How long a connected client may sit idle mid-request before the daemon drops
#: it. The daemon is single-threaded; a hung client must not hold the timers.
CONNECTION_TIMEOUT = 5.0

COMMANDS: tuple[str, ...] = (
    "ping",
    "status",
    "start",
    "stop",
    "toggle",
    "event",
    "list",
)

#: Every code the daemon may return. The CLI maps these to exit codes.
ERROR_CODES: tuple[str, ...] = (
    "bad_request",
    "unknown_command",
    "already_recording",
    "not_recording",
    "no_space",
    "capture_failed",
    "internal",
)


class IpcError(RuntimeError):
    """A structured failure reply. ``code`` is one of the contract's error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DaemonUnreachable(IpcError):
    """No socket, or nothing listening on it. The CLI exits 3."""


class AlreadyRunning(IpcError):
    """Something else is already listening on the socket. The daemon exits 3."""


def socket_path(explicit: Path | str | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/munin/rec.sock``, or ``explicit`` when given.

    :mod:`munin.paths` owns the rule; this is the only place the daemon asks.
    ``paths.runtime_dir`` raises when ``XDG_RUNTIME_DIR`` is unset, which for a
    caller of this module means exactly "there is no socket to reach" -- so it
    is translated into :class:`DaemonUnreachable` and the CLI exits 3.
    """
    if explicit is not None:
        return Path(explicit)
    from munin import paths

    try:
        return paths.socket_path()
    except RuntimeError as exc:
        raise DaemonUnreachable("internal", str(exc)) from exc


@dataclass(frozen=True)
class Request:
    cmd: str
    args: dict[str, Any]

    @classmethod
    def from_line(cls, line: bytes) -> "Request":
        if len(line) > MAX_LINE_BYTES:
            raise IpcError("bad_request", f"request exceeds {MAX_LINE_BYTES} bytes")
        try:
            payload = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IpcError("bad_request", f"not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise IpcError("bad_request", "a request must be a JSON object")
        cmd = payload.get("cmd")
        if not isinstance(cmd, str) or not cmd:
            raise IpcError("bad_request", "a request needs a string 'cmd'")
        args = {key: value for key, value in payload.items() if key != "cmd"}
        return cls(cmd=cmd, args=args)

    def to_line(self) -> bytes:
        payload = {"cmd": self.cmd, **self.args}
        return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class Reply:
    ok: bool
    cmd: str
    data: dict[str, Any] | None = None
    error: dict[str, str] | None = None

    def to_line(self) -> bytes:
        """JSON plus a trailing newline, UTF-8."""
        payload: dict[str, Any] = {
            "ok": self.ok,
            "cmd": self.cmd,
            "protocol": PROTOCOL_VERSION,
        }
        if self.ok:
            payload["data"] = self.data or {}
        else:
            payload["error"] = self.error or {"code": "internal", "message": ""}
        return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def from_line(cls, line: bytes) -> "Reply":
        try:
            payload = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IpcError("internal", f"daemon sent invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise IpcError("internal", "daemon sent something that is not an object")
        return cls(
            ok=bool(payload.get("ok")),
            cmd=str(payload.get("cmd", "")),
            data=payload.get("data"),
            error=payload.get("error"),
        )

    @classmethod
    def ok_reply(cls, cmd: str, data: dict[str, Any]) -> "Reply":
        return cls(ok=True, cmd=cmd, data=data)

    @classmethod
    def error_reply(cls, cmd: str, code: str, message: str) -> "Reply":
        return cls(ok=False, cmd=cmd, error={"code": code, "message": message})


class Server:
    """Accepts connections on the daemon socket and dispatches to handlers.

    Single-threaded and serial: every command either completes quickly or starts
    a subprocess, so a queue of one is enough and there is no locking to get
    wrong. :meth:`poll` returns to the caller after at most ``timeout`` seconds
    whether or not anything arrived, which is what lets the daemon run its
    timers on the same thread.
    """

    def __init__(
        self, path: Path, handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]]
    ) -> None:
        self.path = Path(path)
        self.handlers = dict(handlers)
        self._sock: socket.socket | None = None
        self._selector: selectors.BaseSelector | None = None
        self._buffers: dict[int, bytearray] = {}

    # --- lifecycle -------------------------------------------------------
    def bind(self) -> None:
        """Create the socket, 0600, cleaning up a stale one.

        Connects to any existing socket first: a refused connection means the
        previous daemon died and the path may be unlinked; a successful one means
        another daemon is running and this process must exit 3.
        """
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

        if self.path.exists() or self.path.is_symlink():
            if self._someone_is_listening():
                raise AlreadyRunning(
                    "already_running",
                    f"another munin-rec is listening on {self.path}",
                )
            log.warning("removing stale socket path=%s", self.path)
            self.path.unlink(missing_ok=True)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.bind(str(self.path))
        os.chmod(self.path, 0o600)
        sock.listen(8)
        self._sock = sock
        self._selector = selectors.DefaultSelector()
        self._selector.register(sock, selectors.EVENT_READ, "listen")
        log.info("ipc listening path=%s", self.path)

    def _someone_is_listening(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(str(self.path))
        except (ConnectionRefusedError, FileNotFoundError):
            return False
        except OSError:
            # Permission or a non-socket file in the way: treat as stale rather
            # than refusing to start for ever.
            return False
        else:
            return True
        finally:
            probe.close()

    def close(self) -> None:
        if self._selector is not None:
            for key in list(self._selector.get_map().values()):
                if key.data != "listen":
                    self._drop(key.fileobj)  # type: ignore[arg-type]
            self._selector.close()
            self._selector = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self.path.unlink(missing_ok=True)

    # --- serving ---------------------------------------------------------
    def poll(self, timeout: float | None = 0.0) -> int:
        """Handle whatever is ready, waiting at most ``timeout`` seconds.

        Returns the number of requests answered.
        """
        if self._selector is None:
            raise IpcError("internal", "server is not bound")
        handled = 0
        for key, _events in self._selector.select(timeout):
            if key.data == "listen":
                self._accept()
            else:
                handled += self._read(key.fileobj)  # type: ignore[arg-type]
        return handled

    def serve_forever(
        self,
        *,
        on_idle: Callable[[], None] | None = None,
        poll_interval: float = 1.0,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        while True:
            if should_stop is not None and should_stop():
                return
            try:
                self.poll(poll_interval)
            except InterruptedError:  # pragma: no cover - signal race
                pass
            if on_idle is not None:
                on_idle()

    def dispatch(self, request: Request) -> Reply:
        handler = self.handlers.get(request.cmd)
        if handler is None:
            return Reply.error_reply(
                request.cmd, "unknown_command", f"unknown command {request.cmd!r}"
            )
        try:
            data = handler(request.args)
        except IpcError as exc:
            log.info("ipc refused cmd=%s code=%s message=%s", request.cmd, exc.code, exc)
            return Reply.error_reply(request.cmd, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - the daemon must not die of a bad request
            log.exception("ipc handler failed cmd=%s", request.cmd)
            return Reply.error_reply(
                request.cmd, "internal", f"{type(exc).__name__}: {exc}"
            )
        return Reply.ok_reply(request.cmd, data or {})

    # --- connection plumbing --------------------------------------------
    def _accept(self) -> None:
        assert self._sock is not None and self._selector is not None
        try:
            conn, _ = self._sock.accept()
        except BlockingIOError:  # pragma: no cover - spurious readiness
            return
        conn.settimeout(CONNECTION_TIMEOUT)
        self._buffers[conn.fileno()] = bytearray()
        self._selector.register(conn, selectors.EVENT_READ, "conn")

    def _read(self, conn: socket.socket) -> int:
        buffer = self._buffers.get(conn.fileno())
        if buffer is None:  # pragma: no cover - defensive
            self._drop(conn)
            return 0
        try:
            chunk = conn.recv(MAX_LINE_BYTES)
        except (TimeoutError, OSError):
            self._drop(conn)
            return 0
        if not chunk:
            self._drop(conn)
            return 0
        buffer.extend(chunk)

        handled = 0
        while b"\n" in buffer:
            raw, _, rest = bytes(buffer).partition(b"\n")
            buffer.clear()
            buffer.extend(rest)
            if not raw.strip():
                continue
            try:
                request = Request.from_line(raw)
            except IpcError as exc:
                reply = Reply.error_reply("", exc.code, str(exc))
            else:
                reply = self.dispatch(request)
            handled += 1
            try:
                conn.sendall(reply.to_line())
            except OSError:
                self._drop(conn)
                return handled
        if len(buffer) > MAX_LINE_BYTES:
            try:
                conn.sendall(
                    Reply.error_reply(
                        "", "bad_request", f"request exceeds {MAX_LINE_BYTES} bytes"
                    ).to_line()
                )
            except OSError:
                pass
            self._drop(conn)
        return handled

    def _drop(self, conn: socket.socket) -> None:
        if self._selector is not None:
            try:
                self._selector.unregister(conn)
            except (KeyError, ValueError):
                pass
        self._buffers.pop(conn.fileno(), None)
        try:
            conn.close()
        except OSError:  # pragma: no cover
            pass


def call(
    cmd: str,
    path: Path | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    **args: Any,
) -> dict[str, Any]:
    """Send one request from the CLI and return ``data``.

    Raises :class:`DaemonUnreachable` when nothing is listening, and
    :class:`IpcError` carrying the daemon's own code for a failure reply.
    """
    target = socket_path(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(target))
        except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
            raise DaemonUnreachable(
                "no_daemon", f"munin-rec is not listening on {target}"
            ) from exc
        except OSError as exc:
            raise DaemonUnreachable(
                "no_daemon", f"cannot reach munin-rec on {target}: {exc}"
            ) from exc

        sock.sendall(Request(cmd=cmd, args=dict(args)).to_line())

        buffer = bytearray()
        while b"\n" not in buffer:
            try:
                chunk = sock.recv(MAX_LINE_BYTES)
            except TimeoutError as exc:
                raise IpcError(
                    "internal", f"munin-rec did not answer {cmd!r} within {timeout:g}s"
                ) from exc
            if not chunk:
                raise DaemonUnreachable(
                    "no_daemon", f"munin-rec closed the connection during {cmd!r}"
                )
            buffer.extend(chunk)
            if len(buffer) > MAX_LINE_BYTES:
                raise IpcError("internal", "reply exceeds the line limit")
    finally:
        sock.close()

    line, _, _ = bytes(buffer).partition(b"\n")
    reply = Reply.from_line(line)
    if not reply.ok:
        error = reply.error or {}
        raise IpcError(
            str(error.get("code", "internal")), str(error.get("message", "failed"))
        )
    return reply.data or {}
