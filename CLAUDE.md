# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this repo is

Munin: a cross-platform digital meeting recorder and transcription pipeline
(two-track capture → self-hosted Whisper + pyannote → transcript in the session
directory under `~/munin/recordings/`).

**Current state: the PoC is built and installed.** `main` is the only branch.
`src/munin/` holds the CLI, capture daemon and worker; `plugin/local.munin/` is
the Omarchy shell plugin; `install.sh` installs the lot; `tests/` is a real
suite. The PoC ships the `none` backend, so sessions capture and then stay
`pending` by design. No model is downloaded and none is run.

```bash
./install.sh --dry-run          # read the seven steps before running them
./install.sh                    # install; never runs sudo
~/.local/share/munin/venv/bin/python -m pytest
munin doctor                    # 22 checks
```

`tools/render-sketches.py` regenerates `docs/design/sketches/*.jpg` from
`docs/design/munin-plugin-sketches.html`. Run it from the repo root after
editing that HTML; it needs `chromium` and ImageMagick.

## Source of truth

`docs/superpowers/specs/2026-09-14-meeting-recorder-design.md` is the
authoritative design. Read the relevant section before answering anything about
how Munin should behave. It covers: problem, goals, non-goals, settled
decisions (D1–D24), architecture, capture and Teams detection, pipeline
(including the voice register, §7.4.1) and M365 enrichment, transcription
backends, Omarchy integration and the admin surface, Plaud export, failure
handling, compliance, testing, open questions, install and setup, and four
appendices of investigation notes.

Two companions:

- `docs/superpowers/plans/2026-09-14-implementation-plan.md` — stack, layout,
  milestones M0–M13, backend order, what to do first.
- `docs/design/munin-plugin-sketches.html` — UI sketches, with per-mockup JPGs
  in `docs/design/sketches/` and an index in `docs/design/README.md`.

Two categories must not be confused:

- **D1–D24 are settled.** Revisit only if the user explicitly reopens one.
- **§14's open questions and Appendix D's "Not verified" list are open.** They
  need checking on real hardware. Never present them as decided, and never
  quietly resolve one by assumption — say what would have to be verified.

Appendix D is now mostly *verified on this machine*, not inferred from the
dotfiles repo. Three findings shape everything: Omarchy 4 has a real plugin
system (`manifest.json` + QML in `~/.config/omarchy/plugins/`), notification
actions are supported, and `Quickshell.Services.Pipewire` is reachable from
plugin QML. When working on the plugin, read the first-party plugins under
`/usr/share/omarchy/shell/plugins/` for the house idiom — never edit them.

New specs go in `docs/superpowers/specs/` named `YYYY-MM-DD-<topic>.md`.

## Public-copy rule (important)

This repo is a **public, sanitised copy of an internal design document**.
Customer names, colleague names and internal project references are replaced
with generic descriptors ("Product name: letters + `365`", "Customer name:
five-letter acronym"). Counts and measurements are real and unchanged.

When editing anything here: keep it sanitised. Never write a real customer
name, colleague name, internal project name or customer content into a file in
this repo, even if it comes up in conversation or from a connected tool
(Linear, M365, the wiki). Use generic descriptors and synthetic examples. The
transcript samples in §7.5 use placeholder names (`Ola Nordmann`,
`Kari Nordmann`) — follow that pattern.

## Conventions that carry into implementation

- **Three separable processes:** `munin-rec` (capture daemon, never
  transcribes), `munin-work` (worker, drains spools, runs the pipeline),
  `munin` (CLI).
- **Script names are always `munin-`-prefixed, never `omarchy-`** (D12). The
  graphical-session PATH puts `/usr/share/omarchy/bin` before `~/.local/bin`,
  so an `omarchy-*` override from a user directory silently loses.
- **Suggest, never auto-record** (D4). The daemon notifies; the user confirms.
  Calendar data enriches a session and never starts one.
- **All data lives under `~/munin/`** (D18) — `recordings/YYYY/MM/<session>/`,
  plus `voices/`, `inbox/`, `config.toml`. **The session directory is the only
  place a transcript is written** (D24). There is no copy to the second brain or
  anywhere else; do not reintroduce one.
- **Output format:** `[HH:MM:SS - HH:MM:SS] <display name>: <text>`, strictly
  monotonic segments, with a `.json` sidecar for word-level data. The `.txt` is
  the source of record.
- **The shell plugin never captures** (D20). It detects, prompts and renders;
  `munin-rec` captures. A shell hot-reload must never kill a recording.
- **Naming a speaker and enrolling a voice are separate actions** (D17). A voice
  profile is a name and an embedding, stored locally under `~/munin/voices/`.
- **The glossary** is `~/munin/glossary.toml`, schema in spec §7.3.1 with a
  synthetic template at `examples/glossary.toml`. A real glossary is a list of
  real customer, product and colleague names, so it never enters this repo —
  keep `examples/glossary.toml` synthetic when editing it.
- **Shell scripts:** bash, `set -euo pipefail`, a comment block explaining
  *why* before any code, state under `${XDG_CACHE_HOME:-$HOME/.cache}/munin`.
- **Omarchy integration:** systemd `--user` units copied into place and enabled
  by hand (never commit `*.target.wants/` symlinks); notifications via
  `omarchy-notification-send`; the bar is Quickshell, not Waybar. Appendix D
  lists what was verified and what was not.

## Compliance

This system records identifiable people, including customers, in a regulated
context (ISO 9001 / 27001 / 42001, GDPR). §12 of the spec is not boilerplate.
Flag explicitly when a change touches retention, access control, or audit
trail. Everything stays on the user's own machine, which is a deliberate part
of the design. Examples always use synthetic data.

The voice register was ruled out in the first draft and has since been decided
in: it holds a name and an embedding, locally, with no consent workflow. That
decision has been made — do not relitigate it. Retention defaults remain open.

## House style

English throughout. Metric units, 24-hour time. ISO 8601 dates in technical
contexts; locale-formatted dates and numbers in anything user-facing. Markdown
(`.md`) is the default output format. Be concise; state assumptions explicitly
when requirements are ambiguous, and include at least one counterargument or
risk when proposing a tradeoff.
