"""Idle inhibition on Omarchy.

Omarchy's idle service gates on a state file, and ``omarchy-toggle-idle``
flips it. Reading the file rather than asking the command is deliberate: the
command is a toggle with no "report" mode, so the only way to learn the current
state without changing it is to look at what it writes.

The script name is ``omarchy-``-prefixed because it is Omarchy's, not ours --
D12's "always ``munin-``" rule governs scripts Munin ships, not ones it calls.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping
from pathlib import Path

from munin.desktop.base import IdleInhibitor

__all__ = [
    "OmarchyIdleInhibitor",
    "STAY_AWAKE_STATE",
    "TOGGLE_IDLE",
    "prepare_session_environment",
]

log = logging.getLogger("munin.desktop")

#: Relative to ``$XDG_STATE_HOME``; Omarchy's idle service gates on it.
STAY_AWAKE_STATE = "omarchy/indicators/stay-awake"

TOGGLE_IDLE = "omarchy-toggle-idle"


#: Set by :func:`prepare_session_environment` so ``munin doctor`` can say which
#: variables were derived rather than inherited.
DERIVED_MARKER = "MUNIN_ENV_DERIVED"


def prepare_session_environment(
    environ: "MutableMapping[str, str] | None" = None,
    *,
    run_root: str | os.PathLike[str] = "/run/user",
    uid: int | None = None,
) -> list[str]:
    """Fill in the graphical-session variables a terminal may not have inherited.

    Measured on this machine: a terminal opened from the shell had neither
    ``XDG_RUNTIME_DIR`` nor ``HYPRLAND_INSTANCE_SIGNATURE``, so every PipeWire
    tool "found nothing" and ``hyprctl`` refused to run. The systemd unit sets
    its own ``XDG_RUNTIME_DIR``; this is for the CLI, ``doctor`` and ``setup``
    run by hand. Both derivations are the standard ones (``/run/user/<uid>``,
    and the single instance directory under ``$XDG_RUNTIME_DIR/hypr``); when the
    answer is ambiguous nothing is set. Returns the names that were derived and
    records them in :data:`DERIVED_MARKER`.
    """
    env = os.environ if environ is None else environ
    derived: list[str] = []
    if not env.get("XDG_RUNTIME_DIR"):
        candidate = Path(run_root) / str(os.getuid() if uid is None else uid)
        if candidate.is_dir():
            env["XDG_RUNTIME_DIR"] = str(candidate)
            derived.append("XDG_RUNTIME_DIR")
    runtime = env.get("XDG_RUNTIME_DIR")
    if runtime and not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        try:
            instances = sorted(p.name for p in (Path(runtime) / "hypr").iterdir() if p.is_dir())
        except OSError:
            instances = []
        if len(instances) == 1:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = instances[0]
            derived.append("HYPRLAND_INSTANCE_SIGNATURE")
    if derived:
        env[DERIVED_MARKER] = ",".join(derived)
    return derived


class OmarchyIdleInhibitor(IdleInhibitor):
    """Holds ``omarchy-toggle-idle stay-awake`` for a recording's duration."""

    method = "omarchy-stay-awake"

    def state_file(self) -> Path:
        state_home = os.environ.get("XDG_STATE_HOME") or str(
            Path.home() / ".local" / "state"
        )
        return Path(state_home) / STAY_AWAKE_STATE

    def is_inhibited(self) -> bool:
        return self.state_file().exists()

    def inhibit(self) -> None:
        self._toggle("stay-awake")

    def release(self) -> None:
        self._toggle("allow-idle")

    def _toggle(self, mode: str) -> None:
        try:
            code = self.runner([TOGGLE_IDLE, mode])
        except Exception as exc:  # noqa: BLE001 - never take a recording down
            log.warning("idle toggle failed mode=%s error=%s", mode, exc)
            return
        if code != 0:
            log.warning("idle toggle exited %d mode=%s", code, mode)

    def describe(self) -> dict[str, str]:
        return {
            "method": self.method,
            "command": TOGGLE_IDLE,
            "state_file": str(self.state_file()),
            "held": "yes" if self.is_inhibited() else "no",
        }
