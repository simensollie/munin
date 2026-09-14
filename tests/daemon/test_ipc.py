"""The wire protocol, over a real Unix socket in ``tmp_path``.

Nothing here needs the daemon: the server takes a handler map, so the protocol
is tested for what it is -- newline-delimited JSON with a version, an error
shape and a line limit.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from munin import ipc
from munin.ipc import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    AlreadyRunning,
    DaemonUnreachable,
    IpcError,
    Reply,
    Request,
    Server,
)


@pytest.fixture()
def server(tmp_path: Path):
    calls: list[tuple[str, dict]] = []

    def ping(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(("ping", args))
        return {"pid": 4211, "version": "0.1.0"}

    def boom(args: dict[str, Any]) -> dict[str, Any]:
        raise IpcError("not_recording", "nothing is being recorded")

    def explode(args: dict[str, Any]) -> dict[str, Any]:
        raise ZeroDivisionError("the daemon must survive this")

    srv = Server(tmp_path / "munin" / "rec.sock", {"ping": ping, "stop": boom, "bad": explode})
    srv.bind()
    srv.calls = calls  # type: ignore[attr-defined]
    yield srv
    srv.close()


def _serve_once(srv: Server, requests: int = 1) -> threading.Thread:
    """Pump the server on a background thread for one exchange."""

    def loop() -> None:
        handled = 0
        while handled < requests:
            handled += srv.poll(0.25)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def test_round_trip_returns_data(server: Server) -> None:
    thread = _serve_once(server)
    data = ipc.call("ping", server.path, timeout=5)
    thread.join(2)
    assert data == {"pid": 4211, "version": "0.1.0"}
    assert server.calls == [("ping", {})]  # type: ignore[attr-defined]


def test_a_failure_reply_carries_the_daemon_code(server: Server) -> None:
    thread = _serve_once(server)
    with pytest.raises(IpcError) as excinfo:
        ipc.call("stop", server.path, timeout=5)
    thread.join(2)
    assert excinfo.value.code == "not_recording"


def test_an_unknown_command_is_named(server: Server) -> None:
    thread = _serve_once(server)
    with pytest.raises(IpcError) as excinfo:
        ipc.call("teleport", server.path, timeout=5)
    thread.join(2)
    assert excinfo.value.code == "unknown_command"


def test_a_handler_that_raises_does_not_kill_the_daemon(server: Server) -> None:
    thread = _serve_once(server, requests=2)
    with pytest.raises(IpcError) as excinfo:
        ipc.call("bad", server.path, timeout=5)
    assert excinfo.value.code == "internal"
    assert ipc.call("ping", server.path, timeout=5)["pid"] == 4211
    thread.join(2)


def test_two_requests_share_one_connection(server: Server) -> None:
    thread = _serve_once(server, requests=2)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(server.path))
    sock.sendall(Request("ping", {}).to_line())
    sock.sendall(Request("ping", {}).to_line())
    buffer = b""
    while buffer.count(b"\n") < 2:
        buffer += sock.recv(4096)
    sock.close()
    thread.join(2)
    lines = [json.loads(line) for line in buffer.splitlines() if line]
    assert len(lines) == 2
    assert all(line["ok"] and line["protocol"] == PROTOCOL_VERSION for line in lines)


def test_garbage_is_a_bad_request_not_a_crash(server: Server) -> None:
    thread = _serve_once(server)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(server.path))
    sock.sendall(b"not json at all\n")
    reply = json.loads(sock.recv(4096).decode())
    sock.close()
    thread.join(2)
    assert reply["ok"] is False
    assert reply["error"]["code"] == "bad_request"


def test_no_socket_is_daemon_unreachable(tmp_path: Path) -> None:
    with pytest.raises(DaemonUnreachable):
        ipc.call("ping", tmp_path / "nothing" / "rec.sock", timeout=1)


def test_a_stale_socket_is_reclaimed(tmp_path: Path) -> None:
    path = tmp_path / "munin" / "rec.sock"
    path.parent.mkdir(parents=True)
    path.touch()  # a plain file left behind by a dead daemon
    srv = Server(path, {})
    srv.bind()
    try:
        assert path.is_socket()
    finally:
        srv.close()


def test_a_live_socket_refuses_a_second_daemon(server: Server) -> None:
    second = Server(server.path, {})
    with pytest.raises(AlreadyRunning):
        second.bind()
    assert server.path.is_socket(), "the loser must not unlink the winner's socket"


def test_request_and_reply_shapes() -> None:
    line = Request("start", {"title": "Weekly quality sync", "resume": False}).to_line()
    assert line.endswith(b"\n")
    parsed = Request.from_line(line.rstrip(b"\n"))
    assert parsed.cmd == "start"
    assert parsed.args == {"title": "Weekly quality sync", "resume": False}

    ok = json.loads(Reply.ok_reply("start", {"segment": 1}).to_line())
    assert ok == {"ok": True, "cmd": "start", "protocol": PROTOCOL_VERSION, "data": {"segment": 1}}
    bad = json.loads(Reply.error_reply("stop", "not_recording", "nothing").to_line())
    assert bad["error"] == {"code": "not_recording", "message": "nothing"}


def test_an_oversized_line_is_refused() -> None:
    with pytest.raises(IpcError) as excinfo:
        Request.from_line(b"x" * (MAX_LINE_BYTES + 1))
    assert excinfo.value.code == "bad_request"


def test_socket_path_follows_xdg_runtime_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert ipc.socket_path() == tmp_path / "munin" / "rec.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(DaemonUnreachable):
        ipc.socket_path()
