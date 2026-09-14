"""PipeWire + Hyprland detection evidence (spec 6.3, 16.3).

Verified on this machine: process identity lives on the PipeWire *Client*
object, not on the stream node. Build ``client.id -> (pid, binary,
application.name)`` from the Client objects in ``pw-dump``, then join every node
whose ``media.class`` starts with ``Stream/`` to its client. A pid holding both
``Stream/Output/Audio`` and ``Stream/Input/Audio`` satisfies condition 1.

The window owning an audio stream is often a parent of the process that holds it
(Chrome's audio process sits one hop below its window), so the pid walk through
``/proc/<pid>/status`` is bounded rather than assumed to be exactly one hop.

Owner: capture workstream.
"""

from __future__ import annotations

from typing import ClassVar

from munin.detect.base import CallEvidence, Detector, WindowInfo

__all__ = ["PipewireDetector", "MAX_PARENT_HOPS"]

#: How far up ``/proc/<pid>/status`` PPid to look for the window owning a stream.
MAX_PARENT_HOPS = 5


class PipewireDetector(Detector):
    """Evidence from ``pw-dump``, window facts from ``hyprctl clients -j``."""

    platform: ClassVar[str] = "linux"

    def evidence(self) -> list[CallEvidence]:
        raise NotImplementedError

    def window_for_pid(self, pid: int) -> WindowInfo | None:
        raise NotImplementedError

    def describe(self) -> dict[str, str]:
        raise NotImplementedError
