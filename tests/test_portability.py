"""The portability boundary, enforced mechanically rather than by review.

Spec 16.5: everything platform-specific lives behind a factory. Only these
modules may name a platform's tools:

* ``capture/`` and ``detect/`` -- the two platform packages proper
* ``desktop/`` -- idle inhibition, same idiom
* ``doctor.py`` and ``setup.py`` -- they exist to inspect *this* machine
* ``notify.py`` -- the desktop notification shim

Everywhere else, a reference to ``pw-record``, ``hyprctl``, PipeWire or Omarchy
means a port to macOS or Windows has to edit a file that should not have known
about Linux in the first place. Ports are the reason this rule exists (the spec
designs for macOS and Windows), and a rule nothing checks is a rule that rots.

Comments and docstrings are allowed to *explain* why a platform behaves as it
does, and so is the user-facing config template; everything else is policed,
including ordinary string literals, because that is the shape a stray
``["omarchy-toggle-idle", mode]`` actually takes.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "munin"

#: Modules allowed to name platform tooling.
EXEMPT_PREFIXES = ("capture/", "detect/", "desktop/")
EXEMPT_FILES = ("doctor.py", "setup.py", "notify.py")

#: Lowercased substrings that mean "this code knows which platform it is on".
FORBIDDEN = ("pw-record", "pw-dump", "pw-cli", "hyprctl", "pipewire", "omarchy")

#: The one accepted exception, and why.
#:
#: ``[idle] method`` is a *mechanism name* that contracts section 9 fixes as a
#: default config value. It is inert data -- the module that acts on it is
#: ``munin.desktop.linux``, which owns the command itself. Changing it would
#: break the frozen config schema for no portability gain, since a macOS build
#: reads the same key and resolves it through the same factory.
ALLOWED_LITERALS = {"omarchy-stay-awake"}

#: Module constants whose string value is user-facing *text*, not code.
#:
#: ``DEFAULT_CONFIG_TOML`` is the template written to ``~/munin/config.toml``.
#: It is a document the user reads and edits, and contracts section 9 fixes its
#: contents, comments included. A config file that explains what
#: ``mic_source = "default"`` follows on the machine in front of you is doing its
#: job. A port would ship its own template; it would not have to change any code.
ALLOWED_CONSTANTS = {"DEFAULT_CONFIG_TOML"}


def _code_only(path: Path) -> list[tuple[int, str]]:
    """Source lines with comments and docstrings blanked out.

    Prose explaining why a platform behaves as it does is not a violation; a
    call to that platform's binary is -- and that call is usually a plain string
    literal (``["omarchy-toggle-idle", mode]``), so ordinary string literals
    must stay policed. Only comments, docstrings and the explicitly allowed
    literals are removed. Tokenising is how the two are told apart.
    """
    source = path.read_text(encoding="utf-8")
    blanked = source.splitlines()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:  # pragma: no cover - a syntax error fails elsewhere
        return list(enumerate(blanked, start=1))
    drop: list[tuple[int, int, int, int]] = []
    # A string in statement position is a docstring; anything else is a value.
    statement_start = True
    assigning: str | None = None
    for token in tokens:
        if token.type == tokenize.COMMENT:
            drop.append((*token.start, *token.end))
            continue
        if token.type == tokenize.STRING:
            text = token.string.strip("\"'bBrRfFuU \n")
            if (
                statement_start
                or text in ALLOWED_LITERALS
                or assigning in ALLOWED_CONSTANTS
            ):
                drop.append((*token.start, *token.end))
        if token.type in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                          tokenize.DEDENT, tokenize.ENCODING):
            statement_start = True
            assigning = None
        elif token.type != tokenize.COMMENT:
            if statement_start and token.type == tokenize.NAME:
                assigning = token.string
            statement_start = False
    lines = list(blanked)
    for srow, scol, erow, ecol in drop:
        for row in range(srow, erow + 1):
            index = row - 1
            if index >= len(lines):
                continue
            line = lines[index]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line)
            lines[index] = line[:start] + " " * (end - start) + line[end:]
    return list(enumerate(lines, start=1))


def _policed_files() -> list[Path]:
    files = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith(EXEMPT_PREFIXES) or relative in EXEMPT_FILES:
            continue
        files.append(path)
    return files


def test_no_platform_tooling_above_the_boundary() -> None:
    offences: list[str] = []
    for path in _policed_files():
        relative = path.relative_to(SRC).as_posix()
        for number, line in _code_only(path):
            lowered = line.lower()
            for needle in FORBIDDEN:
                if needle in lowered:
                    offences.append(f"{relative}:{number}: {needle!r} in {line.strip()!r}")
    assert not offences, (
        "platform-specific tooling above the portability boundary (spec 16.5).\n"
        "Move it into capture/, detect/ or desktop/ behind the platform factory:\n  "
        + "\n  ".join(offences)
    )


def test_the_policed_set_is_not_accidentally_empty() -> None:
    """A guard on the guard: a bad exemption rule could exempt everything."""
    policed = {path.name for path in _policed_files()}
    assert {"daemon.py", "worker.py", "spool.py", "config.py", "paths.py"} <= policed


def test_only_the_factories_consult_sys_platform() -> None:
    """``sys.platform`` is a branch on the platform, wherever it appears.

    The three ``__init__`` factories are the only places allowed to ask. Session
    metadata records ``sys.platform`` as a *value* (contracts section 3), which
    is why ``spool.py`` is listed here rather than policed.
    """
    allowed = {"capture/__init__.py", "detect/__init__.py", "desktop/__init__.py",
               "spool.py", "doctor.py", "setup.py"}
    offences = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative in allowed:
            continue
        for number, line in _code_only(path):
            if "sys.platform" in line:
                offences.append(f"{relative}:{number}")
    assert not offences, f"sys.platform outside the platform factories: {offences}"
