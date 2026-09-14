"""The detection interface. The two conditions of spec 6.3 *are* the interface.

Condition 1 -- a call is live: one process holds a playback stream and a capture
stream at the same time. This removes the whole false-positive class; a browser
playing video has the first and not the second.

Condition 2 -- it is Teams: matched against a configured table of application
shapes, PipeWire client first, then window class, then window title. Adding Zoom,
Meet or Slack is a row in that table, never code (D13).

Per-platform code supplies evidence for each condition and nothing else.
:func:`identify` is pure, so the detection matrix of spec 13 is tested without
hardware.

FROZEN: see docs/superpowers/specs/2026-09-14-poc-contracts.md, section 6.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal, Sequence

__all__ = [
    "AppRule",
    "CallEvidence",
    "DetectedCall",
    "Detector",
    "Identity",
    "WindowInfo",
    "identify",
]

MatchedBy = Literal["pipewire", "window_class", "window_title"]


@dataclass(frozen=True)
class CallEvidence:
    """Condition 1, for one process.

    ``playback_handle`` is the opaque platform token for that process's output
    stream, handed to ``CaptureTarget(kind="app")`` if the user says yes.
    """

    pid: int
    has_playback: bool
    has_capture: bool
    playback_handle: str | None = None
    client_name: str | None = None
    binary: str | None = None

    @property
    def is_call(self) -> bool:
        """Both halves present: a live call, whatever application it is."""
        return self.has_playback and self.has_capture


@dataclass(frozen=True)
class WindowInfo:
    """What the window manager says about the process owning a stream."""

    pid: int
    window_class: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class AppRule:
    """One row of the ``[[detection.apps]]`` table (spec 6.3).

    A rule matches on the first of its populated fields that succeeds, in the
    order PipeWire client, binary, window class, window title. Empty fields are
    not constraints; a rule with no populated field never matches.
    """

    app_id: str
    label: str
    client_name: str | None = None
    binary: str | None = None
    window_class: str | None = None
    window_title_contains: str | None = None


@dataclass(frozen=True)
class Identity:
    """Condition 2: which configured application this call belongs to."""

    app_id: str
    label: str
    matched_by: MatchedBy


@dataclass(frozen=True)
class DetectedCall:
    """A live call, with an identity when one of the rules matched."""

    evidence: CallEvidence
    identity: Identity | None
    window: WindowInfo | None
    observed_at: datetime


def identify(
    evidence: CallEvidence,
    window: WindowInfo | None,
    rules: Sequence[AppRule],
) -> Identity | None:
    """Match one live call against the configured application table.

    Match order is fixed and deliberate (spec 6.3): the PipeWire client first,
    because a native application is unambiguous and costs nothing, then the
    window class of the process owning the stream, then the window title. Rules
    are tried in file order within each stage, so the table's order is the
    tie-break.

    Returns ``None`` when nothing matches -- a live call in an application Munin
    was not asked to care about.
    """
    for rule in rules:
        if rule.client_name and evidence.client_name:
            if rule.client_name.casefold() == evidence.client_name.casefold():
                return Identity(rule.app_id, rule.label, "pipewire")
        if rule.binary and evidence.binary:
            if rule.binary.casefold() == evidence.binary.casefold():
                return Identity(rule.app_id, rule.label, "pipewire")

    if window is not None:
        for rule in rules:
            if rule.window_class and window.window_class:
                if rule.window_class.casefold() == window.window_class.casefold():
                    return Identity(rule.app_id, rule.label, "window_class")
        for rule in rules:
            if rule.window_title_contains and window.title:
                if rule.window_title_contains.casefold() in window.title.casefold():
                    return Identity(rule.app_id, rule.label, "window_title")

    return None


class Detector(ABC):
    """Supplies evidence for the two conditions on one platform.

    Subclasses implement :meth:`evidence` and :meth:`window_for_pid` only. The
    combination of the two conditions lives here, once, for every platform.
    """

    #: ``sys.platform`` value this detector serves.
    platform: ClassVar[str] = "abstract"

    def __init__(self, rules: Sequence[AppRule]) -> None:
        self.rules = list(rules)

    @abstractmethod
    def evidence(self) -> list[CallEvidence]:
        """One entry per process currently holding any audio stream."""

    @abstractmethod
    def window_for_pid(self, pid: int) -> WindowInfo | None:
        """The window owning ``pid``, walking parents if the audio process is a child.

        Returns ``None`` when the window manager cannot be reached, which
        degrades detection to PipeWire-only matching rather than failing.
        """

    @abstractmethod
    def describe(self) -> dict[str, str]:
        """Flat, printable facts for ``munin doctor``."""

    def scan(self, *, now: datetime | None = None) -> list[DetectedCall]:
        """Every live call visible right now, identified where a rule matches.

        Concrete and platform-free: condition 1 filters, condition 2 labels. A
        call with no matching rule is still returned, with ``identity=None``, so
        ``munin doctor`` can show what it saw and chose not to act on.
        """
        from datetime import datetime as _dt

        observed_at = now or _dt.now().astimezone()
        calls: list[DetectedCall] = []
        for item in self.evidence():
            if not item.is_call:
                continue
            window = self.window_for_pid(item.pid)
            calls.append(
                DetectedCall(
                    evidence=item,
                    identity=identify(item, window, self.rules),
                    window=window,
                    observed_at=observed_at,
                )
            )
        return calls
