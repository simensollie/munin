"""``munin setup``: the four questions that have no sensible default.

Backend, Microsoft 365 sign-in, which microphone, and a ten-second test
recording played back to confirm track separation (spec 15). The PoC asks only
the microphone and runs the test recording; the other two are deferred with the
features behind them.

``--non-interactive`` is what ``install.sh`` calls: create the data root and
write a default ``config.toml``, ask nothing.

Owner: install workstream.
"""

from __future__ import annotations

from pathlib import Path

from munin.config import Config

__all__ = ["ensure_home", "write_default_config", "main"]


def ensure_home(home: Path) -> Path:
    """Create ``~/munin`` and its subdirectories. Idempotent, never destructive."""
    raise NotImplementedError


def write_default_config(home: Path, *, overwrite: bool = False) -> Path:
    """Write ``config.toml`` from ``config.DEFAULT_CONFIG_TOML``.

    Refuses to overwrite an existing file unless asked: a config is hand-edited
    and an installer that clobbers it is one people stop running.
    """
    raise NotImplementedError


def main(config: Config, *, non_interactive: bool = False) -> int:
    raise NotImplementedError
