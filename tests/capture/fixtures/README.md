# Detection fixtures

Decoded `pw-dump` and `hyprctl clients -j` payloads, in the exact object shape
observed on the development machine on 2026-09-15 (PipeWire 1.6.8, Hyprland via
Omarchy 4), reduced to the objects the parser reads.

**Every identity in them is synthetic.** Process ids, `object.serial` values,
application names and window titles were replaced with invented ones, per the
repo's public-copy rule. What is real is the *structure*: which key lives on the
Client object and which on the Node, that `client.id` is the only link between
a stream and its process, and that a browser's audio pid sits one hop below its
window pid.

| File | Shape (spec 6.3) | Live call? | Identified by |
|---|---|---|---|
| `pw-dump-teams-native.json` | native `teams-for-linux` | yes | PipeWire client |
| `pw-dump-teams-pwa.json` | installed PWA | yes | window class |
| `pw-dump-teams-tab.json` | browser tab | yes | window title |
| `pw-dump-browser-playback-only.json` | a video playing | no | — |
| `pw-dump-teams-tab-no-call.json` | Teams tab, no call | no | (title would match) |
| `pw-dump-two-playback-streams.json` | two tabs making noise | yes | window title |
| `pw-dump-idle.json` | nothing running | no | — |

`hyprctl-clients.json` covers every window pid used above; `ppid-map.json` is
the `/proc/<pid>/status` `PPid` map the parent walk needs, so that walk is
tested without a single real process.

Every dump also contains the shell's own `quickshell` node, which holds a
capture stream permanently and must never be mistaken for a call.
