#!/usr/bin/env bash
#
# Munin installer.
#
# Why this script exists at all: installing should not require reading the spec
# (spec 15). It checks the tools, puts the CLI, daemon and worker on PATH,
# installs and validates the shell plugin, binds the key, copies the systemd
# unit and creates the data root -- seven steps, each reversible.
#
# Two things it must never do quietly. It never runs sudo: non-interactive sudo
# does not work on this machine, so a missing package is printed as an
# `omarchy pkg add` line for a human to run. And it never edits the keybinding
# file silently: ~/.config/hypr/bindings.lua is a symlink into a dotfiles repo,
# so the installer backs up the real target, writes through the link, and says
# on stdout which tracked file in which repo it just changed.
#
# Enablement of the systemd unit is printed, never run (the dotfiles convention).
# `--uninstall` removes the binaries, plugin, bar entry, keybind and unit, and
# leaves ~/munin/ where it is. Nothing recorded is ever removed by an
# uninstaller.
#
# Owner: install workstream. Contract: docs/superpowers/specs/2026-09-14-poc-contracts.md, section 13.

set -euo pipefail

echo "install.sh: not implemented" >&2
exit 1
