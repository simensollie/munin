"""The naming rules of contracts section 2.

The directory name is the index: it has to say when and what without anything
being opened, and it has to survive Norwegian letters and a calendar title with
a colon and a slash in it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from munin import paths


# -- slugs ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Weekly quality sync", "weekly-quality-sync"),
        # ae / oe / aa fold before NFKD, which would otherwise drop the vowel.
        ("Kvalitetsmøte", "kvalitetsmoete"),
        ("Rådata", "raadata"),
        ("Årsmøte i NORVA", "aarsmoete-i-norva"),
        ("Smørebrød og øl", "smoerebroed-og-oel"),
        # A calendar subject with the two characters pensieve also sanitises.
        ("Beacon 365: rollout / phase 2", "beacon-365-rollout-phase-2"),
        # Accents that NFKD can strip on its own.
        ("Café résumé", "cafe-resume"),
        ("  padded  ", "padded"),
        ("multiple   ---   separators", "multiple-separators"),
    ],
)
def test_slugify_folds_and_sanitises(title: str, expected: str) -> None:
    assert paths.slugify(title) == expected


@pytest.mark.parametrize("title", ["", "   ", "!!!", "///", "——"])
def test_an_empty_slug_becomes_adhoc(title: str) -> None:
    assert paths.slugify(title) == paths.FALLBACK_SLUG


def test_slug_truncates_at_a_word_boundary() -> None:
    title = "quarterly management review of every single open quality deviation"
    slug = paths.slugify(title)
    assert len(slug) <= paths.SLUG_MAX_LENGTH
    assert not slug.endswith("-")
    # The cut lands between words, never mid-word.
    assert title.lower().replace(" ", "-").startswith(slug)
    # This one happens to fill the 48 exactly, so nothing is trimmed back.
    assert slug == "quarterly-management-review-of-every-single-open"


def test_slug_cut_mid_word_drops_the_partial_word() -> None:
    title = "quarterly management review of every single deviation"
    slug = paths.slugify(title)
    assert slug == "quarterly-management-review-of-every-single"
    assert len(slug) < paths.SLUG_MAX_LENGTH


def test_slug_exactly_at_the_limit_is_untouched() -> None:
    slug = paths.slugify("a" * paths.SLUG_MAX_LENGTH)
    assert slug == "a" * paths.SLUG_MAX_LENGTH


def test_a_single_long_word_is_cut_hard() -> None:
    """There is no boundary to cut at, so keep 48 characters rather than lose it."""
    slug = paths.slugify("b" * 90)
    assert slug == "b" * paths.SLUG_MAX_LENGTH


def test_boundary_cut_does_not_leave_a_trailing_dash() -> None:
    title = "a" * paths.SLUG_MAX_LENGTH + " tail"
    assert paths.slugify(title) == "a" * paths.SLUG_MAX_LENGTH


# -- ids and directories ----------------------------------------------------


def test_session_id_is_local_time_to_the_minute() -> None:
    created = datetime(2026, 9, 14, 13, 25, 7)
    assert paths.session_id(created, "Weekly quality sync") == (
        "2026-09-14T1325-weekly-quality-sync"
    )


def test_session_id_has_no_seconds_and_no_timezone_suffix() -> None:
    sid = paths.session_id(datetime(2026, 1, 2, 3, 4, 5), "x")
    assert sid == "2026-01-02T0304-x"


def test_session_dir_uses_the_created_month(tmp_path: Path) -> None:
    created = datetime(2026, 9, 14, 23, 58)
    sid = paths.session_id(created, "Late one")
    directory = paths.session_dir(tmp_path, created, sid)
    assert directory == tmp_path / "recordings" / "2026" / "09" / sid


def test_session_dir_honours_a_renamed_recordings_folder(tmp_path: Path) -> None:
    created = datetime(2026, 9, 14, 8, 0)
    directory = paths.session_dir(tmp_path, created, "sid", "opptak")
    assert directory == tmp_path / "opptak" / "2026" / "09" / "sid"


# -- the root ---------------------------------------------------------------


def test_munin_home_prefers_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MUNIN_HOME", str(tmp_path / "from-env"))
    assert paths.munin_home("/some/configured/place") == tmp_path / "from-env"


def test_munin_home_falls_back_to_the_config_then_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MUNIN_HOME", raising=False)
    assert paths.munin_home("~/elsewhere") == Path.home() / "elsewhere"
    assert paths.munin_home(None) == Path.home() / "munin"


def test_home_layout_is_one_place(tmp_path: Path) -> None:
    assert paths.recordings_root(tmp_path).name == "recordings"
    assert paths.inbox_dir(tmp_path).name == "inbox"
    assert paths.voices_dir(tmp_path).name == "voices"
    assert paths.latest_link(tmp_path).name == "latest"
    assert paths.log_file(tmp_path).name == "munin.log"
    assert paths.config_file(tmp_path).name == "config.toml"


# -- the runtime directory --------------------------------------------------


def test_runtime_dir_is_private_and_created(runtime_dir: Path) -> None:
    created = paths.runtime_dir()
    assert created == runtime_dir / "munin"
    assert created.is_dir()
    assert created.stat().st_mode & 0o777 == 0o700
    assert paths.socket_path() == created / paths.SOCKET_NAME
    assert paths.state_path() == created / paths.STATE_NAME


def test_runtime_dir_without_the_variable_is_a_named_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to /tmp would survive a reboot and confuse the plugin."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with pytest.raises(RuntimeError, match="XDG_RUNTIME_DIR"):
        paths.runtime_dir()
