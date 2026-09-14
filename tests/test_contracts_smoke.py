"""Smoke tests over the frozen interfaces.

These assert the contract, not an implementation, so they must keep passing
through every workstream's merge. A failure here means a frozen file moved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import munin
from munin.backends import get_backend
from munin.backends.base import Backend, BackendUnavailable
from munin.capture import get_capturer, segment_filenames
from munin.capture.base import Capturer
from munin.detect import get_detector, identify
from munin.detect.base import AppRule, CallEvidence, Detector, WindowInfo
from munin.spool import TRANSITIONS


def test_version() -> None:
    assert munin.__version__


def test_segment_filenames_first_segment_is_unnumbered() -> None:
    assert segment_filenames(1) == ("mic.opus", "app.opus")
    assert segment_filenames(2) == ("mic.002.opus", "app.002.opus")
    with pytest.raises(ValueError):
        segment_filenames(0)


def test_platform_factories_resolve_on_linux() -> None:
    assert issubclass(get_capturer("linux"), Capturer)
    assert issubclass(get_detector("linux"), Detector)


def test_platform_factories_refuse_unknown_platforms() -> None:
    with pytest.raises(NotImplementedError):
        get_capturer("haiku")
    with pytest.raises(NotImplementedError):
        get_detector("haiku")


def test_nothing_above_the_boundary_imports_a_platform_module() -> None:
    """Spec 16.5: if the pipeline ever needs to know it is on Linux, the
    interface is wrong."""
    root = Path(munin.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.parent.name in {"capture", "detect"}:
            continue
        text = path.read_text(encoding="utf-8")
        for token in ("capture.linux", "detect.linux", "capture.macos", "detect.macos"):
            if token in text:
                offenders.append(f"{path.relative_to(root)}: {token}")
    assert not offenders, offenders


def test_call_needs_both_conditions() -> None:
    playback_only = CallEvidence(pid=1, has_playback=True, has_capture=False)
    both = CallEvidence(pid=1, has_playback=True, has_capture=True)
    assert not playback_only.is_call
    assert both.is_call


@pytest.mark.parametrize(
    ("client_name", "window", "expected"),
    [
        ("Teams", None, "pipewire"),
        ("Chromium", WindowInfo(1, "chrome-teams.microsoft.com__-Default", None), "window_class"),
        ("Chromium", WindowInfo(1, "google-chrome", "Weekly sync | Microsoft Teams"), "window_title"),
    ],
)
def test_three_teams_shapes_each_identify(client_name, window, expected) -> None:
    """Spec 6.3: the native app, the PWA and the browser tab each match, in that
    precedence order."""
    rules = [
        AppRule("teams-native", "Microsoft Teams", client_name="Teams"),
        AppRule(
            "teams-pwa",
            "Microsoft Teams",
            window_class="chrome-teams.microsoft.com__-Default",
        ),
        AppRule("teams-tab", "Microsoft Teams", window_title_contains="Microsoft Teams"),
    ]
    evidence = CallEvidence(
        pid=1, has_playback=True, has_capture=True, client_name=client_name
    )
    identity = identify(evidence, window, rules)
    assert identity is not None
    assert identity.matched_by == expected


def test_a_browser_with_no_teams_anywhere_does_not_identify() -> None:
    rules = [AppRule("teams-tab", "Microsoft Teams", window_title_contains="Microsoft Teams")]
    evidence = CallEvidence(pid=1, has_playback=True, has_capture=True, client_name="Chromium")
    assert identify(evidence, WindowInfo(1, "google-chrome", "Some video"), rules) is None


def test_none_backend_leaves_a_reason() -> None:
    backend = get_backend("none")
    assert isinstance(backend, Backend)
    assert backend.available() is False
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.transcribe(None)  # type: ignore[arg-type]
    assert excinfo.value.reason == "no transcription backend configured"


def test_state_machine_ownership() -> None:
    """Spec 11: after ``captured``, only the worker writes state."""
    for (src, dst), writer in TRANSITIONS.items():
        if dst in {"pending", "transcribing", "done"}:
            assert writer == "munin-work", (src, dst, writer)
        if dst in {"ending", "captured"}:
            assert writer == "munin-rec", (src, dst, writer)


def test_munin_home_fixture_is_isolated(munin_home: Path) -> None:
    import os

    assert os.environ["MUNIN_HOME"] == str(munin_home)
    assert (munin_home / "recordings").is_dir()
    assert str(Path.home()) not in str(munin_home.resolve().parent)


def test_two_track_fixture_produces_a_pair(tmp_path: Path, two_track) -> None:
    mic, app = two_track(tmp_path / "session", seconds=0.5)
    assert mic.name == "mic.opus" and app.name == "app.opus"
    assert mic.stat().st_size > 0 and app.stat().st_size > 0


def test_platform_is_linux_for_this_poc() -> None:
    assert sys.platform == "linux"
