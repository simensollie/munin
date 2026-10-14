# Munin: microphone preflight and `munin-audiofix`

**Status:** Proposed
**Date:** 2026-10-14
**Amends:** [`2026-09-14-meeting-recorder-design.md`](2026-09-14-meeting-recorder-design.md) §6.3 (notifications), §9 (Omarchy integration), §11 (failure handling); [`2026-09-14-poc-contracts.md`](2026-09-14-poc-contracts.md) §3 (`session.json`, additive), §7.2 (state file, additive), §8 (notifications, one row added), §9 (config, one key added), §11 (CLI), §13 (install layout, one script added)
**Decision added:** D28

> Public copy, same rules as the spec. No real customer, colleague or internal
> project names. Device addresses in this document are synthetic
> (`AA:BB:CC:DD:EE:FF`). Counts and timings are real.

---

## 1. Problem

On 2026-10-14 a Bluetooth headset's call-mode link (HFP/SCO) flapped three
times in a morning while the user was in Teams. On the last flap WirePlumber
aborted inside its bluez node-creation script (`create-node.lua:43: attempt to
index a nil value (local 'node')`, WirePlumber 0.5.17) and left a **ghost
device**: the headset's card at profile `off`, a stale microphone node still
registered, and the session's configured default sink naming an output that no
longer existed. Every application, Teams included, fell back to the built-in
output, which was muted. The user lost the meeting on this machine and joined
from another computer.

Munin's part in this was passive and is the point of this spec:

- `munin-rec` resolved the default source as usual, found the stale node,
  pinned it and recorded. The daemon reported `mic=ok` at stop because the
  Opus file was playable. Nothing told the user that the microphone was dead
  at the moment they could still have fixed it.
- Between 13:01 and 13:03 the daemon detected, discarded, ad-hoc-started and
  discarded again, each time opening and closing a mic stream on a link that
  was already failing. Munin did not cause the flap (the same sequence had
  produced three good recordings earlier that day), but it added churn.
- The only repair was `systemctl --user restart wireplumber` followed by a
  reconnect, which the user had no way to know.

Spec §11 covers a track that dies *during* a recording. It does not cover a
microphone that is already dead *before* one starts, which is the common case
after a ghost: every subsequent call is captured from a corpse.

## 2. Goals

1. At the two moments Munin already touches the microphone (call detected,
   capture start), know whether the resolved source can actually deliver
   audio, and say so immediately if it cannot.
2. Offer the repair where the warning is: a *Fix audio* action on the
   notification and in the panel, backed by one script.
3. Make the recording honest about itself: a segment captured from a degraded
   microphone is marked in `session.json`, so the pipeline, the panel and an
   auditor see it.
4. Give `munin doctor` the same checks, so a ghost is visible without a call.

## 3. Non-goals

- Fixing the headset or the Bluetooth stack. The flap is between the adapter
  firmware, bluez and the headset; the WirePlumber nil-index is an upstream
  bug and is reported separately.
- Silently recording from a different microphone when the default is dead.
  A wrong mic that sounds fine is worse than a dead mic that is flagged; the
  user may be on a headset for privacy reasons (§12).
- Refusing to record. D4 says the user confirmed; a degraded mic is a warning
  on the record, not a veto.
- Running anything as root. `munin-audiofix` touches the user's PipeWire and
  WirePlumber session and `bluetoothctl`, nothing else.

## 4. Decision

| # | Decision | Why |
|---|---|---|
| D28 | Munin checks microphone **health**, not just presence, before it notifies and before it captures, and ships `munin-audiofix` as the one-click repair for the ghost-device failure | Presence is what `default_mic_target` checks today and it was satisfied by a node WirePlumber had already marked `error`. The repair is five commands in a fixed order with waits between them; a user in a failing meeting cannot be expected to know or type them. D12 naming applies (`munin-`, never `omarchy-`), the script never captures (D20) and never runs sudo. The user's personal `audiofix` script, written the same day, is the prototype |

## 5. Design

### 5.1 The ghost signature

Three signals, read from one `pw-dump`. Any one of them makes the microphone
**degraded**; the reason names the first that fired.

| Signal | What it looks like | Verified? |
|---|---|---|
| S1 `node-error` | The resolved mic node's `info.state` is `"error"` | **Not verified.** WirePlumber logged `running -> error` for the node, but no `pw-dump` was taken while the ghost existed. `audiofix` now snapshots `pw-dump` before every repair (§5.6), so the next occurrence settles this. Until then the check is written against the other two signals and S1 is added when a fixture exists |
| S2 `profile-off` | A `bluez_card.*` device with `bluez5.profile = "off"` while `api.bluez5.connection = "connected"` | Verified from `pactl list cards` on 2026-10-14: `Active Profile: off`, 0 sinks, 0 sources, connection `connected` |
| S3 `default-sink-missing` | The `default` metadata object's `default.configured.audio.sink` names a node not present in the dump | Verified from `wpctl status` on 2026-10-14: `Default Configured Devices` named `bluez_output.AA_BB_CC_DD_EE_FF.1`, no such sink existed |

S3 is the signal most likely to fire first in practice, because WirePlumber
keeps the configured default even after the node is gone. It is also the
cheapest: one metadata lookup.

A muted or 0 % default sink is **not** a signal. That is a user choice and the
panel already shows it.

### 5.2 Where the check runs

| Moment | Code | On degraded |
|---|---|---|
| Call detected, before the *Meeting detected* notification | `daemon.py`, detection path | Send the *Microphone not working* notification (§5.3) **in addition to** *Meeting detected*, not instead. D4: the user still decides whether to record |
| Capture start, in `default_mic_target` | `capture/linux.py` | Start anyway. Append the reason to the capturer's warnings; write a `mic_degraded` entry to `history[]` (§5.4); set `mic_health` in the state file (§5.5) |
| A capture track dies mid-recording (existing §11 path) | `daemon.py`, `last_error` | Re-run the check so `mic_health.reason` says *why* if it was the ghost |
| `munin doctor` | `doctor.py` | Three new checks: `default sink`, `mic health`, `bluetooth profile` |
| `munin status` | `cli.py` | Prints `mic_health` when not `ok` |

The check reads `pw-dump` once per moment. It must never block detection:
a `pw-dump` failure yields `mic_health = {"state": "unknown", ...}` and the
daemon carries on exactly as today.

### 5.3 Notification and panel

New row in contracts §8:

| When | Urgency | Title / body | Primary action (`--exec`) | Secondaries live in |
|---|---|---|---|---|
| Microphone degraded at call detection or capture start | `critical` | "Microphone not working" / "<label> is <reason>. Recording will have no mic audio. Click to fix audio." | `munin-audiofix` | Panel: *Fix audio*. Once per session, replaced by `-r` like the others |

`<reason>` is one of three user-facing strings, mapped from the signal:
`in an error state` (S1), `connected but has no audio profile` (S2),
`set as default but no longer exists` (S3).

Panel (`BarWidget.qml`): when `status.mic_health.state == "degraded"`, an
`InfoPair` labelled *Microphone* with the reason, and a *Fix audio* action
that runs `munin-audiofix` through `Quickshell.execDetached`, the same path the
existing actions use. The plugin still never captures (D20); it starts a
repair script, which is no different from starting `munin stop`.

**While a recording is live**, *Fix audio* restarts WirePlumber under a running
`pw-record`. What happens to the pinned mic track then is **not verified**
(§7). Until it is, the button is shown but the daemon ends the current segment
and starts a new one after the script exits 0, which the capture model already
supports (`segment` increments on resume). A clean boundary beats a track that
silently re-linked to whatever became default.

### 5.4 `session.json` (additive)

- `history[]` gains an entry `{"event": "mic_degraded", "at": <ts>,
  "segment": n, "reason": "<signal>", "label": "<node description>"}` when a
  segment starts degraded.
- `history[]` gains `{"event": "audio_repaired", "at": <ts>, "segment": n}`
  when `munin-audiofix` exits 0 during a live recording and the daemon rolls
  the segment.
- No new top-level key. The pipeline reads `history[]` for `mic_degraded` and
  the renderer prefixes the transcript with one line, `[microphone degraded
  for segment n: <reason>]`, so a transcript of silence does not pass as a
  quiet meeting.

Compliance: this adds to the audit trail (`history[]`), which is the intended
place; it records a device label and a reason, no personal data beyond what
the session already holds.

### 5.5 State file (additive, contracts §7.2)

```json
"mic_health": { "state": "ok", "reason": null, "label": "Headset", "checked_at": "2026-10-14T13:01:23+02:00" }
```

`state` is `ok`, `degraded` or `unknown`. `reason` is the signal name or
`null`. Written at every moment in §5.2; `null` before the first check.

### 5.6 `munin-audiofix`

Shipped in `bin/`, installed to `~/.local/bin/` by `install.sh` step for
scripts, bash, `set -euo pipefail`, a *why* comment block first. Argument:
optional device, matched as a Bluetooth address or a case-insensitive part of
the paired name; with none, the connected audio device, or the only paired
one. `-n` is a dry run.

Steps, in order:

0. **Snapshot**, read-only, runs in dry-run too: `pw-dump`, `wpctl status`,
   `pactl list cards`, `bluetoothctl info <device>` and the last 30 min of
   the WirePlumber journal, under
   `${XDG_CACHE_HOME:-$HOME/.cache}/munin/audiofix/<ISO timestamp>/`. Keeps
   the last 10. This is where the S1 fixture will come from.
1. `systemctl --user restart wireplumber`, wait until `wpctl status` answers
   (10 s).
2. `bluetoothctl disconnect` if connected, then `connect`; wait for the bluez
   card (15 s) and its sink (10 s).
3. `pactl set-default-sink` / `set-default-source` to the headset; unmute;
   raise the volume to 50 % only if it was at 0.
4. Print the sinks and the configured defaults; exit 0 only when the sink
   exists and is default.

Exit 1 with a message on an unknown device; list the candidates when several
are paired and none is connected. Never `sudo`: a `systemctl restart
bluetooth` would also drop the mouse and every other device, and is left to
the user.

The script is Omarchy-specific only in where it is installed. It needs
`pw-dump`, `wpctl`, `pactl`, `bluetoothctl`, all already required by
`munin doctor` except `bluetoothctl`, which doctor gains as a check.

### 5.7 Config (contracts §9, one key)

```toml
[capture]
mic_source   = "default"
audio_repair = "munin-audiofix"   # argv run by the Fix audio action; "" disables the action
```

The notification and the panel button are omitted when `audio_repair` is
empty. The check itself cannot be disabled; it only reads.

### 5.8 CLI

- `munin doctor`: three new checks (§5.2). `bluetooth profile` is `skip`
  when no `bluez_card.*` exists.
- `munin status`: prints `microphone: degraded (<reason>)` when applicable.
- `munin-audiofix [-n] [device]` as above.

## 6. Tests

Fixtures are real `pw-dump` captures with addresses rewritten to
`AA:BB:CC:DD:EE:FF`, under `tests/fixtures/pw-dump/`:

- `healthy-bluetooth.json`: the 2026-10-14 14:28 snapshot (headset connected,
  A2DP, defaults consistent).
- `ghost-s3.json`: `healthy-bluetooth.json` with the default sink node
  removed, so S3 fires alone.
- `ghost-s2.json`: card at profile `off`, connection `connected`, no sink.
- `ghost-s1.json`: **pending** the next real occurrence.

Cases: each signal alone, all three together, `pw-dump` absent, no Bluetooth
device at all (every check `ok`/`skip`), `audio_repair = ""` suppresses the
action but not the state, `history[]` entry written at degraded start, segment
rolls after a 0 exit from the repair during a live recording, notification
argv carries `--exec munin-audiofix`. The script itself gets a `bash -n` and a
dry-run test against a stub `bluetoothctl` in `PATH`, as `install.sh` is
tested.

## 7. Open questions

1. **S1.** Does the ghost mic node report `info.state == "error"` in
   `pw-dump`? Needs the next occurrence; the snapshot step is there to catch
   it.
2. **Live repair.** When WirePlumber restarts under a running `pw-record`
   pinned by `object.serial`, does the stream re-link to the rebuilt node,
   stall, or exit? `pw-record` is known to fall back to the default source
   when a `--target` disappears (capture/linux.py, module docstring), which is
   the silent-wrong-mic failure §3 rules out. The segment roll in §5.3 is the
   conservative answer until this is measured on this machine.
3. **Detection cadence.** Should the daemon re-check while a call is live but
   no recording is running, so a ghost that forms *during* a call the user
   declined to record still gets the notification? Proposed: no, once per
   detection is enough; a new call re-checks.
4. **Secondary headset.** When two paired headsets are both auto-connected,
   `munin-audiofix` reconnects one and leaves the other. Whether it should
   disconnect the other first (the 09:03 drop on 2026-10-14 was one headset
   admitting the other) is a user preference, not a repair step. Left out.

## 8. Rollout

1. `munin-audiofix` from the prototype, with the snapshot step; install and
   doctor wiring. Useful on its own the same day.
2. S2 and S3 checks in `doctor` and `default_mic_target`; state file and
   `history[]`; `munin status`.
3. Notification row and panel `InfoPair` plus *Fix audio*.
4. S1 and the live-repair behaviour once a fixture and a measurement exist.

No migration: every field is additive and `schema_version` stays at 1.
