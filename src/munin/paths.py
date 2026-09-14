"""Where everything lives (D18).

One named root, ``~/munin``, resolved once: ``$MUNIN_HOME`` first (tests and the
installer set it), then ``[munin] home`` in the config, then the default. The
runtime directory under ``$XDG_RUNTIME_DIR/munin`` holds the socket and the
plugin-facing state file and is recreated on every boot.

Owner: spool workstream.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "SOCKET_NAME",
    "STATE_NAME",
    "munin_home",
    "recordings_root",
    "inbox_dir",
    "voices_dir",
    "latest_link",
    "log_file",
    "runtime_dir",
    "socket_path",
    "state_path",
    "session_dir",
    "slugify",
    "session_id",
]

SOCKET_NAME = "rec.sock"
STATE_NAME = "state.json"


def munin_home(configured: str | None = None) -> Path:
    """The data root: ``$MUNIN_HOME``, else ``configured``, else ``~/munin``."""
    raise NotImplementedError


def recordings_root(home: Path) -> Path:
    """``<home>/recordings``."""
    raise NotImplementedError


def inbox_dir(home: Path) -> Path:
    """``<home>/inbox`` -- one relative symlink per unfinished session."""
    raise NotImplementedError


def voices_dir(home: Path) -> Path:
    """``<home>/voices`` -- created but unused in the PoC."""
    raise NotImplementedError


def latest_link(home: Path) -> Path:
    """``<home>/latest`` -- relative symlink to the newest session."""
    raise NotImplementedError


def log_file(home: Path) -> Path:
    """``<home>/munin.log``."""
    raise NotImplementedError


def runtime_dir() -> Path:
    """``$XDG_RUNTIME_DIR/munin``, mode 0700. Raises if the variable is unset."""
    raise NotImplementedError


def socket_path() -> Path:
    """``$XDG_RUNTIME_DIR/munin/rec.sock``."""
    raise NotImplementedError


def state_path() -> Path:
    """``$XDG_RUNTIME_DIR/munin/state.json`` -- the plugin's only input."""
    raise NotImplementedError


def slugify(title: str) -> str:
    """Title to directory slug.

    Norwegian letters fold first (ae/oe/aa), then NFKD to ASCII, lowercase, runs
    of non ``[a-z0-9]`` to ``-``, stripped, truncated to 48 characters at a
    ``-`` boundary. An empty result becomes ``adhoc``.
    """
    raise NotImplementedError


def session_id(created, title: str) -> str:
    """``YYYY-MM-DDTHHMM-<slug>`` from a local-time datetime and a title."""
    raise NotImplementedError


def session_dir(home: Path, created, sid: str) -> Path:
    """``<home>/recordings/YYYY/MM/<session id>``, from the session's created date."""
    raise NotImplementedError
