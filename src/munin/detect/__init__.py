"""Platform factory for the detection layer.

Mirrors :mod:`munin.capture`: the only module here that consults
``sys.platform``, with lazy imports of the platform modules (spec 16.5).

FROZEN: see docs/superpowers/specs/2026-09-14-poc-contracts.md, section 6.
"""

from __future__ import annotations

import importlib
import sys

from munin.detect.base import (
    AppRule,
    CallEvidence,
    DetectedCall,
    Detector,
    Identity,
    WindowInfo,
    identify,
)

__all__ = [
    "AppRule",
    "CallEvidence",
    "DetectedCall",
    "Detector",
    "Identity",
    "WindowInfo",
    "get_detector",
    "identify",
]

_MODULES: dict[str, tuple[str, str]] = {
    "linux": ("munin.detect.linux", "PipewireDetector"),
    "darwin": ("munin.detect.macos", "CoreAudioDetector"),
    "win32": ("munin.detect.windows", "WasapiDetector"),
}


def get_detector(platform: str | None = None) -> type[Detector]:
    """Return the :class:`Detector` subclass for ``platform`` (default: this one)."""
    key = platform or sys.platform
    try:
        module_name, class_name = _MODULES[key]
    except KeyError:
        raise NotImplementedError(f"no detector for platform {key!r}") from None
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        raise NotImplementedError(
            f"detector {module_name} is not implemented yet"
        ) from None
    return getattr(module, class_name)
