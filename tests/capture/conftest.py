"""Fixtures for the capture and detection tests.

Everything here is deterministic and offline: the dumps are committed JSON, the
parent-pid walk reads a committed map instead of ``/proc``, and no test in this
directory needs PipeWire, Hyprland or audio hardware.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from munin.detect.base import AppRule, WindowInfo
from munin.detect.linux import parse_windows

FIXTURES = Path(__file__).parent / "fixtures"

#: The three Teams shapes of spec 6.3, in the file order of contracts 9, which
#: is also the match order.
TEAMS_RULES: tuple[AppRule, ...] = (
    AppRule(app_id="teams-native", label="Microsoft Teams", client_name="Teams"),
    AppRule(
        app_id="teams-pwa",
        label="Microsoft Teams",
        window_class="chrome-teams.microsoft.com__-Default",
    ),
    AppRule(
        app_id="teams-tab",
        label="Microsoft Teams",
        window_title_contains="Microsoft Teams",
    ),
)


def load_fixture(name: str) -> Any:
    """Decode one committed fixture by file name."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def dump() -> Callable[[str], list[dict[str, Any]]]:
    """Factory: load a ``pw-dump`` fixture by its shape name.

    ``dump("teams-native")`` reads ``pw-dump-teams-native.json``.
    """

    def _load(shape: str) -> list[dict[str, Any]]:
        return load_fixture(f"pw-dump-{shape}.json")

    return _load


@pytest.fixture()
def windows() -> list[WindowInfo]:
    """Every window in the committed ``hyprctl clients -j`` fixture."""
    return parse_windows(load_fixture("hyprctl-clients.json"))


@pytest.fixture()
def ppid_of() -> Callable[[int], int | None]:
    """``/proc/<pid>/status`` PPid lookup, backed by the committed map."""
    table = {int(k): int(v) for k, v in load_fixture("ppid-map.json").items()}

    def _ppid(pid: int) -> int | None:
        return table.get(pid)

    return _ppid


@pytest.fixture()
def rules() -> list[AppRule]:
    """The configured application table, in match order."""
    return list(TEAMS_RULES)
