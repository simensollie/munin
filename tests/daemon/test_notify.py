"""The three notifications of spec 6.3, as command lines.

The contract fixes the glyph, the single ``--exec`` action each one carries and
the ``-r`` replace id that makes them update rather than stack. That is exactly
what these tests pin, because it is what the user sees.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from munin import notify


def test_detected_offers_start_from_detection() -> None:
    note = notify.detected("Beacon 365", "13:25")
    assert note.title == "Meeting detected"
    assert "Beacon 365" in note.body and "13:25" in note.body
    assert note.action == ("munin", "start", "--from-detection")
    assert note.urgency == "normal"
    assert note.timeout_ms == 30000


def test_ending_soon_offers_stop() -> None:
    note = notify.ending_soon(60, session_id="2026-09-14T1325-weekly-quality-sync")
    assert note.title == "Meeting looks finished"
    assert note.body == "Stops by itself in 1:00"
    assert note.action == ("munin", "stop")
    assert note.timeout_ms == 60000


def test_auto_stopped_always_offers_resume() -> None:
    note = notify.auto_stopped(37)
    assert note.title == "Recording stopped"
    assert note.body == "37 min captured, queued for transcription"
    assert note.action == ("munin", "start", "--resume")
    assert note.urgency == "normal"


def test_a_failed_stop_is_critical_and_says_the_audio_is_kept() -> None:
    note = notify.auto_stopped(4, failed=True)
    assert note.urgency == "critical"
    assert "kept" in note.body


def test_the_replace_id_is_stable_per_session_and_numeric() -> None:
    first = notify.replace_id_for("2026-09-14T1325-weekly-quality-sync")
    second = notify.replace_id_for("2026-09-14T1325-weekly-quality-sync")
    other = notify.replace_id_for("2026-09-14T1500-risk-review")
    assert first == second != other
    assert first is not None and first.isdigit()
    assert notify.replace_id_for(None) is None


def test_argv_matches_the_wrapper_the_machine_has() -> None:
    note = notify.detected("Beacon 365", "13:25", session_id="s1")
    argv = notify.build_argv(note)
    assert argv[0] == "omarchy-notification-send"
    assert argv[1:3] == ["-g", notify.GLYPH]
    assert "-u" in argv and "-t" in argv and "-r" in argv
    # --exec swallows the rest, so it must be last.
    assert argv[argv.index("--exec") + 1 :] == ["munin", "start", "--from-detection"]
    assert argv.index("--exec") == len(argv) - 4


@pytest.mark.parametrize(("seconds", "expected"), [(0, "0:00"), (59, "0:59"), (120, "2:00"), (3661, "1:01:01")])
def test_clock_formatting_is_locale_neutral(seconds: int, expected: str) -> None:
    assert notify.format_clock(seconds) == expected


def test_send_uses_the_injected_runner() -> None:
    seen: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> int:
        seen.append(list(argv))
        return 0

    assert notify.send(notify.auto_stopped(2), runner=runner) is True
    assert seen and seen[0][0] == "omarchy-notification-send"


def test_a_failing_wrapper_never_raises() -> None:
    def runner(argv: Sequence[str]) -> int:
        raise OSError("no session bus")

    assert notify.send(notify.auto_stopped(2), runner=runner) is False


def test_a_nonzero_exit_is_reported_as_false() -> None:
    assert notify.send(notify.auto_stopped(2), runner=lambda argv: 1) is False


def test_disabled_notifications_send_nothing() -> None:
    sent: list[Sequence[str]] = []
    assert notify.send(notify.auto_stopped(2), enabled=False, runner=sent.append) is False
    assert sent == []


def test_a_missing_wrapper_is_a_log_line_not_a_crash(monkeypatch) -> None:
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    assert notify.send(notify.auto_stopped(2)) is False
