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

Owner: worker workstream. Signatures fixed by PoC contracts section 10.
"""

from __future__ import annotations

from typing import Sequence

from munin.backends.base import Transcript

__all__ = [
    "GAP_TEMPLATE",
    "format_timestamp",
    "gap_line",
    "render_transcript",
    "pensieve_filename",
]

GAP_TEMPLATE = "[{ts}] --- recording resumed (gap {minutes} min) ---"


def format_timestamp(seconds: float) -> str:
    """Seconds to ``HH:MM:SS``. Hours are unbounded, never wrapped (D7)."""
    raise NotImplementedError


def gap_line(at_seconds: float, gap_seconds: float) -> str:
    """The marker written between two segments of a resumed session (D15)."""
    raise NotImplementedError


def render_transcript(
    transcript: Transcript, *, gaps: Sequence[tuple[float, float]] = ()
) -> str:
    """``[HH:MM:SS - HH:MM:SS] <display name>: <text>``, one line per segment.

    Enforces monotonicity: each segment's start is clamped up to the previous
    end, and a segment left with ``end <= start`` is dropped rather than emitted
    with a negative duration. ``gaps`` is ``(at_seconds, gap_seconds)`` pairs, in
    timeline order.
    """
    raise NotImplementedError


def pensieve_filename(title: str) -> str:
    """``<title>-transcript.txt`` with ``:`` and ``/`` replaced by ``_``.

    Matches the existing pensieve ingest convention exactly (spec 7.5).
    """
    raise NotImplementedError
