"""Fixtures for the daemon tests: a daemon with every collaborator injected.

No test here touches PipeWire, a socket path outside ``tmp_path``, the real
``~/munin`` or the real stay-awake state file, and no test sleeps: the clock is
advanced by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from munin.config import Config, DetectionConfig, IdleConfig, NotificationConfig
from munin.daemon import Daemon
from munin.notify import Notification

from .fakes import FakeClock, FakeSpool, recording_capturer_factory


class QuietDetector:
    """A detector that never sees a call, so no test shells out to pw-dump."""

    def scan(self, *, now: Any = None) -> list[Any]:
        return []

    def describe(self) -> dict[str, str]:
        return {"platform": "fake"}


@dataclass
class Harness:
    daemon: Daemon
    clock: FakeClock
    spool: FakeSpool
    home: Path
    state_path: Path
    notifications: list[Notification] = field(default_factory=list)
    idle_calls: list[list[str]] = field(default_factory=list)

    def state_json(self) -> dict[str, Any]:
        """The state file as the plugin would read it."""
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def session_json(self, session_id: str) -> dict[str, Any]:
        session = self.spool.load(session_id)
        return json.loads(session.json_path.read_text(encoding="utf-8"))

    def titles(self) -> list[str]:
        return [note.title for note in self.notifications]


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    home = tmp_path / "munin"
    home.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.setenv("MUNIN_HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    clock = FakeClock()
    config = Config(
        home=home,
        detection=DetectionConfig(source="plugin"),
        idle=IdleConfig(inhibit=True),
        notifications=NotificationConfig(enabled=True),
    )
    spool = FakeSpool(home, clock=clock)
    factory = recording_capturer_factory(clock)

    notifications: list[Notification] = []
    idle_calls: list[list[str]] = []

    def notifier(note: Notification) -> bool:
        notifications.append(note)
        return True

    def idle_runner(argv: Any) -> int:
        idle_calls.append(list(argv))
        return 0

    state_path = run / "munin" / "state.json"
    daemon = Daemon(
        config,
        spool=spool,
        capturer_factory=factory,
        notifier=notifier,
        clock=clock,
        idle_runner=idle_runner,
        detector=QuietDetector(),
        socket_path_override=run / "munin" / "rec.sock",
        state_path_override=state_path,
    )
    return Harness(
        daemon=daemon,
        clock=clock,
        spool=spool,
        home=home,
        state_path=state_path,
        notifications=notifications,
        idle_calls=idle_calls,
    )
