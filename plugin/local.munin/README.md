# `local.munin` — the Omarchy shell plugin

The bar half of Munin. It detects, prompts and renders; `munin-rec` captures
(D20). A shell hot-reload, crash or theme change can therefore never kill a
recording.

## What is in here

| File | What it is |
|---|---|
| `manifest.json` | `schemaVersion` 1, `kinds: ["service", "bar-widget"]`, `keepLoaded: true` |
| `Service.qml` | Reads the daemon's `state.json`; watches PipeWire for a live call |
| `BarWidget.qml` | The bar button and its dropdown panel, on `qs.Ui`'s `Panel` |
| `Model.js` | Pure state, formatting and detection helpers, tested with `node` |
| `components/TrackMeter.qml` | One level row in the panel |
| `components/SessionRow.qml` | One session row in the panel |

`Panel.qml` is not a separate file and the manifest declares no `panel` kind:
the bar widget *is* a `qs.Ui` `Panel`, which is how `omarchy.dropbox` ships its
own popup. `omarchy.clock` and `omarchy.weather` split it into two files; one
file is the simpler of the two idioms and keeps the manifest to the two kinds
the contract names.

## What it needs from the daemon

Exactly one input and one output. No socket, ever (contracts §7.2) — one client
of the wire protocol, so a shell reload can never hold the daemon's socket
open.

**Input:** `$XDG_RUNTIME_DIR/munin/state.json`, the state view of contracts
§7.2. Read with `FileView { watchChanges: true }` plus a 2 s reload timer,
because a watch cannot attach to a file that does not exist yet and the daemon
creates it on start. The plugin computes elapsed time from `started_at` itself,
so **no timer ever writes that file**.

If the file is missing, truncated or half-written, `Model.parseState` returns a
safe idle view. A parse error in a bar widget takes down the whole bar, so it
never throws.

**Output:** the CLI, run as an argv vector through `bash -lc 'exec "$@"'` —
which gives the login `PATH` that has `~/.local/bin` on it, and guarantees a
window title is never re-tokenized by a shell:

| When | Command |
|---|---|
| A process starts holding a playback and a capture stream | `munin event call-started [--pid N] [--app ID] [--title T]` |
| That process stops holding both | `munin event call-ended [--pid N]` |
| Panel primary button, bar middle-click | `munin start [--from-detection\|--resume]` / `munin stop` |
| Bar right-click | `munin toggle` |
| Panel opens | `munin list --json --limit 5` |
| Panel folder rows | `xdg-open` on `$MUNIN_HOME` or a session directory |

Detection events are only sent while a daemon is actually running (`state.json`
present and `daemon_pid` set), so a stopped daemon never costs a process spawn
per PipeWire event. The plugin does not read `config.toml`: when
`detection.source` is not `"plugin"` the **daemon** ignores these events, which
is what the `accepted` field of the `event` reply is for.

## The seven bar states (spec §9.2)

| State | Bar shows |
|---|---|
| `idle` | nothing — the widget hides itself |
| `detected` | dim microphone glyph and the app label |
| `recording` | pulsing red dot and `HH:MM:SS` |
| `ending` | the same red dot held steady, still counting |
| `captured` / `transcribing` | a turning glyph and the queue depth |
| `done` | a tick and "Transcript ready", for 30 s |
| `failed` | an exclamation and "Retry", until it is acknowledged |

Which one is rendered is a pure function of `state.json` — see
`Model.effectiveState` and `Model.barGlyph` / `barTone` / `barLabel`. No colour
is named in `Model.js`: a state maps to a *tone* (`urgent`, `foreground`,
`dim`) and `BarWidget.qml` resolves that against the theme, so a theme change
needs no code.

`󰻂` (U+F0EC2), the glyph `bar/indicators/ScreenRecording.qml` uses, is Munin's
identity mark in the panel hero and on the primary button. The bar mark while
recording is a red dot instead, because a dot is read at a glance and a glyph
is not — that is what sketch 01 draws.

## Detection

The two conditions of spec §6.3, kept independent so another app is a config
row rather than code (D13):

1. **A call is live.** One owner holds a PipeWire playback stream *and* a
   capture stream. Playback is `isStream && isSink`; capture is
   `isStream && !isSink` — the flags `panels/audio` relies on, because
   Quickshell versions differ in how `PwNode.type` is exposed.
2. **It is Teams.** `Model.identify` runs the three-shape table in match order:
   the PipeWire client name, then the Hyprland window class, then the window
   title (case-insensitive). Same table and same order as
   `detect/base.identify`, so the plugin and the daemon-side detector agree.

Owners are grouped by `application.process.id` from the node's properties, and
by `client.id` when PipeWire published no pid on the node (some clients publish
process identity on the Client object only, which Quickshell does not expose).
An owner with no pid still satisfies condition 1; the pid is simply left off
the event rather than guessed.

The window owning a pid is found in `Hyprland.toplevels`. Chrome's audio
process is not the process that owns the window, so an unmatched pid is walked
up `/proc/<pid>/status` `PPid` for at most five hops, asynchronously, and gives
up quietly rather than failing.

`PwNode.properties` is only read on a node that reports `ready`, and every
stream node is held in a `PwObjectTracker` — `panels/audio/Panel.qml` warns
that reading properties off an unbound node while capture streams appear can
destabilize Quickshell's PipeWire service. Scans are debounced by 1.2 s, so a
call that adds six nodes as it starts produces one event, not six.

## Install

The installer does this (contracts §13); none of it belongs in this directory.

```bash
cp -r plugin/local.munin ~/.config/omarchy/plugins/local.munin
omarchy plugin validate ~/.config/omarchy/plugins/local.munin
omarchy-shell shell rescanPlugins          # make the live shell see it
omarchy plugin enable local.munin          # enabling is a live IPC call
omarchy bar put local.munin --before omarchy.tray
```

Copy, never symlink: `omarchy plugin validate` rejects a plugin folder that
contains any symlink. Enabling a third-party plugin is what makes its *service*
load too — the registry treats a plugin with an entry in `shell.json` as
enabled, and both kinds come from this one manifest.

## Developing

```bash
node tests/plugin/test_model.mjs          # the pure half, no shell needed
omarchy plugin validate plugin/local.munin
```

QML lint, with the shell's own modules on the import path:

```bash
mkdir -p /tmp/qs-imports/qs
ln -sfn /usr/share/omarchy/shell/Ui      /tmp/qs-imports/qs/Ui
ln -sfn /usr/share/omarchy/shell/Commons /tmp/qs-imports/qs/Commons
/usr/lib/qt6/bin/qmllint -I /usr/lib/qt6/qml -I /tmp/qs-imports plugin/local.munin/*.qml
```

The remaining warnings are `Style.font.<token>` and `bar.<token>` member
lookups that `qmllint` cannot resolve through a `QtObject`, plus unqualified
`root.` access inside `Component` blocks. The first-party panels produce the
same two categories in quantity (`panels/dropbox/Panel.qml`: 71 of them), so
this is house-consistent rather than clean.

After editing, the live shell picks the plugin up with
`omarchy-shell shell rescanPlugins`; QML errors land in
`journalctl --user -t omarchy-shell -f`.

## What is deferred

Everything sketch 04 draws that the PoC does not build: the calendar subtitle
and participant count (M9), *Add marker*, *Discard*, backend status, the mixed
track, and *Settings*. The panel's *Live input* block shows PipeWire's own
levels and says so — the plugin has no access to the capture at all, and a
meter that implied otherwise would be a lie about a recording.
