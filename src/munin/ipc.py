"""The daemon's wire protocol: a Unix socket, one JSON object per line.

One client of this protocol exists -- the CLI. The shell plugin deliberately
does not speak it (D20 in spirit): it reads ``state.json`` and runs ``munin``,
so a shell reload can never hold the daemon's socket open.

Owner: daemon workstream. Protocol: PoC contracts section 7.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "MAX_LINE_BYTES",
    "COMMANDS",
    "Request",
    "Reply",
    "IpcError",
    "DaemonUnreachable",
    "Server",
    "call",
]

MAX_LINE_BYTES = 64 * 1024

COMMANDS: tuple[str, ...] = (
    "ping",
    "status",
    "start",
    "stop",
    "toggle",
    "event",
    "list",
)


class IpcError(RuntimeError):
    """A structured failure reply. ``code`` is one of the contract's error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DaemonUnreachable(IpcError):
    """No socket, or nothing listening on it. The CLI exits 3."""


@dataclass(frozen=True)
class Request:
    cmd: str
    args: dict[str, Any]

    @classmethod
    def from_line(cls, line: bytes) -> "Request":
        raise NotImplementedError


@dataclass(frozen=True)
class Reply:
    ok: bool
    cmd: str
    data: dict[str, Any] | None = None
    error: dict[str, str] | None = None

    def to_line(self) -> bytes:
        """JSON plus a trailing newline, UTF-8."""
        raise NotImplementedError

    @classmethod
    def ok_reply(cls, cmd: str, data: dict[str, Any]) -> "Reply":
        raise NotImplementedError

    @classmethod
    def error_reply(cls, cmd: str, code: str, message: str) -> "Reply":
        raise NotImplementedError


class Server:
    """Accepts connections on the daemon socket and dispatches to handlers.

    Single-threaded and serial: every command either completes quickly or starts
    a subprocess, so a queue of one is enough and there is no locking to get
    wrong.
    """

    def __init__(
        self, path: Path, handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]]
    ) -> None:
        self.path = path
        self.handlers = handlers

    def bind(self) -> None:
        """Create the socket, 0600, cleaning up a stale one.

        Connects to any existing socket first: a refused connection means the
        previous daemon died and the path may be unlinked; a successful one means
        another daemon is running and this process must exit 3.
        """
        raise NotImplementedError

    def serve_forever(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def call(cmd: str, path: Path | None = None, **args: Any) -> dict[str, Any]:
    """Send one request from the CLI and return ``data``.

    Raises :class:`DaemonUnreachable` when nothing is listening, and
    :class:`IpcError` carrying the daemon's own code for a failure reply.
    """
    raise NotImplementedError
