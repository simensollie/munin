"""The desktop-integration interface: idle inhibition, for now.

A recording must outlive the idle timer. On Omarchy that is one command and one
state file; on macOS it is ``caffeinate``; on Windows it is
``SetThreadExecutionState``. None of that belongs in :mod:`munin.daemon`, which
is platform-free by the same rule that keeps PipeWire inside
:mod:`munin.capture` (spec 16.5).

Idle inhibition is a convenience, never a precondition: every method here is
allowed to fail quietly, and the daemon keeps recording when it does.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Callable

__all__ = ["IdleInhibitor", "NullIdleInhibitor", "Runner", "run_quiet"]

#: Runs an argv, returns its exit code. Injected so tests never shell out.
Runner = Callable[[Sequence[str]], int]


def run_quiet(argv: Sequence[str]) -> int:
    """Run ``argv`` with all three streams closed and return its exit code."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode


class IdleInhibitor(ABC):
    """Keeps the session awake while a recording runs."""

    #: Goes into ``munin doctor`` output; names the mechanism, not the platform.
    method: str = "none"

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner: Runner = runner or run_quiet

    @abstractmethod
    def is_inhibited(self) -> bool:
        """True when something is already holding the session awake.

        The daemon reads this *before* inhibiting and restores it afterwards, so
        a user who keeps the machine awake permanently does not lose that when a
        recording ends.
        """

    @abstractmethod
    def inhibit(self) -> None:
        """Hold the session awake. Must not raise."""

    @abstractmethod
    def release(self) -> None:
        """Let the session idle again. Must not raise."""

    def describe(self) -> dict[str, str]:
        return {"method": self.method}


class NullIdleInhibitor(IdleInhibitor):
    """Does nothing, for a platform with no inhibitor wired up yet.

    A missing inhibitor means the screen may lock during a long meeting. The
    recording is unaffected -- capture does not stop when the session locks --
    so this degrades rather than refusing to record.
    """

    method = "none"

    def is_inhibited(self) -> bool:
        return False

    def inhibit(self) -> None:
        return None

    def release(self) -> None:
        return None
