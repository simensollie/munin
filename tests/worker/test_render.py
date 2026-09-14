"""Output conformance for ``pipeline.render`` (PoC contracts section 10).

No PipeWire, no filesystem beyond what pytest's ``tmp_path`` gives for free --
these are pure-function tests over ``Transcript``/``TranscriptSegment``.
"""

from __future__ import annotations

from munin.backends.base import Transcript, TranscriptSegment
from munin.pipeline.render import (
    adjust_segments,
    format_timestamp,
    gap_line,
    pensieve_filename,
    render_transcript,
)


def _transcript(segments: list[TranscriptSegment]) -> Transcript:
    return Transcript(segments=segments, language="nb", backend="fake")


# ---------------------------------------------------------------------------
# format_timestamp


def test_format_timestamp_basic() -> None:
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(59) == "00:00:59"
    assert format_timestamp(60) == "00:01:00"
    assert format_timestamp(3661) == "01:01:01"


def test_format_timestamp_past_100_minutes_and_past_24_hours() -> None:
    # 100 minutes exactly.
    assert format_timestamp(100 * 60) == "01:40:00"
    # Past 24 hours: D7 says hours are unbounded, never wrapped.
    assert format_timestamp(25 * 3600) == "25:00:00"
    assert format_timestamp(103 * 3600 + 6 * 60 + 7) == "103:06:07"


def test_format_timestamp_negative_is_clamped_to_zero() -> None:
    assert format_timestamp(-5) == "00:00:00"


# ---------------------------------------------------------------------------
# gap_line


def test_gap_line_matches_template() -> None:
    line = gap_line(3600, 300)
    assert line == "[01:00:00] --- recording resumed (gap 5 min) ---"


# ---------------------------------------------------------------------------
# adjust_segments / monotonicity


def test_adjust_segments_passes_through_already_monotonic_input() -> None:
    segments = [
        TranscriptSegment(start=0.0, end=5.0, speaker="Ola Nordmann", text="Hei."),
        TranscriptSegment(start=5.0, end=9.0, speaker="Kari Nordmann", text="Hei, hei."),
    ]
    adjusted, report = adjust_segments(segments)
    assert adjusted == segments
    assert report == []


def test_adjust_segments_clamps_an_overlapping_start() -> None:
    segments = [
        TranscriptSegment(start=0.0, end=5.0, speaker="Ola Nordmann", text="Hei."),
        # Starts before the previous segment ended.
        TranscriptSegment(start=3.0, end=9.0, speaker="Kari Nordmann", text="Hei, hei."),
    ]
    adjusted, report = adjust_segments(segments)
    assert [s.start for s in adjusted] == [0.0, 5.0]
    assert [s.end for s in adjusted] == [5.0, 9.0]
    assert len(report) == 1
    assert report[0]["action"] == "clamped_start"
    assert report[0]["original_start"] == 3.0
    assert report[0]["adjusted_start"] == 5.0


def test_adjust_segments_drops_negative_duration_after_clamping() -> None:
    segments = [
        TranscriptSegment(start=0.0, end=5.0, speaker="Ola Nordmann", text="Hei."),
        # Entirely inside the previous segment: clamping leaves end <= start.
        TranscriptSegment(start=1.0, end=2.0, speaker="Kari Nordmann", text="Avbrutt."),
        TranscriptSegment(start=6.0, end=8.0, speaker="Kari Nordmann", text="Fortsetter."),
    ]
    adjusted, report = adjust_segments(segments)
    assert [s.text for s in adjusted] == ["Hei.", "Fortsetter."]
    assert len(report) == 1
    assert report[0]["action"] == "dropped"
    assert report[0]["index"] == 1


def test_adjust_segments_rejects_a_segment_with_negative_duration_outright() -> None:
    # end < start even before any clamping is applied.
    segments = [TranscriptSegment(start=5.0, end=2.0, speaker="Ola Nordmann", text="?")]
    adjusted, report = adjust_segments(segments)
    assert adjusted == []
    assert report[0]["action"] == "dropped"
    assert report[0]["original_start"] == 5.0
    assert report[0]["original_end"] == 2.0


# ---------------------------------------------------------------------------
# render_transcript


def test_render_transcript_line_format() -> None:
    transcript = _transcript(
        [TranscriptSegment(start=0.0, end=3.0, speaker="Ola Nordmann", text="Hei, alle sammen.")]
    )
    text = render_transcript(transcript)
    assert text == "[00:00:00 - 00:00:03] Ola Nordmann: Hei, alle sammen.\n"


def test_render_transcript_drops_and_clamps_silently_in_the_txt() -> None:
    transcript = _transcript(
        [
            TranscriptSegment(start=0.0, end=5.0, speaker="Ola Nordmann", text="Hei."),
            TranscriptSegment(start=1.0, end=2.0, speaker="Kari Nordmann", text="Avbrutt."),
            TranscriptSegment(start=3.0, end=9.0, speaker="Kari Nordmann", text="Hei, hei."),
        ]
    )
    text = render_transcript(transcript)
    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0] == "[00:00:00 - 00:00:05] Ola Nordmann: Hei."
    # Clamped up to 5.0, not the original 3.0.
    assert lines[1] == "[00:00:05 - 00:00:09] Kari Nordmann: Hei, hei."


def test_render_transcript_empty_segments_is_empty_string() -> None:
    assert render_transcript(_transcript([])) == ""


def test_render_transcript_two_segment_concatenation_with_gap_marker() -> None:
    """A resumed session: two capture segments already offset onto one
    captured-audio timeline, with a wall-clock gap marked between them
    (contracts section 10 / spec D15)."""
    transcript = _transcript(
        [
            TranscriptSegment(start=0.0, end=10.0, speaker="Ola Nordmann", text="Første del."),
            # Offset by the first segment's duration; the wall-clock break
            # itself (e.g. a 5-minute pause) is not represented as audio.
            TranscriptSegment(start=10.0, end=16.0, speaker="Ola Nordmann", text="Andre del."),
        ]
    )
    gaps = [(10.0, 300.0)]  # 5 minutes, right at the boundary
    text = render_transcript(transcript, gaps=gaps)
    lines = text.splitlines()
    assert lines == [
        "[00:00:00 - 00:00:10] Ola Nordmann: Første del.",
        "[00:00:10] --- recording resumed (gap 5 min) ---",
        "[00:00:10 - 00:00:16] Ola Nordmann: Andre del.",
    ]


# ---------------------------------------------------------------------------
# pensieve_filename


def test_pensieve_filename_sanitises_colon_and_slash() -> None:
    assert (
        pensieve_filename("Weekly quality sync: 2026/09/14")
        == "Weekly quality sync_ 2026_09_14-transcript.txt"
    )


def test_pensieve_filename_plain_title() -> None:
    assert pensieve_filename("Weekly quality sync") == "Weekly quality sync-transcript.txt"
