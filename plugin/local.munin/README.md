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
| A process starts holding a playback **and** a capture stream, *and* the rule table identifies it | `munin event call-started [--pid N] [--app LABEL] [--app-id ID] [--handle SERIAL] [--title T]` |
| That process stops holding both | `munin event call-ended [--pid N]` |
| Panel primary button, bar middle-click | `munin start [--from-detection\|--resume]` / `munin stop` |
| Panel secondary button while recording | `munin split --now` — one recording holding two meetings is cut here (D26, spec §6.4) |
| Bar right-click | `munin toggle` |
| Panel opens | `munin list --json --limit 5` |
| Panel folder rows | `xdg-open` on `$MUNIN_HOME` or a session directory |

`--handle` is the `object.serial` of the owner's playback node, and it is what
binds the app track: without it the daemon has no app target and the capturer
writes a silent `app.opus` beside the microphone. Both conditions of spec §6.3
have to hold before an event is sent — an owner holding both stream kinds that
no rule identifies is a Discord call or a WebRTC page, not a meeting, and
prompting for it would ask to record people Munin was never asked to care about.
Only *audio* streams count: a screencast or a webcam is a stream node too.

Detection events are only sent while a daemon is actually running — `state.json`
present, `daemon_pid` set, **and** `updated_at` inside three 30 s heartbeats. The
file outlives a killed daemon, pid and all, so the heartbeat is the only liveness
signal there is; a stale one also stops the bar rendering a recording that ended
when the process did.

The plugin does not read `config.toml` — it has no TOML parser — so the daemon
publishes the resolved `[[detection.apps]]` table as `detection_rules` in
`state.json`, and `Model.rulesFor` prefers it over the table compiled into
`Model.js`. That is what keeps D13 true here: another meeting application is a
row in the config, never a code change. When `detection.source` is not
`"plugin"` the **daemon** ignores these events, which is what the `accepted`
field of the `event` reply is for.

## The seven bar states (spec §9.2)

| State | Bar shows |
|---|---|
| `idle` | nothing — the widget hides itself |
| `detected` | dim microphone glyph and the app label |
| `recording` | pulsing red dot and `HH:MM:SS` |
| `ending` | the same red dot held steady, still counting |
| `captured` | a static glyph and the queue depth — the audio is safe and nothing is running |
| `transcribing` | a turning glyph and the queue depth |
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
   Quickshell versions differ in how `PwNode.type` is exposed. A node with no
   `audio` interface is skipped first: a screencast or a webcam publishes
   `isStream` and not `isSink` too, and counting one as a capture stream would
   let "a browser playing a video while sharing its screen" look like a call —
   exactly the false positive condition 1 exists to remove.
2. **It is Teams.** `Model.identify` runs the four-shape table in match order:
   the PipeWire client name, then the process binary, then the Hyprland window
   class, then the window title. Every comparison is case-folded. Same shapes,
   same order and the same casefolding as `detect/base.identify`, so the plugin
   and the daemon-side detector agree — and the table itself comes from the
   daemon (`detection_rules` in `state.json`), so it is the user's config that
   is being matched, not a copy compiled in here.

Both conditions must hold before anything is reported. Condition 1 alone is a
Discord call, a Signal call or any WebRTC page, and raising the D4 prompt for one
would offer to record a conversation nobody asked Munin about.

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

## The panel's actions

The notification wrapper takes exactly one `--exec`, so every secondary action
lives here (contracts §8). During the grace period the panel therefore carries a
**Keep recording** button, and the `k` key, which runs `munin event
call-started` — that cancels the countdown; `start --resume` is not it, because
the session is still recording. The *Stops at* row next to it says when the
recording ends by itself if nobody does.

## What is deferred

Everything sketch 04 draws that the PoC does not build: the calendar subtitle
and participant count (M9), *Add marker*, *Discard*, backend status, the mixed
track, and *Settings*. The panel's *Live input* block shows PipeWire's own
levels and says so — the plugin has no access to the capture at all, and a
meter that implied otherwise would be a lie about a recording.
