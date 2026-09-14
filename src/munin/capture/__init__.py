"""Platform factory for the capture layer.

The only module in the package that consults ``sys.platform``. Platform modules
are imported lazily so that importing ``munin.capture`` on a machine missing a
platform's tooling still works (spec 16.5).

FROZEN: see docs/superpowers/specs/2026-09-14-poc-contracts.md, section 5.
"""

from __future__ import annotations

import importlib
import sys

from munin.capture.base import (
    CaptureError,
    CaptureResult,
    CaptureTarget,
    Capturer,
    SessionRef,
    segment_filenames,
)

__all__ = [
    "CaptureError",
    "CaptureResult",
    "CaptureTarget",
    "Capturer",
    "SessionRef",
    "get_capturer",
    "segment_filenames",
]

_MODULES: dict[str, tuple[str, str]] = {
    "linux": ("munin.capture.linux", "PipewireCapturer"),
    "darwin": ("munin.capture.macos", "ScreenCaptureKitCapturer"),
    "win32": ("munin.capture.windows", "WasapiCapturer"),
}


def get_capturer(platform: str | None = None) -> type[Capturer]:
    """Return the :class:`Capturer` subclass for ``platform`` (default: this one).

    Raises ``NotImplementedError`` for a platform Munin does not capture on yet.
    """
    key = platform or sys.platform
    try:
        module_name, class_name = _MODULES[key]
    except KeyError:
        raise NotImplementedError(f"no capture backend for platform {key!r}") from None
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        raise NotImplementedError(
            f"capture backend {module_name} is not implemented yet"
        ) from None
    return getattr(module, class_name)
