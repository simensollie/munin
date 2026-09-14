# Munin PoC: status

**Status:** Proof of concept built on branch `poc`; not yet installed on a machine
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
| Omarchy plugin: PipeWire watch, seven bar states, dropdown panel | `plugin/local.munin/` | Built, validates, QML compiles; **never loaded in the running shell** |
| `install.sh`, `munin doctor`, `munin setup` | repo root, `src/munin/doctor.py`, `setup.py` | Built, tested against a shimmed machine and `--dry-run`; **never run for real** |
| systemd user unit | `systemd/munin.service` | Written; never started by systemd |

Tests: `408 passed, 1 skipped`. The suite is deterministic and offline.

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

Not verified, and why:

- **Install on the real machine.** `install.sh --enable` was not run in this
  session (the automated run was not permitted to change user configuration), so
  the plugin has never loaded in the running Omarchy shell, the bar placement,
  the keybind reload and the systemd start are unproven. Each is one command;
  see §4.
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

After the first real install, the things to look at once: `journalctl --user
-t omarchy-shell` for QML errors mentioning `local.munin`; `hyprctl
configerrors` (this machine already reports two unrelated `hl.focus` errors
from the dotfiles bindings); and `munin doctor`.

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
