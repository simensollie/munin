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

Owner: daemon workstream. Contract: PoC contracts section 11.
"""

from __future__ import annotations

import argparse

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


def build_parser() -> argparse.ArgumentParser:
    """start, stop, toggle, status, list, event, doctor, setup, daemon, worker."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError
