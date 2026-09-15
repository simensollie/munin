# Munin PoC: status

**Status:** Proof of concept built on branch `poc`, installed and running on the primary Omarchy machine (2026-09-15)
**Date:** 2026-09-15
**Owner:** Simen Sollie
**Implements:** [`../specs/2026-09-14-poc-contracts.md`](../specs/2026-09-14-poc-contracts.md) (PoC contracts) against [`../specs/2026-09-14-meeting-recorder-design.md`](../specs/2026-09-14-meeting-recorder-design.md) (the spec)

> Public copy, same rules as the spec. Every name below is synthetic.

---

## 1. What exists

Milestones M1, M2, M3 and M5 of the [implementation plan](2026-09-14-implementation-plan.md),
plus the seam for M4. Runtime code is Python 3.12+ stdlib only; the tools it
drives are subprocesses.

| Piece | Where | State |
|---|---|---|
| Two-track Linux capture (PipeWire, `pw-record` → `ffmpeg` libopus 24 kbps mono) | `src/munin/capture/linux.py` behind `capture/base.py` | Built, tested, run live |
| Detection: the two conditions of spec §6.3, three Teams shapes | `src/munin/detect/` (daemon side), `plugin/local.munin/Service.qml` (shell side) | Built, tested against fixtures; never seen a real Teams call |
| Spool: session layout, `session.json`, state machine, segments, resume window, disk guard, crash recovery | `src/munin/spool.py`, `config.py`, `paths.py` | Built, tested, run live |
| `munin-rec` daemon: state machine, timers, grace period, resume, notifications, idle inhibit, socket + state file | `src/munin/daemon.py`, `ipc.py`, `notify.py`, `desktop/` | Built, tested, run live |
| `munin` CLI: `start stop toggle status list event doctor setup daemon worker` | `src/munin/cli.py` | Built, tested, run live |
| `munin-work` worker, backend protocol, transcript renderer | `src/munin/worker.py`, `backends/`, `pipeline/render.py` | Built, tested; only the `none` backend exists |
| Omarchy plugin: PipeWire watch, seven bar states, dropdown panel | `plugin/local.munin/` | Built, validates; loaded by the running shell with no QML errors (visual check of the seven states still pending) |
| `install.sh`, `munin doctor`, `munin setup` | repo root, `src/munin/doctor.py`, `setup.py` | Built, tested; `install.sh --enable` run for real, `doctor` reports 0 failed; `setup` not yet run |
| systemd user unit | `systemd/munin.service` | Enabled and active under `graphical-session.target` |

Tests: `417 passed, 1 skipped`. The suite is deterministic and offline.

## 2. Verified live on this machine (2026-09-15)

Run from the repository venv against a scratch data root and the real
PipeWire, with a 440 Hz tone played through `pw-play` as the "application".
Nothing was installed and nothing under `~/.config`, `~/.local` or `~/munin`
was written.

| Check | Result |
|---|---|
| `munin-rec` binds `$XDG_RUNTIME_DIR/munin/rec.sock` (mode 0600) and writes `state.json` | yes |
| `munin start "PoC live test"` → `status --json` shows `recording`, elapsed climbing → `stop` | yes, 11.3 s segment |
| Session directory `recordings/2026/09/<ts>-poc-live-test/` with `mic.opus`, `app.opus`, `session.json`; `inbox/` symlink; `latest` symlink | yes |
| `ffprobe` durations match `session.json` within 0.05 s | yes (11.27 / 11.31 s) |
| Mic track carries the room (not −∞) and does not carry the tone | mic −57 dB RMS |
| App track carries the tone on an ad-hoc start | **−21 dB RMS** after the §16.5 fix; −∞ before it |
| `munin-work --once` exits 0; session becomes `pending` with `pending_reason = "no transcription backend configured"`; `history[]` stays at 5 rows | yes |
| `munin start --resume` inside the window adds segment 2 (`mic.002.opus`, `app.002.opus`) to the same session | yes |
| `munin event call-started …` → `detected` + "Meeting detected" notification; `call-ended` → no recording created (D4) | yes |
| `munin toggle` twice | yes |
| `SIGTERM` to the daemon mid-recording → session finalised as `captured`, "Recording stopped" notification, daemon restarts clean | yes |

### 2.1 The real install (2026-09-15)

`./install.sh --enable` was run by the owner from a terminal. All seven steps
completed. Findings:

- The terminal had **no `XDG_RUNTIME_DIR` and no `HYPRLAND_INSTANCE_SIGNATURE`**,
  so step 5's `hyprctl reload` failed and the first `munin doctor` showed five
  fails that were all that one missing variable. Fixed the same day: the CLI,
  `doctor` and `install.sh` now derive both (`/run/user/<uid>`, and the single
  instance under `$XDG_RUNTIME_DIR/hypr`) and `doctor` reports a derived value
  as a warning rather than a failure. A manual `hyprctl reload` then applied the
  keybind; `hyprctl binds` lists SUPER + SHIFT + R as "Record meeting" (Lua
  binds show a `__lua` dispatcher, not the command text).
- The shell hot-reloaded `local.munin` (one plugin folder with kinds `service`
  and `bar-widget`) and logged no QML error for it. Contracts §15.2 is answered:
  a combined folder loads. `doctor`: `local.munin in bar.layout.right[0]`.
- `munin doctor` from a bare shell after the fix: **23 checks, 0 failed, 2
  warnings** (derived runtime dir; transcription deferred).
- Through the installed unit, from a bare shell: `munin start` → 6.8 s
  recording → `stop`; app track −21.1 dB RMS (tone), mic −56.0 dB; `munin-work
  --once` leaves it `pending` with the deferred-transcription reason. Session:
  `~/munin/recordings/2026/09/2026-09-15T1053-installed-daemon-test/`.
- `hyprctl configerrors` is now empty; the two `hl.focus` errors seen before the
  install came from the dotfiles bindings and are gone after the reload.
- **Step 4 flipped `bar.transparent` from `false` to `true`** and the bar became
  hard to read. Cause: `omarchy plugin enable` is answered by the running shell,
  which persists its *in-memory* configuration (`shell.qml`
  `mutateShellConfig`), and that differed from the file. Restored with
  `omarchy bar transparent false`; the installer now snapshots
  `bar.transparent` and `bar.position` before step 4 and puts them back if the
  enable changed them (regression test with a shim that reproduces the flip).
  Worth reporting upstream: an unattended `plugin enable` should not rewrite
  unrelated bar settings.

### 2.2 Smoke rebuild (2026-09-15, afternoon)

`./install.sh --enable` re-run over the existing install: every step idempotent
(keybind block left unchanged, bar settings preserved). `munin doctor`: 0
failed. `munin toggle` twice recorded 5.8 s with the tone on the app track
(−21.1 dB) and the room on the mic (−55.3 dB); `munin-work --once` left it
pending. Two findings:

- **A plugin hot-reload keeps the old `Model.js`.** The shell logged the reload
  and the new files were on disk, but the bar kept rendering with the previous
  module until `omarchy-restart-shell`. The installer now restarts the shell
  when the plugin it copies differs from the one installed (never on a first
  install or an unchanged re-run). `munin.service` stayed active through the
  restart, which is D20 observed rather than asserted. Never use
  `omarchy-refresh-shell` for this: it resets `shell.json` to defaults and
  drops the widget.
- After the restart the bar shows the dim hourglass with no count for the
  deferred queue (§2.1 follow-up), confirmed by screenshot.

Not verified, and why:

- **The dropdown's buttons and the notification buttons** as clicked by a
  person, and the recording/ending/done/failed bar states on screen. The
  idle-with-queue chip is confirmed by screenshot; the rest of the seven states
  have only been exercised through the state file.
- **A real Microsoft Teams call.** The three detection shapes come from spec
  §6.3, not a measurement. If one fails it is a row in `[[detection.apps]]`.
- **Speech in the microphone track.** Every run was in a quiet room.
- **Idle inhibition** over a real 150 s idle, and how the notifications look on
  screen (their argv is pinned by tests).
- **`detection.source = "daemon"` learning that a meeting ended.** The default
  (`plugin`) path gets `call-ended` from the shell plugin; the daemon-side poller
  does not yet consult `app_stream_present()`.

## 3. Deferred, by design

ASR, language routing, diarization, speaker attribution, the voice register,
the glossary, M365 enrichment, Plaud export and `mixed.mp3`, `transcript.json`,
`markers.json`, the admin web UI, the `ssh` and `api` backends, retention, macOS
and Windows. Pipeline stubs raise `NotImplementedError` pointing at the spec
section. **Compliance flag:** with no retention pass the PoC keeps audio
indefinitely, and `segments[].app_source` now records that an ad-hoc recording
may hold the whole output mix rather than one application.

## 4. Install, run, uninstall

```bash
git checkout poc
./install.sh --dry-run          # shows every step; changes nothing
./install.sh --enable           # 7 steps; --enable also starts munin.service
munin doctor                    # every check as one line; exit 0 = healthy
munin setup                     # pick a microphone, 10 s two-track test
munin start "Weekly quality sync"   # or SUPER + SHIFT + R, or the bar button
munin status --json
munin stop
munin list
./install.sh --uninstall        # reverses steps 2-6, leaves ~/munin
```

What the installer touches outside the repo: `~/.local/share/munin/venv`,
`~/.local/bin/munin{,-rec,-work}`, `~/.config/omarchy/plugins/local.munin/`,
the `bar` layout in `~/.config/omarchy/shell.json` (via `omarchy plugin enable`),
a marked block appended through the `~/.config/hypr/bindings.lua` symlink into
the dotfiles repo (backup in `$XDG_STATE_HOME/munin/`), `~/.config/systemd/user/munin.service`,
and `~/munin/` with a default `config.toml`. It never uses `sudo`; missing
packages are printed as an `omarchy pkg add` line.

Things to look at once after an install: `journalctl --user -t omarchy-shell`
for QML errors mentioning `local.munin`, `hyprctl configerrors`, and `munin
doctor`. A terminal that does not export `XDG_RUNTIME_DIR` is handled: the CLI
derives it and `doctor` says so as a warning.

## 5. Plugging in transcription

One module. Implement `munin.backends.base.Backend` (`transcribe(session) ->
Transcript`, plus `available()`), register it in `backends/__init__.py`, and set
`[transcribe] backend` in `config.toml`. The worker already drains the inbox,
takes the lock, walks `pending → transcribing → done | failed`, renders
`transcript.txt` through `pipeline/render.py` in the contracted format, writes
the `pensieve` copy, and retries a session the moment a backend reports itself
available. Diarization and voice matching stay local regardless of where ASR
runs (D9); the `api` backend splits the pipeline exactly there (spec §8.1).
Which of `local`, `ssh`, `api` comes first is the open decision from plan §5,
and depends on where the model is hosted.
