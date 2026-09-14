"""Shared fixtures.

Two things every workstream needs: a data root that is not the real ``~/munin``,
and a synthetic two-track pair to exercise the session layout and the renderer
without a meeting. Fixtures are synthetic or own-voice; no real customer audio
ever enters this corpus (spec 13).

FROZEN: see docs/superpowers/specs/2026-09-14-poc-contracts.md, section 14.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

FFMPEG = shutil.which("ffmpeg")

#: Two distinguishable tones, so a test can assert which track it is looking at.
MIC_HZ = 440
APP_HZ = 880


@pytest.fixture()
def munin_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway data root, exported as ``$MUNIN_HOME``.

    Every module resolves the root through ``munin.paths.munin_home``, which
    reads this variable first, so nothing under test can reach the real one.
    """
    home = tmp_path / "munin"
    (home / "recordings").mkdir(parents=True)
    (home / "inbox").mkdir()
    (home / "voices").mkdir()
    monkeypatch.setenv("MUNIN_HOME", str(home))
    return home


@pytest.fixture()
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway ``$XDG_RUNTIME_DIR`` for the socket and ``state.json``."""
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    return run


@pytest.fixture()
def two_track(tmp_path: Path) -> Callable[..., tuple[Path, Path]]:
    """Factory: write a synthetic ``(mic, app)`` Opus pair into a directory.

    Sine tones an octave apart at 24 kbps mono, matching the real encode
    settings, so separation assertions have something to measure. Skips the test
    when ffmpeg is absent rather than failing it.
    """

    def _make(
        directory: Path | None = None,
        *,
        seconds: float = 2.0,
        mic_name: str = "mic.opus",
        app_name: str = "app.opus",
    ) -> tuple[Path, Path]:
        if FFMPEG is None:
            pytest.skip("ffmpeg is not installed; synthetic fixtures need it")
        target = directory or tmp_path
        target.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, hz in ((mic_name, MIC_HZ), (app_name, APP_HZ)):
            out = target / name
            subprocess.run(
                [
                    FFMPEG, "-nostdin", "-loglevel", "error", "-y",
                    "-f", "lavfi",
                    "-i", f"sine=frequency={hz}:duration={seconds}:sample_rate=48000",
                    "-ac", "1", "-c:a", "libopus", "-b:a", "24k",
                    str(out),
                ],
                check=True,
                env={**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "")},
            )
            paths.append(out)
        return (paths[0], paths[1])

    return _make
