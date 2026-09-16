"""Transcript rendering: the output format is part of the product (spec 7.5).

Three deliberate breaks from the Plaud format Munin replaces:

- ``HH:MM:SS``, not ``MM:SS`` (D7). ``[103:06 - 103:07]`` past 100 minutes is
  unreadable, and 25 of 204 existing files exceed an hour.
- Strictly monotonic segments. 52 segments across 35 existing files start before
  the previous one ended, and 5 have negative duration.
- Stable speaker labels.

The time axis is captured-audio time, not wall clock: a resumed session's later
segments are offset by the summed duration of the earlier ones, and the
wall-clock break shows up as a gap line rather than as missing audio.

Owner: worker workstream. Signatures fixed by PoC contracts section 10:
``GAP_TEMPLATE``, ``format_timestamp``, ``gap_line`` and ``render_transcript``
keep exactly their documented signatures. ``adjust_segments``
is additional surface this workstream added so the worker can put the same
clamp/drop report into the ``transcript.json`` sidecar that ``render_transcript``
already applies to the ``.txt`` -- the contract asks for both from one
computation, not two independent ones that could disagree.
"""

from __future__ import annotations

from typing import Sequence

from munin.backends.base import Transcript, TranscriptSegment

__all__ = [
    "GAP_TEMPLATE",
    "format_timestamp",
    "gap_line",
    "render_transcript",
    "adjust_segments",
]

GAP_TEMPLATE = "[{ts}] --- recording resumed (gap {minutes} min) ---"


def format_timestamp(seconds: float) -> str:
    """Seconds to ``HH:MM:SS``. Hours are unbounded, never wrapped (D7).

    Negative input (should not occur once ``adjust_segments`` has run) is
    clamped to zero rather than producing a malformed string.
    """
    if seconds < 0:
        seconds = 0.0
    total = int(seconds)  # truncate to whole seconds; sub-second precision is not shown
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def gap_line(at_seconds: float, gap_seconds: float) -> str:
    """The marker written between two segments of a resumed session (D15)."""
    minutes = round(gap_seconds / 60)
    return GAP_TEMPLATE.format(ts=format_timestamp(at_seconds), minutes=minutes)


def adjust_segments(
    segments: Sequence[TranscriptSegment],
) -> tuple[list[TranscriptSegment], list[dict]]:
    """Clamp starts up to the previous end; drop anything left ``end <= start``.

    Returns the surviving segments (in their original order, otherwise
    unmodified) and a report of every adjustment -- ``{"index", "action":
    "clamped_start" | "dropped", ...}`` with the original and (where kept) the
    adjusted values. ``index`` refers to the position in the *input* sequence,
    so a caller can cross-reference the report against the segments a backend
    returned. This report is what ``render_transcript`` applies silently to the
    ``.txt`` and what the worker writes verbatim into the ``transcript.json``
    sidecar, so the two files can never disagree about what was dropped.
    """
    adjusted: list[TranscriptSegment] = []
    report: list[dict] = []
    previous_end = 0.0
    for index, segment in enumerate(segments):
        clamped_start = max(segment.start, previous_end)
        if clamped_start >= segment.end:
            report.append(
                {
                    "index": index,
                    "action": "dropped",
                    "original_start": segment.start,
                    "original_end": segment.end,
                    "reason": "end <= start after clamping to the previous segment's end",
                }
            )
            continue
        if clamped_start != segment.start:
            report.append(
                {
                    "index": index,
                    "action": "clamped_start",
                    "original_start": segment.start,
                    "adjusted_start": clamped_start,
                    "end": segment.end,
                }
            )
            segment = TranscriptSegment(
                start=clamped_start, end=segment.end, speaker=segment.speaker, text=segment.text
            )
        adjusted.append(segment)
        previous_end = segment.end
    return adjusted, report


def render_transcript(
    transcript: Transcript, *, gaps: Sequence[tuple[float, float]] = ()
) -> str:
    """``[HH:MM:SS - HH:MM:SS] <display name>: <text>``, one line per segment.

    Enforces monotonicity: each segment's start is clamped up to the previous
    end, and a segment left with ``end <= start`` is dropped rather than emitted
    with a negative duration (see :func:`adjust_segments`). ``gaps`` is
    ``(at_seconds, gap_seconds)`` pairs, in timeline order; each becomes a
    :func:`gap_line` interleaved at its position, before any segment that starts
    at or after it.
    """
    adjusted, _report = adjust_segments(transcript.segments)

    # (position, tie-break, line): tie-break puts a gap before a segment that
    # starts exactly where the gap sits, matching "resumed, then this segment".
    lines: list[tuple[float, int, str]] = []
    for segment in adjusted:
        line = (
            f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] "
            f"{segment.speaker}: {segment.text}"
        )
        lines.append((segment.start, 1, line))
    for at_seconds, gap_seconds in gaps:
        lines.append((at_seconds, 0, gap_line(at_seconds, gap_seconds)))

    lines.sort(key=lambda item: (item[0], item[1]))
    if not lines:
        return ""
    return "\n".join(line for _, _, line in lines) + "\n"

