"""Platform factory for desktop integration.

Mirrors :mod:`munin.capture` and :mod:`munin.detect`: the only module here that
consults ``sys.platform``, with lazy imports of the platform modules (spec
16.5). Unlike those two, an unsupported platform is not an error -- it falls
back to :class:`NullIdleInhibitor`, because a meeting that records without
inhibiting idle is still a recorded meeting.
"""

from __future__ import annotations

import importlib
import sys

from munin.desktop.base import IdleInhibitor, NullIdleInhibitor, Runner, run_quiet

__all__ = [
    "IdleInhibitor",
    "NullIdleInhibitor",
    "Runner",
    "get_idle_inhibitor",
    "run_quiet",
]

_MODULES: dict[str, tuple[str, str]] = {
    "linux": ("munin.desktop.linux", "OmarchyIdleInhibitor"),
    "darwin": ("munin.desktop.macos", "CaffeinateIdleInhibitor"),
    "win32": ("munin.desktop.windows", "ExecutionStateIdleInhibitor"),
}


def get_idle_inhibitor(platform: str | None = None) -> type[IdleInhibitor]:
    """Return the :class:`IdleInhibitor` for ``platform`` (default: this one).

    Falls back to :class:`NullIdleInhibitor` rather than raising: the daemon
    calls this on every start, and no platform gap should stop a recording.
    """
    key = platform or sys.platform
    entry = _MODULES.get(key)
    if entry is None:
        return NullIdleInhibitor
    module_name, class_name = entry
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return NullIdleInhibitor
    return getattr(module, class_name, NullIdleInhibitor)
