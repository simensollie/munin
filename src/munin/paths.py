"""Where everything lives (D18).

One named root, ``~/munin``, resolved once: ``$MUNIN_HOME`` first (tests and the
installer set it), then ``[munin] home`` in the config, then the default. The
runtime directory under ``$XDG_RUNTIME_DIR/munin`` holds the socket and the
plugin-facing state file and is recreated on every boot.

Owner: spool workstream.
"""

from __future__ import annotations

import os
import unicodedata
from datetime import datetime
from pathlib import Path

__all__ = [
    "SOCKET_NAME",
    "STATE_NAME",
    "SLUG_MAX_LENGTH",
    "FALLBACK_SLUG",
    "munin_home",
    "recordings_root",
    "inbox_dir",
    "voices_dir",
    "latest_link",
    "log_file",
    "config_file",
    "runtime_dir",
    "socket_path",
    "state_path",
    "session_dir",
    "slugify",
    "session_id",
]

SOCKET_NAME = "rec.sock"
STATE_NAME = "state.json"

#: A directory name has to stay readable in ``ls`` and in the bar's tooltip.
SLUG_MAX_LENGTH = 48

#: What an untitled, unidentified recording is called.
FALLBACK_SLUG = "adhoc"

#: Folded before NFKD, because NFKD turns "a-ring" into "a" and loses the vowel.
#: Norwegian first, then the neighbouring letters that turn up in the same
#: sentences.
_FOLD = {
    "æ": "ae",  # ae ligature
    "ø": "oe",  # o with stroke
    "å": "aa",  # a with ring
    "Æ": "AE",
    "Ø": "OE",
    "Å": "AA",
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "Ä": "AE",
    "Ö": "OE",
    "Ü": "UE",
    "ß": "ss",
    "đ": "d",
    "Đ": "D",
}
_FOLD_TABLE = str.maketrans(_FOLD)


def munin_home(configured: str | None = None) -> Path:
    """The data root: ``$MUNIN_HOME``, else ``configured``, else ``~/munin``."""
    env = os.environ.get("MUNIN_HOME")
    if env:
        return Path(env).expanduser()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "munin"


def recordings_root(home: Path, name: str = "recordings") -> Path:
    """``<home>/recordings``."""
    return home / name


def inbox_dir(home: Path, name: str = "inbox") -> Path:
    """``<home>/inbox`` -- one relative symlink per unfinished session."""
    return home / name


def voices_dir(home: Path, name: str = "voices") -> Path:
    """``<home>/voices`` -- created but unused in the PoC."""
    return home / name


def latest_link(home: Path) -> Path:
    """``<home>/latest`` -- relative symlink to the newest session."""
    return home / "latest"


def log_file(home: Path, name: str = "munin.log") -> Path:
    """``<home>/munin.log``."""
    return home / name


def config_file(home: Path) -> Path:
    """``<home>/config.toml``."""
    return home / "config.toml"


def runtime_dir() -> Path:
    """``$XDG_RUNTIME_DIR/munin``, mode 0700. Raises if the variable is unset.

    Every PipeWire tool needs ``XDG_RUNTIME_DIR`` as well, so an unset variable
    is a hard error with a named cause rather than a silent fallback to
    ``/tmp``, which would survive a reboot and confuse the plugin.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise RuntimeError(
            "XDG_RUNTIME_DIR is not set; munin-rec needs it for the socket, the "
            "state file and every PipeWire tool (the systemd unit sets "
            "Environment=XDG_RUNTIME_DIR=%t)"
        )
    path = Path(base) / "munin"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def socket_path() -> Path:
    """``$XDG_RUNTIME_DIR/munin/rec.sock``."""
    return runtime_dir() / SOCKET_NAME


def state_path() -> Path:
    """``$XDG_RUNTIME_DIR/munin/state.json`` -- the plugin's only input."""
    return runtime_dir() / STATE_NAME


def slugify(title: str) -> str:
    """Title to directory slug.

    Norwegian letters fold first (ae/oe/aa), then NFKD to ASCII, lowercase, runs
    of non ``[a-z0-9]`` to ``-``, stripped, truncated to 48 characters at a
    ``-`` boundary. An empty result becomes ``adhoc``.

    A single word longer than the limit has no boundary to cut at, so it is cut
    hard rather than thrown away.
    """
    folded = (title or "").translate(_FOLD_TABLE)
    ascii_only = (
        unicodedata.normalize("NFKD", folded)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    out: list[str] = []
    for char in ascii_only:
        if "a" <= char <= "z" or "0" <= char <= "9":
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")
    if len(slug) > SLUG_MAX_LENGTH:
        cut = slug[:SLUG_MAX_LENGTH]
        if slug[SLUG_MAX_LENGTH] != "-":
            head, sep, _tail = cut.rpartition("-")
            if sep:
                cut = head
        slug = cut.strip("-")
    return slug or FALLBACK_SLUG


def session_id(created: datetime, title: str) -> str:
    """``YYYY-MM-DDTHHMM-<slug>`` from a local-time datetime and a title."""
    return f"{created:%Y-%m-%dT%H%M}-{slugify(title)}"


def session_dir(
    home: Path, created: datetime, sid: str, recordings: str = "recordings"
) -> Path:
    """``<home>/recordings/YYYY/MM/<session id>``, from the session's created date.

    ``YYYY/MM`` come from the session's *created* local date and are never
    recomputed, so a meeting that runs past midnight stays where it started.
    """
    return recordings_root(home, recordings) / f"{created:%Y}" / f"{created:%m}" / sid
