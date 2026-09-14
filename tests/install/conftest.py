"""A synthetic Omarchy machine, built out of shell scripts that log their argv.

``install.sh`` is mostly a sequence of calls to other people's tools, so the
only honest way to test it is to replace those tools with stand-ins that record
what they were asked to do and then assert on the record. Everything here is
confined to ``tmp_path``: no test touches a real ``~/.config``, ``~/.local`` or
``~/munin``.

Owner: install workstream.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"

# --- the fake tools --------------------------------------------------------

OMARCHY = """#!/usr/bin/env bash
set -euo pipefail
printf 'omarchy %s\\n' "$*" >>"$SHIM_LOG"
if [[ ${1:-} == plugin && ${2:-} == validate ]]; then
  [[ -f ${3:-}/manifest.json ]] || exit 1
  exit 0
fi
if [[ ${1:-} == bar && ${2:-} == put ]]; then
  [[ -f ${SHIM_SHELL_JSON:-} ]] || exit 0
  tmp="$(mktemp)"
  jq --arg id "${3:-}" '.bar.layout.right = ([{id: $id}] + .bar.layout.right)' \\
    "$SHIM_SHELL_JSON" >"$tmp"
  mv "$tmp" "$SHIM_SHELL_JSON"
  exit 0
fi
exit 0
"""

OMARCHY_SHELL = """#!/usr/bin/env bash
printf 'omarchy-shell %s\\n' "$*" >>"$SHIM_LOG"
exit 0
"""

HYPRCTL = """#!/usr/bin/env bash
printf 'hyprctl %s\\n' "$*" >>"$SHIM_LOG"
[[ ${1:-} == configerrors ]] && echo "no errors"
exit 0
"""

SYSTEMCTL = """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >>"$SHIM_LOG"
case "${2:-}" in
  is-enabled) echo "${SHIM_UNIT_ENABLED:-disabled}" ;;
  is-active) echo "${SHIM_UNIT_ACTIVE:-inactive}" ;;
esac
exit 0
"""

PW_DUMP = """#!/usr/bin/env bash
printf 'pw-dump %s\\n' "$*" >>"$SHIM_LOG"
cat "${SHIM_PW_DUMP:-/dev/null}" 2>/dev/null || echo '[]'
exit 0
"""

TRIVIAL = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >>"$SHIM_LOG"
exit 0
"""

# A python3 that fakes `-m venv` and delegates everything else to the real one,
# so the version check in step 1 stays honest while step 2 stays fast.
PYTHON3 = """#!/usr/bin/env bash
printf 'python3 %s\\n' "$*" >>"$SHIM_LOG"
if [[ ${1:-} == -m && ${2:-} == venv ]]; then
  d="${3:?venv directory}"
  mkdir -p "$d/bin"
  cat >"$d/bin/pip" <<'PIP'
#!/usr/bin/env bash
printf 'pip %s\\n' "$*" >>"$SHIM_LOG"
exit 0
PIP
  cat >"$d/bin/munin" <<'MUNIN'
#!/usr/bin/env bash
printf 'munin %s\\n' "$*" >>"$SHIM_LOG"
if [[ ${1:-} == setup ]]; then
  mkdir -p "$MUNIN_HOME"
  printf '# synthetic default config\\n[capture]\\nmic_source = "default"\\n' \\
    >"$MUNIN_HOME/config.toml"
fi
exit 0
MUNIN
  chmod +x "$d/bin/pip" "$d/bin/munin"
  cp "$d/bin/munin" "$d/bin/munin-rec"
  cp "$d/bin/munin" "$d/bin/munin-work"
  : >"$d/bin/python"
  chmod +x "$d/bin/python"
  exit 0
fi
exec @REAL_PYTHON@ "$@"
"""

#: A microphone and one playing application, the shape ``pw-dump`` really emits
#: (identity on the Client object, not on the stream node).
PW_DUMP_FIXTURE = [
    {
        # Metadata objects carry their props at the top level, with no "info"
        # -- the shape pw-dump really emits on this machine.
        "id": 20,
        "type": "PipeWire:Interface:Metadata",
        "props": {"metadata.name": "default", "object.serial": 40},
        "metadata": [
            {"key": "default.audio.source", "value": {"name": "alsa_input.synthetic-mic"}}
        ],
    },
    {
        "id": 56,
        "type": "PipeWire:Interface:Node",
        "info": {
            "props": {
                "media.class": "Audio/Source",
                "node.name": "alsa_input.synthetic-mic",
                "node.description": "Synthetic Mono Microphone",
                "object.serial": 56,
            },
            "params": {"EnumFormat": [{"format": "S16LE", "rate": 16000, "channels": 1}]},
        },
    },
    {
        "id": 64,
        "type": "PipeWire:Interface:Client",
        "info": {
            "props": {
                "application.name": "Beacon 365",
                "application.process.binary": "beacon365",
                "application.process.id": 4242,
            }
        },
    },
    {
        "id": 71,
        "type": "PipeWire:Interface:Node",
        "info": {
            "props": {
                "media.class": "Stream/Output/Audio",
                "node.name": "Beacon 365",
                "object.serial": 71,
                "client.id": 64,
            }
        },
    },
]

SHELL_JSON_FIXTURE = {
    "version": 1,
    "bar": {
        "position": "top",
        "layout": {
            "left": [{"id": "omarchy.menu"}],
            "center": [{"id": "omarchy.clock"}],
            "right": [{"id": "omarchy.tray"}, {"id": "omarchy.power"}],
        },
    },
    "plugins": [],
}

BINDINGS_FIXTURE = """\
-- Personal keybinding overrides.

o.bind("SUPER + SHIFT + I", "Editor", { launch = "editor" })
"""


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@dataclass
class FakeMachine:
    """A home directory, a PATH of stand-ins, and the log they all write to."""

    home: Path
    shim_bin: Path
    log: Path
    env: dict[str, str]
    dotfiles: Path
    shell_json: Path
    bindings_link: Path
    bindings_target: Path

    def run(self, *args: str, expect_rc: int = 0) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["bash", str(INSTALL_SH), "--prefix-home", str(self.home), *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert proc.returncode == expect_rc, (
            f"install.sh {' '.join(args)} exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
        return proc

    @property
    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def called(self, fragment: str) -> bool:
        return any(fragment in line for line in self.calls)

    def clear_log(self) -> None:
        self.log.write_text("", encoding="utf-8")

    # Convenience paths, so a test never re-spells the layout.
    @property
    def bin_dir(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def venv(self) -> Path:
        return self.home / ".local" / "share" / "munin" / "venv"

    @property
    def plugin_dir(self) -> Path:
        return self.home / ".config" / "omarchy" / "plugins" / "local.munin"

    @property
    def unit(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / "munin.service"

    @property
    def data_home(self) -> Path:
        return Path(self.env["MUNIN_HOME"])


@pytest.fixture()
def machine(tmp_path: Path) -> FakeMachine:
    home = tmp_path / "home"
    shim = tmp_path / "shim"
    dotfiles = tmp_path / "dotfiles" / "omarchy" / "hypr"
    for directory in (home, shim, dotfiles):
        directory.mkdir(parents=True)

    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")

    real_python = shutil.which("python3") or "/usr/bin/python3"
    _write_exe(shim / "omarchy", OMARCHY)
    _write_exe(shim / "omarchy-shell", OMARCHY_SHELL)
    _write_exe(shim / "hyprctl", HYPRCTL)
    _write_exe(shim / "systemctl", SYSTEMCTL)
    _write_exe(shim / "pw-dump", PW_DUMP)
    _write_exe(shim / "pw-record", TRIVIAL)
    _write_exe(shim / "ffmpeg", TRIVIAL)
    _write_exe(shim / "python3", PYTHON3.replace("@REAL_PYTHON@", real_python))

    # The keybinding file: a symlink into a "dotfiles repo", as on the real
    # machine, so the backup and the loud warning are exercised.
    bindings_target = dotfiles / "bindings.lua"
    bindings_target.write_text(BINDINGS_FIXTURE, encoding="utf-8")
    bindings_link = home / ".config" / "hypr" / "bindings.lua"
    bindings_link.parent.mkdir(parents=True)
    bindings_link.symlink_to(bindings_target)

    shell_json = home / ".config" / "omarchy" / "shell.json"
    shell_json.parent.mkdir(parents=True, exist_ok=True)
    shell_json.write_text(json.dumps(SHELL_JSON_FIXTURE, indent=2), encoding="utf-8")

    pw_dump_json = tmp_path / "pw-dump.json"
    pw_dump_json.write_text(json.dumps(PW_DUMP_FIXTURE), encoding="utf-8")

    runtime = tmp_path / "run"
    runtime.mkdir()

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "MUNIN_HOME": str(home / "munin"),
            "XDG_RUNTIME_DIR": str(runtime),
            "SHIM_LOG": str(log),
            "SHIM_SHELL_JSON": str(shell_json),
            "SHIM_PW_DUMP": str(pw_dump_json),
            # No key of ours is bound by Omarchy's defaults on this machine;
            # point the check at an empty directory so the test does not depend
            # on what is installed under /usr/share.
            "OMARCHY_DEFAULT_BINDINGS": str(tmp_path / "no-omarchy-defaults"),
        }
    )

    return FakeMachine(
        home=home,
        shim_bin=shim,
        log=log,
        env=env,
        dotfiles=dotfiles,
        shell_json=shell_json,
        bindings_link=bindings_link,
        bindings_target=bindings_target,
    )
