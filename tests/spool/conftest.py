"""Fixtures for the spool, config and paths tests.

Everything here is deterministic: no PipeWire, no real disk-space numbers, no
audio. The one thing these tests touch outside ``tmp_path`` is the process
environment, and only through ``monkeypatch``.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import pytest

from munin import config as config_module
from munin.spool import Spool

#: A fixed local offset, so a test's expected ISO string does not depend on the
#: machine's timezone.
OSLO = timezone(timedelta(hours=2))

#: A meeting on a Monday afternoon, 24-hour clock.
WHEN = datetime(2026, 9, 14, 13, 25, 7, tzinfo=OSLO)


class _Usage(NamedTuple):
    total: int
    used: int
    free: int


@pytest.fixture()
def ample_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend there is 400 GiB free, whatever the machine running the tests has."""
    gib = 1024**3
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _Usage(500 * gib, 100 * gib, 400 * gib)
    )


@pytest.fixture()
def tight_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """100 MiB free: under the 2048 MiB the contract refuses to record below."""
    mib = 1024**2
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _Usage(500 * mib, 400 * mib, 100 * mib)
    )


@pytest.fixture()
def cfg(munin_home: Path) -> config_module.Config:
    """Defaults, rooted at the throwaway ``$MUNIN_HOME``."""
    return config_module.load()


@pytest.fixture()
def spool(cfg: config_module.Config, ample_disk: None) -> Spool:
    """A spool over the throwaway root, with plenty of pretend disk."""
    return Spool(cfg)


def with_resume_window(spool: Spool, seconds: int) -> Spool:
    """The same spool with a different resume window (D15)."""
    detection = replace(spool.config.detection, resume_window_seconds=seconds)
    return Spool(replace(spool.config, detection=detection))
