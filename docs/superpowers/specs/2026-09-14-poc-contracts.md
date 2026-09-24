# Munin PoC: frozen contracts

**Status:** Frozen for the PoC build
**Date:** 2026-09-14
**Amended:** 2026-09-16 — D24 removed the second-brain copy. **Breaking**, see below.
**Implements:** [`2026-09-14-meeting-recorder-design.md`](2026-09-14-meeting-recorder-design.md) (the spec)
**Sequences against:** [`../plans/2026-09-14-implementation-plan.md`](../plans/2026-09-14-implementation-plan.md)

> Public copy, same rules as the spec. No real customer, colleague or internal
> project names. Synthetic names only (`Ola Nordmann`, `Kari Nordmann`, `NORVA`,
> `Beacon 365`).

---

## 0. Scope of the PoC

**In:** install on this Omarchy machine, detect or be told a call is live, ask,
capture two independent tracks to Opus, write a conformant session directory,
show state in the bar, and leave the session in the spool as `pending` with a
clear reason.

**Out (deferred, but the seam exists):** ASR, diarization, voice matching,
glossary, M365 enrichment, Plaud export, `mixed.mp3`, admin web UI, `ssh` and
`api` backends, retention. No model is downloaded and none is run.

The worker, `backends/base.py` and `pipeline/render.py` exist so that adding a
real backend later is one module. `backends/none.py` is the only backend the PoC
ships and it never transcribes.

### Amendment 2026-09-24 (window subject as title): additive

A detected session is named from the meeting application's window title before
it falls back to the app label plus the clock. §3's `title` rule gains one step;
nothing else changes, and `munin start "..."` still wins over both.

The clock fallback produced `Microsoft Teams 14:29`, which is the same sentence
for every meeting of the day and repeats a time the directory name already
carries. Teams shapes its window title as `[(n) ]<surface> | <context> | <app>`,
and the context field is the only part that ever names anything:
`munin.daemon.meeting_subject()` is the single implementation, pure and tested
without hardware.

Measured by replaying it over the 15 sessions on the reference machine: 14 got a
name. **Thirteen of those are participants, not subjects** — they were calls
placed from a chat, which have no subject in Teams at all — and one was a
calendar meeting, which yielded its invite subject. So this names a session after
*who* far more often than after *what*. It is an improvement on a clock and it is
**not** a substitute for M365 enrichment (spec §7.6), which remains the only
source that knows the subject every time.

Additive: `meeting_subject()` returns `None` for a title holding nothing but the
application, so a session that would have been named from the clock before still
is. No `session.json` written before this amendment changes meaning, and no
directory is renamed.

**Compliance (spec §12): this is a new data flow, not only a naming change.**
The session id is the Plaud export filename (§10), so a colleague's name now
leaves the machine with the upload where `microsoft-teams-14-29` disclosed
nothing. It is the user's own opt-in `[export]` folder and a manual drag, but it
is a disclosure to a third party under their retention and it goes with D25.
Retention defaults remain open.

### Amendment 2026-09-21 (exported once): additive

Automating the copy made the folder current; it also made it refill itself. The
2026-09-18 amendment left "the destination file existing is still the whole of
the bookkeeping", which reads a missing file as *never exported* -- and deleting
or moving the file is exactly how an upload ends. Upload a meeting, clear the
file, and the next sweep put it straight back. Observed on this machine with
eleven files in the folder.

| Surface | Before | After |
|---|---|---|
| upload folder | the audio files only | plus `.exported/<session id>`, one marker per session it has carried |
| `munin-work` | exports whenever the file is absent | exports once per session, ever |
| `munin mix --all` | every finished session | every session the folder has not carried; `--force` overrides |
| `munin mix <session>` | mixes and copies | unchanged: an explicit ask always runs, and re-marks |
| `munin doctor` | names the folder | also counts what waits and what has been carried |

The marker holds a timestamp and the filename it was written for, is named for
the session alone (not the format, so changing `[export] format` does not
re-export the archive), and is written only *after* a copy lands, so a failed
export still retries.

What did **not** change: nothing is written to `session.json` -- no field, no
state transition, no history entry. Spec §10 sketched the other design
(`munin mark-uploaded`, upload state in session metadata) and it stays rejected
for that reason; the ledger sits in the folder it describes and is deleted with
it (D25).

Given up: self-healing. A copy deleted by accident no longer returns on the next
sweep. `munin mix <session>` puts it back, which is the right way round -- a
folder that recreates files you removed is the louder failure, and it is the one
that actually happened.

Additive: a folder with no `.exported/` behaves as before until the first sweep,
which backfills a marker for every file already sitting there. Sessions whose
files were cleared *before* this amendment are exported once more, then stay
gone; `touch <folder>/.exported/<session id>` skips even that.

### Amendment 2026-09-18 (auto-export): additive

The mix may now be produced automatically. Section 7.2 said it is written "never
automatically"; that holds only while the upload folder is refilled by hand, and
on this machine it was not -- three meetings sat unexported for a day, with
nothing in `munin list`, `state.json` or a notification to say so, because the
mix has no bookkeeping to be missing from.

| Surface | Before | After |
|---|---|---|
| `config.toml` | no `[export]` section | `[export]` with `enabled`, `directory`, `format` |
| `munin-work` | claims and processes sessions | also mixes and copies, when `[export] enabled` |
| systemd | one unit, `munin.service` | a second unit, `munin-work.service` |
| `munin mix` | `--to` unset, `--format opus` | both fall back to `[export]` |

What did **not** change, and must not: the mix is still absent from the session
record. No field, no state transition, no history entry, which is what keeps a
re-encode from looking like a capture event and lets a missed export self-heal
on the next sweep rather than needing a retry record. The destination file
existing is still the whole of the bookkeeping.

Additive: `enabled` defaults to `false`, so a machine that does not set it
behaves exactly as before, and a `config.toml` written before this amendment
loads unchanged. D11 still calls the Plaud route opt-in -- the opt-in moved from
"per meeting, by typing `munin mix`" to "once, in the config". All of it goes
with D25.

### Amendment 2026-09-16 (D24): breaking

The second-brain copy is removed. Three frozen surfaces changed:

| Surface | Before | After |
|---|---|---|
| `session.json` `transcript` | `{txt, json, <copy-path key>}` | `{txt, json}` |
| `config.toml` | a second-brain section with a `raw_dir` | section removed |
| `pipeline/render.py` | exported a copy-filename helper | removed |

Migration:

- **`session.json`** written before this amendment carries a third key under
  `transcript` that nothing reads any more. It is inert, and no rewrite is
  needed.
- **`config.toml`** carrying the old section still loads; the section name
  lands in `unknown_keys`, and `munin doctor` prints `unknown keys kept but
  ignored`. Delete the section to clear the warning.
- **The copy-filename helper** has no deprecation path, so a caller breaks at
  import. Nothing calls it.
- **No data migration exists.** The PoC ships the `none` backend, so this build
  has never produced a transcript.

The removed names are spelled out in the commit that made this change
(`96f76c2`), which is the one place the old identifiers survive.

**This document is frozen.** Six workstreams code against it in parallel. A
workstream that finds a contract wrong reports it as a **deviation** (to the
architect, in its return value) rather than editing a shared file. §14 lists what
is shared.

---

## 1. Package layout and dev workflow

Per plan §3, reduced to what the PoC needs.

```
munin/
  pyproject.toml            frozen (§14)
  .gitignore
  install.sh                install workstream
  systemd/munin.service     daemon workstream
  plugin/local.munin/       plugin workstream
    manifest.json  Service.qml  BarWidget.qml  Model.js
  src/munin/
    __init__.py             __version__
    cli.py                  daemon workstream
    daemon.py               daemon workstream  (munin-rec)
    ipc.py                  daemon workstream
    notify.py               daemon workstream
    worker.py               worker workstream  (munin-work)
    spool.py                spool workstream
    config.py               spool workstream
    paths.py                spool workstream
    doctor.py               install workstream
    setup.py                install workstream
    capture/__init__.py     FROZEN — platform factory
    capture/base.py         FROZEN — the interface
    capture/linux.py        capture workstream
    detect/__init__.py      FROZEN — platform factory
    detect/base.py          FROZEN — the interface
    detect/linux.py         capture workstream
    backends/__init__.py    FROZEN
    backends/base.py        FROZEN — Backend protocol
    backends/none.py        worker workstream
    pipeline/__init__.py    FROZEN
    pipeline/render.py      worker workstream
  tests/conftest.py         FROZEN — fixtures
  tests/{capture,spool,daemon,worker,install}/
```

`pyproject.toml`: `requires-python = ">=3.12"`, **no runtime dependencies**
(stdlib only, `tomllib` included), console scripts

| Script | Entry point |
|---|---|
| `munin` | `munin.cli:main` |
| `munin-rec` | `munin.daemon:main` |
| `munin-work` | `munin.worker:main` |

and one optional extra, `[dev]` → `pytest`.

Dev workflow (there is no `uv`, `pip` or `pipx` on this machine; the venv's own
`pip` from `ensurepip` is the only installer):

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

`.gitignore` carries at least `.venv/`, `__pycache__/`, `*.egg-info/`.

Runtime code is stdlib-only. External **tools** invoked as subprocesses are
allowed and expected: `pw-record`, `pw-dump`, `pw-cli`, `ffmpeg`, `hyprctl`,
`omarchy-notification-send`, `omarchy-toggle-idle`.

---

## 2. Data root, paths, naming

All data lives under `~/munin/` (D18). The root is resolved once, by
`munin.paths`:

1. `$MUNIN_HOME` if set (tests and the installer use this), else
2. `[munin] home` in the config file, else
3. `~/munin`.

```
$MUNIN_HOME/
├── recordings/YYYY/MM/<session-id>/
├── inbox/<session-id> -> ../recordings/YYYY/MM/<session-id>   (symlink)
├── voices/            (created, unused in the PoC)
├── config.toml
├── munin.log
└── latest -> recordings/YYYY/MM/<session-id>                  (symlink)
```

**Session id** — `YYYY-MM-DDTHHMM-<slug>`, local time, no seconds, no timezone
suffix. `2026-09-14T1325-weekly-quality-sync`.

**Slug** — from the title: NFKD-fold to ASCII (`æ→ae`, `ø→oe`, `å→aa` first),
lowercase, every run of non-`[a-z0-9]` → `-`, strip leading/trailing `-`,
truncate to 48 characters at a `-` boundary. Empty result → `adhoc`. Collision
(directory exists) → append `-2`, `-3`, …

**`YYYY/MM`** come from the session's *created* local date, never recomputed.

**`inbox/`** holds one relative symlink per session that is captured but not
finished, created by the daemon at `captured` and removed by the worker at
`done`. A session that ends `pending` or `failed` keeps its symlink — that is
what makes it visible. The worker must also tolerate a missing or broken
`inbox/` and fall back to scanning `recordings/`, because the symlink is an index,
not the record.

*Counterargument:* symlinks do not port to Windows without developer mode, and a
queue file would. Accepted for the PoC because it is greppable, needs no locking,
and the scan fallback means losing the index loses nothing. Revisit at M14.

**`latest`** is a relative symlink to the most recently *created* session,
replaced atomically (`symlink` to a temp name + `os.replace`) by the daemon on
`recording`.

**Timestamps** in every JSON file are ISO 8601 with a local UTC offset and second
precision: `2026-09-14T13:25:07+02:00`. Never naive, never `Z`-normalised — the
directory name is local time and the two must agree on the day.

---

## 3. `session.json`

One file per session, the only file the worker writes state into (spec §7.5).
Written atomically (write `session.json.tmp` in the same directory, `os.replace`).
Any writer re-reads immediately before writing; the file is small and single-writer
by state (§4).

```json
{
  "schema_version": 1,
  "id": "2026-09-14T1325-weekly-quality-sync",
  "state": "captured",
  "title": "Weekly quality sync",
  "slug": "weekly-quality-sync",
  "source": "adhoc",
  "platform": "linux",
  "capture_method": "pipewire-pw-record+ffmpeg-libopus",
  "host": "workstation",
  "munin_version": "0.1.0",
  "created_at": "2026-09-14T13:25:07+02:00",
  "started_at": "2026-09-14T13:25:08+02:00",
  "stopped_at": "2026-09-14T14:02:11+02:00",
  "duration_seconds": 2223.4,
  "app": {
    "app_id": "teams-tab",
    "label": "Microsoft Teams",
    "matched_by": "window_title",
    "pid": 37022,
    "client_name": "Chromium",
    "binary": "chrome",
    "window_class": "google-chrome",
    "window_title": "Weekly quality sync | Microsoft Teams"
  },
  "calendar_event_id": null,
  "segments": [
    {
      "index": 1,
      "mic": "mic.opus",
      "app": "app.opus",
      "started_at": "2026-09-14T13:25:08+02:00",
      "stopped_at": "2026-09-14T13:52:40+02:00",
      "duration_seconds": 1652.0,
      "app_source": "stream"
    },
    {
      "index": 2,
      "mic": "mic.002.opus",
      "app": "app.002.opus",
      "started_at": "2026-09-14T13:56:10+02:00",
      "stopped_at": "2026-09-14T14:02:11+02:00",
      "duration_seconds": 361.0,
      "app_source": "process-sink"
    }
  ],
  "checksums": { "mic.opus": "sha256:…", "app.opus": "sha256:…" },
  "pending_reason": "no transcription backend configured",
  "error": null,
  "transcript": { "txt": null, "json": null },
  "history": [
    { "at": "2026-09-14T13:25:08+02:00", "from": null, "to": "recording", "by": "munin-rec" }
  ]
}
```

Field rules:

| Field | Rule |
|---|---|
| `schema_version` | `1`. A reader that sees a higher number refuses the session rather than guessing. |
| `state` | §4. |
| `title` | User-supplied, else the meeting-app window subject (amendment 2026-09-24), else the detected app label plus time, else `Meeting <HH:MM>`. Never `null`. |
| `source` | `"adhoc"` (user started it) or `"detected"` (started from a detection prompt or event). |
| `platform` | `sys.platform` value: `"linux"`. §16.5 — a transcript stays reproducible on a machine that could not have recorded it. |
| `capture_method` | `Capturer.method` (§5). Frozen string per implementation. |
| `app` | `null` for a pure ad-hoc session with no identified app. Otherwise every key present, `null` where unknown. |
| `calendar_event_id` | Always `null` in the PoC. The key exists so the schema does not change at M9. |
| `segments` | Never empty once `state` has left `recording` successfully. `index` is 1-based and contiguous. |
| `segments[].app_source` | **Added at integration (§16); `process-sink` added in §16.6.** What the app track actually holds: `"process-sink"` (the meeting application moved onto a sink of its own — one process and nothing else, whatever it does to its streams), `"stream"` (one of the application's stream nodes, bound directly — also isolated, but only for an application that keeps one stream), `"sink-monitor"` (the whole desktop mix, taken because neither of the above could be had) or `"silent"` (a generated silent track, an ad-hoc session with no application). `null` means a capturer that does not report one — read as *unknown*, never as *isolated*. **`process-sink` and `stream` are the isolated values**; the daemon treats everything else as a widened capture and says so while it is happening. |
| `checksums` | `sha256:<hex>` per audio file, keyed by filename, computed by the daemon at `captured`. |
| `pending_reason` | Non-null only in state `pending`. Human-readable, one sentence. |
| `error` | Non-null only in state `failed`: `{ "code": "...", "message": "...", "at": "..." }`. |
| `history` | Append-only list of every transition, including the writer. This is the audit trail (§12 of the spec); do not trim it. |

**Segment filenames** — segment 1 is `mic.opus` / `app.opus` (spec §7.5); segment
*n* > 1 is `mic.NNN.opus` / `app.NNN.opus`, zero-padded to three digits
(`mic.002.opus`). `munin.capture.base.segment_filenames(index)` is the single
implementation; nothing else may construct these names.

`transcript.*` and `markers.json` are not produced in the PoC. The mix is:
**added after the first live meeting**, because with `backend = "none"` a manual
upload elsewhere is the only route from a recording to text. It is written on
demand by `munin mix` (spec §10) as `mixed.opus`, or `mixed.mp3` with
`--format mp3` -- and, since the 2026-09-18 amendment, by `munin-work` on every
sweep when `[export] enabled` is set. Either way it is deliberately *not* on the
session record -- no field, no state transition, no history entry. A re-encode of audio
that already exists is not a capture event, and the `history` list is the audit
trail for what was captured. Its bookkeeping is the filesystem: the file in the
session directory, and one marker per session under
`<upload folder>/.exported/`, which is what stops a copy deleted after its
upload from being written again (amendment 2026-09-21). All of it is temporary
(spec D25).

**Why `app_source` is on the record.** The four values are not interchangeable.
A `sink-monitor` track is the whole desktop mix: other applications, other
people's audio, notification sounds — material that was never part of the
meeting and whose subjects never saw a recording prompt. That is a GDPR and
ISO 27001 question about *what was captured*, not a capture-quality footnote, and
it cannot be recovered from the audio afterwards. The daemon reads it off the
frozen `Capturer.describe()` seam (§5), so no platform module changed to supply
it.

---

## 4. State machine

```
            daemon (munin-rec)                       worker (munin-work)
   ┌──────────────────────────────────┐   ┌────────────────────────────────────┐
   │ recording ──▶ ending ──▶ captured │──▶│ pending ──▶ transcribing ──▶ done  │
   │     │            │                │   │    ▲             │                 │
   │     └────────────┴──▶ failed      │   │    └─────────────┴──▶ failed       │
   └──────────────────────────────────┘   └────────────────────────────────────┘
```

| From | To | Written by | When |
|---|---|---|---|
| — | `recording` | `munin-rec` | Capture processes are up and both tracks are being written. |
| `recording` | `ending` | `munin-rec` | The app stream disappeared; the grace period is running. |
| `ending` | `recording` | `munin-rec` | Stream came back, or the user chose *Keep recording*. |
| `recording`/`ending` | `captured` | `munin-rec` | Encoders exited, files fsynced, checksums written, `inbox/` symlink created. |
| `recording`/`ending` | `failed` | `munin-rec` | Capture died and nothing usable remains. Partial audio is kept and the session goes to `captured` instead wherever it is playable. |
| `captured` | `pending` | `munin-work` | The worker has seen the session and queued it. |
| `pending` | `transcribing` | `munin-work` | A backend is available and has been handed the session. |
| `transcribing` | `pending` | `munin-work` | On worker start: any session left `transcribing` is reset (spec §11, crash recovery). |
| `transcribing` | `done` | `munin-work` | `transcript.txt` and `transcript.json` written into the session directory. |
| `pending`/`transcribing` | `failed` | `munin-work` | Unrecoverable pipeline error. Audio is retained. |

**Ownership rule (spec §11): after `captured`, only the worker writes `state`.**
The daemon owns `recording`, `ending`, `captured` and capture-side `failed`; it
must never write `pending`, `transcribing` or `done`. The worker must never write
`recording`, `ending` or `captured`.

**Resume (D15).** `munin start --resume` inside `resume_window_seconds` (600) of
the previous session's `stopped_at` reopens that session directory, appends a new
segment, and sets `state` back to `recording`. This is the one worker-visible
state moving backwards, and it is the daemon's to write; it is legal only while
the session is still `captured` or `pending` — never once it is `transcribing` or
`done`. Past the window, or on a session in a later state, `--resume` starts a
fresh session and says so.

In the PoC the worker reaches `pending` and stops there, because the configured
backend is `none` (§9). `transcribing` and `done` are implemented as transitions
but never exercised.

---

## 5. Capture interface — `capture/base.py` (FROZEN)

Nothing above `capture/` and `detect/` may import a platform module (spec §16.5).
`capture/__init__.py` exposes `get_capturer(platform=None) -> type[Capturer]`,
which imports the platform module lazily by `sys.platform`.

```python
@dataclass(frozen=True)
class CaptureTarget:
    kind: Literal["mic", "app"]
    handle: str            # opaque platform token (PipeWire object.serial, "default", …)
    label: str             # human-readable, for status output
    pid: int | None = None
    app_id: str | None = None

@dataclass(frozen=True)
class CaptureResult:
    segment_index: int
    mic_path: Path
    app_path: Path
    started_at: datetime
    stopped_at: datetime
    duration_seconds: float

class CaptureError(RuntimeError): ...

class SessionRef(Protocol):            # structural: spool.Session satisfies it
    id: str
    directory: Path

class Capturer(ABC):
    method: ClassVar[str]              # goes into session.json capture_method
    def __init__(self, mic: CaptureTarget, app: CaptureTarget | None, *,
                 bitrate_kbps: int = 24, channels: int = 1,
                 sample_rate: int = 48000) -> None
    @abstractmethod def start(self, session: SessionRef, segment_index: int) -> None
    @abstractmethod def stop(self) -> CaptureResult
    @property @abstractmethod def is_running(self) -> bool
    @abstractmethod def describe(self) -> dict[str, str]
```

Rules the implementation must honour:

- **Two independent streams.** The mic track and the app track are separate
  processes writing separate files. One failing must not truncate the other, and
  `stop()` must return a `CaptureResult` if *either* file is playable, raising
  `CaptureError` only when neither is.
- `start()` is idempotent-hostile: calling it while `is_running` raises
  `CaptureError`.
- `stop()` on a stopped capturer raises `CaptureError`.
- `app is None` is legal (ad-hoc recording with no app identified): the app track
  is then a valid, silent Opus file of the same duration, so `segments[]` is
  always a pair and the worker never special-cases it.
- `describe()` returns flat `str → str` for `munin doctor` and `status --json`
  (e.g. `{"method": ..., "mic": ..., "app": ..., "encoder": ...}`).
- **Optional, not part of the ABC:** a capturer may offer
  `failed_tracks() -> list[TrackHealth]`, a cheap non-blocking read of which
  tracks have died while capture is running. The daemon polls it every 5 s where
  it exists and treats its absence as "cannot answer", never as "healthy". It
  has to be polled: ffmpeg's Ogg-Opus muxer writes nothing until close, so an
  encoder killed mid-meeting is otherwise invisible until `stop`, by which time
  the whole segment is gone.

**Linux implementation (capture workstream), verified by the scouts:**

- Mic: `pw-record --target <serial|default> --format s16 --rate <rate> --channels 1 -`
  piped into `ffmpeg -f s16le -ar <rate> -ac 1 -i pipe:0 -c:a libopus -b:a 24k <out>.opus`.
- App: the same pipe, in one of four modes, tried strictly in this order and
  never upwards. Each step down records less of the meeting and more of
  everything else, so each is reported as `segments[].app_source` and the
  daemon notifies below `stream` (§8).

  | Order | `app_source` | How | Chosen when |
  |---|---|---|---|
  | 1 | `process-sink` | A `module-null-sink` named `munin-app-<pid>`, a `module-loopback` from its monitor so the user still hears the call, every output stream of the target process moved onto it, and `pw-record --target munin-app-<pid> -P "{ stream.capture.sink = true, node.dont-reconnect = true }"`. §16.6. | The app `CaptureTarget` carries a `pid`. |
  | 2 | `stream` | `--target <object.serial of the app's Stream/Output/Audio node>`, no `stream.capture.sink`. Isolates one application cleanly (measured ~52 dB rejection of a second concurrent stream) — but only an application that keeps one stream node, which Chromium does not (§16.6). | No pid, or a private sink could not be created, *and* the handle still names a live node. |
  | 3 | `sink-monitor` | `stream.capture.sink = true` with no `--target`: the whole desktop mix. | Asked for outright via `SYSTEM_OUTPUT_HANDLE` (§16.5), or everything above failed. |
  | 4 | `silent` | A generated silent Opus file of the segment's measured duration. | No application at all, or every mode above failed. |

- The app track always carries `node.dont-reconnect = true`. Measured: without
  it, a `--target` that disappears makes `pw-record` **silently record the
  default source** — the user's own microphone on the meeting track. With it the
  track goes to true digital silence instead.
- `--target <sink>.monitor` does **not** work for mode 1: `.monitor` is a
  PulseAudio-compatibility name pw-record cannot resolve, and it recorded the
  microphone. The sink's own name plus `stream.capture.sink` is the form.
- The daemon calls `munin.capture.recover_capture()` at startup (the
  platform-free seam) so that private sinks a SIGKILLed `munin-rec` left loaded
  are unloaded and any stream still on one is moved back to the default sink.
- `XDG_RUNTIME_DIR` must be set in the child environment or every PipeWire tool
  fails silently. The daemon gets it from systemd (`Environment=XDG_RUNTIME_DIR=%t`);
  the capturer must still assert it and raise `CaptureError` with a clear message
  if it is empty.
- `capture_method` string: `"pipewire-pw-record+ffmpeg-libopus"`.
- *Counterargument to the ffmpeg pipe:* `pw-record --format opus` writes Ogg-Opus
  natively and halves the process count. Rejected for the PoC because it gives no
  bitrate control and the spec fixes 24 kbps mono (§6.1); the extra process is
  cheap and the pipe is the same shape macOS and Windows will need.

---

## 6. Detection interface — `detect/base.py` (FROZEN)

The two independent conditions of spec §6.3 **are** the interface. Per-platform
code only supplies evidence.

```python
@dataclass(frozen=True)
class CallEvidence:                 # condition 1: is a call live
    pid: int
    has_playback: bool
    has_capture: bool
    playback_handle: str | None     # token to hand to CaptureTarget(kind="app")
    client_name: str | None         # PipeWire application.name
    binary: str | None              # application.process.binary
    @property def is_call(self) -> bool     # has_playback and has_capture

@dataclass(frozen=True)
class WindowInfo:
    pid: int
    window_class: str | None
    title: str | None

@dataclass(frozen=True)
class AppRule:                      # one row of the config table
    app_id: str
    label: str
    client_name: str | None = None            # match on PipeWire client
    binary: str | None = None
    window_class: str | None = None           # exact match
    window_title_contains: str | None = None  # case-insensitive substring

@dataclass(frozen=True)
class Identity:                     # condition 2: is it Teams
    app_id: str
    label: str
    matched_by: Literal["pipewire", "window_class", "window_title"]

@dataclass(frozen=True)
class DetectedCall:
    evidence: CallEvidence
    identity: Identity | None
    window: WindowInfo | None
    observed_at: datetime

def identify(evidence, window, rules) -> Identity | None   # pure, fully written in base.py

class Detector(ABC):
    platform: ClassVar[str]
    def __init__(self, rules: Sequence[AppRule]) -> None
    @abstractmethod def evidence(self) -> list[CallEvidence]
    @abstractmethod def window_for_pid(self, pid: int) -> WindowInfo | None
    def scan(self) -> list[DetectedCall]     # concrete: evidence → filter is_call → identify
    @abstractmethod def describe(self) -> dict[str, str]
```

`identify()` implements the three-shape table in match order — **PipeWire client
first, then window class, then window title** — and is pure, so the detection
matrix of spec §13 is tested without hardware. Adding Zoom, Meet or Slack is a
row in `[[detection.apps]]`, never code (D13).

**Linux implementation (capture workstream):** `evidence()` parses `pw-dump`
JSON. Verified by the scouts: process identity lives on the **Client** object,
not on the stream node. Build `client.id → (pid, binary, application.name)` from
`type == "PipeWire:Interface:Client"` objects, then join every node whose
`media.class` starts with `Stream/` to its client; a pid holding both
`Stream/Output/Audio` and `Stream/Input/Audio` satisfies condition 1.
`playback_handle` is the output node's `object.serial` as a string.
`window_for_pid()` shells `hyprctl clients -j` and matches `pid`, walking
`/proc/<pid>/status` `PPid` up to **5 hops** (Chrome's audio process is one hop
below its window pid; only one hop was observed, so the walk is bounded, not
assumed). `hyprctl` needs `HYPRLAND_INSTANCE_SIGNATURE`; if it is unset, resolve
it from the single entry in `$XDG_RUNTIME_DIR/hypr/`, and degrade to
`window=None` (PipeWire-only matching) rather than failing.

**Where evidence comes from.** The daemon owns the state machine and every timer.
Evidence reaches it two ways, chosen by `detection.source`:

| `detection.source` | Evidence path |
|---|---|
| `"plugin"` (default on Omarchy) | The shell plugin watches `Quickshell.Services.Pipewire` and calls `munin event call-started --pid … --app … --app-id … --handle … --title …` / `munin event call-ended --pid …`. No polling in the daemon. **`--handle` is not optional in practice:** it is the `object.serial` of §6's `playback_handle`, and without it the daemon has no app target and the capturer writes a *silent* app track. `--app` is the human label and `--app-id` the rule id, matching what the daemon's own poller passes. The plugin reports only calls its rule table identifies — condition 1 alone is a Discord call or a WebRTC page, not a meeting. |
| `"daemon"` | `detect/linux.py` polls `pw-dump` + `hyprctl` every `detection.poll_seconds` and raises the same internal events. |
| `"off"` | No detection. Ad-hoc only. |

The PoC ships both paths. `munin doctor` always uses `detect/linux.py` directly
to print what it can currently see, whatever `detection.source` says — that is
how a broken plugin is told apart from a broken detector.

---

## 7. Daemon IPC and the plugin-facing state file

### 7.1 Socket

`$XDG_RUNTIME_DIR/munin/rec.sock`, `AF_UNIX`/`SOCK_STREAM`, directory mode
`0700`, socket mode `0600`. Stale socket at startup: connect first; on
`ECONNREFUSED`, unlink and rebind; on success, exit 3 ("another munin-rec is
running").

Protocol: **one JSON object per line, UTF-8, `\n`-terminated**, request then
reply, then the connection may carry further requests. Max 64 KiB per line. The
daemon never pushes unsolicited frames in the PoC.

Request: `{"cmd": "<name>", ...args}`
Reply, success: `{"ok": true, "cmd": "<name>", "data": {…}}`
Reply, failure: `{"ok": false, "cmd": "<name>", "error": {"code": "…", "message": "…"}}`

| `cmd` | Args | `data` on success |
|---|---|---|
| `ping` | — | `{"pid": int, "version": str}` |
| `status` | — | the full state view of §7.2 |
| `start` | `title?: str`, `resume?: bool`, `from_detection?: bool` | `{"session": "<abs path>", "session_id": str, "state": "recording", "segment": int, "resumed": bool}` |
| `stop` | — | `{"session": …, "session_id": …, "state": "captured", "duration_seconds": float}` |
| `toggle` | `title?: str` | the `start` or `stop` payload plus `"action": "started" \| "stopped"` |
| `event` | `event: "call-started" \| "call-ended"`, `pid?: int`, `app?: str`, `title?: str` | `{"accepted": bool, "state": str}` |
| `list` | `limit?: int` (default 20) | `{"sessions": [{"id","state","title","started_at","duration_seconds","path","pending_reason"}]}` |

Error codes: `bad_request`, `unknown_command`, `already_recording`,
`not_recording`, `no_space`, `capture_failed`, `internal`.

### 7.2 State file

`$XDG_RUNTIME_DIR/munin/state.json`, written **atomically (tmp + `os.replace`)
on every transition** and on daemon start and clean exit. It is the plugin's only
input.

```json
{
  "schema_version": 1,
  "state": "recording",
  "since": "2026-09-14T13:25:08+02:00",
  "started_at": "2026-09-14T13:25:08+02:00",
  "title": "Weekly quality sync",
  "session": "/home/<user>/munin/recordings/2026/09/2026-09-14T1325-weekly-quality-sync",
  "session_id": "2026-09-14T1325-weekly-quality-sync",
  "segment": 1,
  "detected_app": { "app_id": "teams-tab", "label": "Microsoft Teams", "pid": 37022 },
  "grace_deadline": null,
  "queue_depth": 2,
  "last_error": null,
  "detection_rules": [
    { "app_id": "teams-tab", "label": "Microsoft Teams", "window_title_contains": "Microsoft Teams" }
  ],
  "updated_at": "2026-09-14T13:25:08+02:00",
  "daemon_pid": 4211
}
```

`state` ∈ `idle | detected | recording | ending | captured | transcribing | done |
failed` — the bar states of spec §9.2. `idle` and `detected` exist only here (no
session yet); `transcribing`, `done` and `failed` are mirrored from the newest
session's `session.json`. `started_at` lets the plugin compute elapsed time
itself, so **no timer ever writes this file**. `grace_deadline` is set only in
`ending`. `queue_depth` is the count of sessions in `inbox/`.

`transcription_backend` is `[transcribe] backend` as the daemon loaded it.
With `"none"` the plugin renders a captured queue as a dim hourglass with no
count and no spinner: nothing will drain it, and the panel carries the count
and the reason. (Added after the first real install, where the bar showed a
permanent "1 queued" and the panel painted the pending session as a failure.)

`resume_window_seconds` is `[detection] resume_window_seconds` (D15). The
panel uses it with `since` to label *Resume* with the time left in the window
and to offer *New recording* beside it; past the window the one button reads
*New recording*, which is what `munin start --resume` would have done anyway.

`detection_rules` is the resolved `[[detection.apps]]` table, flattened with
empty fields dropped. The plugin is the default detection source and has no TOML
parser, so this is how D13 ("another application is a row in `config.toml`, never
code") reaches it; the plugin falls back to its own compiled-in table only when
the key is absent or empty.

**A reader must treat this file as stale when `updated_at` is older than three
heartbeats (90 s).** `state.json` lives in `$XDG_RUNTIME_DIR` and survives a
SIGKILL'd daemon, pid and `state: "recording"` intact — the heartbeat is the only
liveness signal there is, which is why one is written at all. A stale
`recording`/`ending`/`detected` renders as `failed`, never as a live recording:
a pulsing indicator with no daemon behind it is worse than none (D10).

**The plugin reads this file with `FileView { watchChanges: true }` and calls the
CLI for actions. It never opens the socket** — one client of the wire protocol,
and a shell reload can never hold the daemon's socket open.

---

## 8. Notifications

Sent by the daemon through `omarchy-notification-send`. Glyph `󰻂` (U+F0EC2),
matching `bar/indicators/ScreenRecording.qml`.

The wrapper supports exactly **one** action, `--exec <program> [args…]` (the
underlying daemon supports more; the wrapper does not, and the PoC does not
bypass the wrapper). So each of the three notifications of spec §6.3 carries one
primary action, and the secondary actions live in the panel and on the keybind —
which is what "offered alongside rather than instead" already promised.

| When | Urgency | Title / body | Primary action (`--exec`) | Secondaries live in |
|---|---|---|---|---|
| Call detected | `normal`, `-t 30000` | "Meeting detected" / "<app> call, <time>. Click to record." | `munin start --from-detection` | — (declining is dismissing the notification; *Never for this meeting* is deferred, it needs the calendar series id at M9). |
| Streams gone 1 min (`warn_seconds`) | `normal`, `-t 60000` | "Meeting seems over" / "Recording stops in 1:00. Click to stop now." | `munin stop` | Panel and keybind: *Keep recording* (`munin start --resume` is not it — the session is still recording; the panel calls `munin event call-started` to cancel the grace period). |
| Auto-stopped | `critical` on failure, else `normal` | "Recording stopped" / "*n* min saved. Click to resume." | `munin start --resume` | Panel: *Open session*. |
| A capture track died mid-meeting | `critical` | "Recording problem" / "Your microphone (or The meeting audio) stopped recording. The rest continues. Click to stop." | `munin stop` | — (once per track per session; a dead microphone ends the session instead and notifies as *Auto-stopped*). |
| The app stream could not be bound | `critical` | "Recording all desktop audio" / "Could not record only <app>. All sound from this computer is included. Click to stop." | `munin stop` | — (spec §12: a sink-monitor track holds people who were never in the meeting, and telling the user afterwards through `app_source` is too late to stop it). |
| Ad-hoc start with no call to bind | `normal`, `-t 15000` | "No meeting found, recording desktop audio" / "All sound from this computer is included. Click to stop." | `munin stop` | — (§16.5: asked for by `[capture] adhoc_app_source`, not a fallback, but the same §12 point applies). |

D14 holds: auto-stop **always** notifies. `-r <id>` is used to replace the
previous Munin notification rather than stacking, with a stable id per session.

---

## 9. Config — `~/munin/config.toml`

Loaded by `munin.config` with `tomllib`. Every key has a default; a missing file
is not an error (`munin setup` writes one). `$MUNIN_HOME` overrides `[munin] home`
for tests and the installer. Unknown keys are preserved on read and reported by
`munin doctor`, never silently dropped.

```toml
[munin]
home = "~/munin"

[paths]
recordings   = "recordings"
inbox        = "inbox"
voices       = "voices"
log          = "munin.log"


[capture]
mic_source   = "default"     # "default" or a PipeWire node name / object.serial
bitrate_kbps = 24
channels     = 1
sample_rate  = 48000
min_free_mb  = 2048          # refuse to start below this (spec §11)

[detection]
enabled               = true
source                = "plugin"   # plugin | daemon | off
poll_seconds          = 3          # source = "daemon" only
grace_seconds         = 120        # auto-stop after the app stream is gone this long
warn_seconds          = 60         # warn at this point inside the grace period
resume_window_seconds = 600        # D15

[[detection.apps]]
app_id       = "teams-native"
label        = "Microsoft Teams"
client_name  = "Teams"
binary       = "teams-for-linux"   # measured: the native client reports "Chromium" as its name

[[detection.apps]]
app_id       = "teams-pwa"
label        = "Microsoft Teams"
window_class = "chrome-teams.microsoft.com__-Default"

[[detection.apps]]
app_id       = "teams-tab"
label        = "Microsoft Teams"
window_title_contains = "Microsoft Teams"

[transcribe]
backend  = "none"            # PoC: nothing transcribes
fallback = []

[idle]
inhibit = true
method  = "omarchy-stay-awake"   # §12

[notifications]
enabled = true
glyph   = "2"

[export]                            # §10, added 2026-09-18; goes with D25
enabled   = false
directory = "~/plaud-upload"        # absolute or ~-relative, unlike [paths]
format    = "opus"
```

`munin.config.load(path=None) -> Config` returns a frozen dataclass tree with
those defaults applied; `Config.app_rules` returns `list[AppRule]` in file order,
which is the match order.

---

## 10. Worker and backend seam

```python
# backends/base.py  (FROZEN)
@dataclass(frozen=True)
class TranscriptSegment:
    start: float            # seconds from the start of the session's audio timeline
    end: float
    speaker: str            # display name; "Ola Nordmann" for the mic track in later phases
    text: str

@dataclass(frozen=True)
class Transcript:
    segments: list[TranscriptSegment]
    language: str | None
    backend: str
    model: str | None = None
    words: list[dict] | None = None      # sidecar payload, opaque to the renderer

class BackendUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None    # .reason is written to pending_reason

class Backend(Protocol):
    name: str
    def available(self) -> bool: ...
    def transcribe(self, session: SessionRef) -> Transcript: ...
```

`backends/none.py` — `NoneBackend.name = "none"`, `available()` returns `False`,
`transcribe()` raises `BackendUnavailable("no transcription backend configured")`.
The worker catches `BackendUnavailable`, sets `state = "pending"` and
`pending_reason = exc.reason`, leaves the `inbox/` symlink in place, and moves on.
**That string is the PoC's visible end state and is part of this contract.**

`pipeline/render.py`:

```python
GAP_TEMPLATE = "[{ts}] --- recording resumed (gap {minutes} min) ---"

def format_timestamp(seconds: float) -> str        # "HH:MM:SS", hours unbounded (D7)
def gap_line(at_seconds: float, gap_seconds: float) -> str
def render_transcript(transcript: Transcript, *, gaps: Sequence[tuple[float, float]] = ()) -> str
```

- Line format, exactly: `[HH:MM:SS - HH:MM:SS] <display name>: <text>`.
- **Strictly monotonic** (spec §7.5): the renderer clamps `start` up to the
  previous `end`, and drops any segment left with `end <= start`, returning the
  drop count in the log rather than emitting a negative-duration line.
- The time axis is **captured-audio time**, not wall clock: segment *k*'s times
  are offset by the summed duration of segments 1…*k*−1. Wall-clock gaps between
  segments appear only as a gap line, so a 3-minute break does not silently
  become 3 minutes of missing audio in the timeline.
- The transcript is written into the session directory and nowhere else (D24).
  There is no second copy and no second filename convention.

---

## 11. CLI surface

`munin <command>`. `munin-rec` = `munin daemon`, `munin-work` = `munin worker`.

| Command | Args | Does |
|---|---|---|
| `start` | `[title]` `--resume` `--from-detection` | Start (or resume) a recording. |
| `stop` | — | Stop and finish the current recording. |
| `toggle` | `[title]` | Start if idle, stop if recording. The keybind target. |
| `status` | `--json` | Human line, or the §7.2 state view verbatim. |
| `list` | `--limit N` `--json` | Recent sessions with state and pending reason. |
| `mix` | `[session]` `--all` `--force` `--format opus\|mp3` `--to DIR` | Sum the two tracks into `mixed.opus` for a manual upload (spec §10, D25). Defaults to the most recent session; reads the spool directly, so it works with the daemon down. |
| `event` | `call-started\|call-ended` `--pid` `--app` `--app-id` `--handle` `--title` | Feed detection evidence from the plugin. |
| `doctor` | `--json` | Every check, one line each, per-platform table (§16.5). |
| `setup` | `--non-interactive`, `--write-default-config` (create the root and `config.toml`, then stop — what `install.sh` step 7 calls) | Create `~/munin/`, write `config.toml`, pick the mic. |
| `daemon` | `--foreground` (default under systemd) | Run `munin-rec`. |
| `worker` | `--once` `--interval N` | Run `munin-work`. |

Exit codes — every command, no exceptions:

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Generic runtime failure. |
| 2 | Usage error (bad arguments). |
| 3 | The daemon is not reachable (no socket, or it is already running when starting one). |
| 4 | Precondition failed: already recording, not recording, disk below `min_free_mb`. |
| 5 | `doctor` found at least one failing check. |

`status` when the daemon is down prints `state: unknown (daemon not running)` and
exits 3, so the keybind and the plugin can tell "idle" from "dead".

---

## 12. Idle inhibit

**Decision: `omarchy-toggle-idle stay-awake` on `recording`, `allow-idle` on
leaving it.** Verified path: Omarchy's idle service is a Quickshell plugin that
gates on the state file `~/.local/state/omarchy/indicators/stay-awake`, which is
exactly what `omarchy-toggle-idle` writes. Whether that service's
`IdleMonitor { respectInhibitors: true }` also honours a logind inhibitor created
by `systemd-inhibit --what=idle` could **not** be verified, so the PoC does not
depend on it.

**Reconciled at integration (§16):** the *mechanism* now lives in
`munin/desktop/`, a platform package in the same idiom as `capture/` and
`detect/` — `desktop/base.py` defines `IdleInhibitor`, `desktop/__init__.py`
picks by `sys.platform`, `desktop/linux.py` holds `omarchy-toggle-idle` and the
state-file path. `munin/daemon.py` names neither. An unsupported platform falls
back to `NullIdleInhibitor` rather than raising: a meeting that records without
inhibiting idle is still a recorded meeting. `tests/test_portability.py` fails
the build if the string comes back above the boundary.

Rules: the daemon records whether stay-awake was **already** set before it
touched it, in `state.json` (`"idle_was_inhibited": bool`), and on stop restores
that prior state rather than unconditionally calling `allow-idle` — so a user who
keeps the machine awake permanently does not lose that when a recording ends. If
the daemon dies mid-recording, the next start re-reads the file, so the state
self-heals rather than latching.

*Counterargument:* this writes a file owned by another tool's UI toggle, and the
bar's stay-awake indicator will visibly flip during a meeting. Accepted: a
visible, explainable toggle beats a silent lock screen mid-call, and the
alternative mechanism is unverified. Flagged as an open question (§15) for
verification on hardware.

---

## 13. Install layout

`systemd/munin.service`, per spec §9.1 and the verified `voxtype.service`
template:

```ini
[Unit]
Description=Munin meeting recorder daemon
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart=%h/.local/bin/munin-rec
Restart=on-failure
RestartSec=5
Environment=XDG_RUNTIME_DIR=%t

[Install]
WantedBy=graphical-session.target
```

`install.sh` (bash, `set -euo pipefail`, why-comment block before any code):

| # | Step | Touches |
|---|---|---|
| 1 | Check `python3 >= 3.12`, `ffmpeg`, `pw-record`, `pw-dump`, `hyprctl`, `omarchy`. Print what is missing and the `omarchy pkg add` line; never run `sudo` (non-interactive `sudo` fails on this machine). | nothing |
| 2 | `python3 -m venv ~/.local/share/munin/venv` and `venv/bin/pip install <repo>` | `~/.local/share/munin/venv` |
| 3 | Symlink `~/.local/bin/{munin,munin-rec,munin-work}` → the venv's scripts | `~/.local/bin` |
| 4 | Copy `plugin/local.munin/` → `~/.config/omarchy/plugins/local.munin/`, then `omarchy plugin validate` it, `omarchy-shell shell rescanPlugins`, `omarchy plugin enable local.munin --section center --after omarchy.weather` (falling back to `--section center` alone, because an anchor that is not on the bar fails the enable outright). The placement rides along with `enable`, which already places a bar-widget (at the default anchor of the section the manifest declares); a later `bar put` is too late. A widget already in `bar.layout` is never re-placed, so a user who moves it keeps the move across reinstalls. This step needs a running shell, so a failure here **warns and prints the command to run later** rather than aborting steps 5-7. | `~/.config/omarchy/plugins/` |
| 5 | Append the keybind to `~/.config/hypr/bindings.lua` | see below |
| 6 | Copy `systemd/munin.service` → `~/.config/systemd/user/`, **print** the `systemctl --user enable --now munin.service` line rather than running it (enablement is never committed) | `~/.config/systemd/user/` |
| 7 | `munin setup --non-interactive`: create `$MUNIN_HOME` and a default `config.toml` | `~/munin/` |

`--uninstall` reverses 2-6 and leaves `~/munin/` in place, printing its path.

**The keybind file is a symlink.** `~/.config/hypr/bindings.lua` resolves into the
dotfiles repo (`~/dev/dotfiles/omarchy/hypr/bindings.lua`). The installer must:
resolve the symlink, back up the **target** to
`$XDG_STATE_HOME/munin/bindings.lua.<timestamp>.bak`, append through the link so
the dotfiles repo sees the edit, and **say so on stdout** — naming the real path
and that it is a tracked file in another repo. Silently editing someone's
dotfiles repo is the one thing an installer must not do quietly, and that
includes leaving an untracked `.munin-bak` in it: the backup lives in munin's own
state directory, is written fresh whenever the block is added, and is removed by
`--uninstall` once the block is gone. `--uninstall` also reloads Hyprland, since
the same run deletes the binary the key points at.

```lua
o.bind("SUPER + SHIFT + R", "Record meeting", "munin toggle")
```

A plain command string is the verified form (`voxtype.lua` uses exactly this
shape); the spec's `{ exec = ... }` table form is **not** what the helper takes.
`SUPER + SHIFT + R` is confirmed unbound in the Omarchy defaults, so no
`hl.unbind` is emitted; the installer greps for an existing binding first and
skips with a message if one is found.

Plugin id is `local.munin` — it must not start with `omarchy.` (reserved) and it
passes `^[A-Za-z0-9][A-Za-z0-9._-]*$`. One folder declares
`kinds: ["service", "bar-widget"]` with both `entryPoints.service` and
`entryPoints.barWidget`. *Counterargument:* no first-party plugin combines the
two kinds in one folder; the validator's rules allow it and `omarchy plugin
validate` passes on this manifest. If the running shell turns out to reject it,
the deviation is to split into `local.munin` (service) and `local.munin-widget`
(bar-widget), which changes only the manifest files and the install step.

---

## 14. Ownership map

Six workstreams, branched from `poc` and merged back into it. **Each edits only
its own files.** A change needed in anyone else's file, or in a frozen file, is
reported as a deviation — not made.

| Workstream | Owns |
|---|---|
| **capture** | `src/munin/capture/linux.py`, `src/munin/detect/linux.py`, `tests/capture/` |
| **integration** | `src/munin/desktop/**`, `tests/test_integration_poc.py`, `tests/test_portability.py` (created when the six branches were merged; see §16) |
| **spool** | `src/munin/spool.py`, `src/munin/config.py`, `src/munin/paths.py`, `tests/spool/` |
| **daemon** | `src/munin/daemon.py`, `src/munin/cli.py`, `src/munin/ipc.py`, `src/munin/notify.py`, `systemd/`, `tests/daemon/` |
| **plugin** | `plugin/local.munin/**` |
| **worker** | `src/munin/worker.py`, `src/munin/pipeline/**`, `src/munin/backends/**`, `tests/worker/` |
| **install** | `install.sh`, `src/munin/doctor.py`, `src/munin/setup.py`, `tests/install/` |

**Frozen after this phase** (nobody edits them):
`pyproject.toml`, `.gitignore`, `src/munin/__init__.py`, `src/munin/capture/base.py`,
`src/munin/capture/__init__.py`, `src/munin/detect/base.py`,
`src/munin/detect/__init__.py`, `src/munin/backends/base.py`,
`src/munin/backends/__init__.py`,
`tests/conftest.py`, and this document.

`src/munin/pipeline/__init__.py` was on that list and is **no longer**: the
worker workstream was asked for `run(session, backend)` as the single entry the
worker calls, which could not be added without editing it. The addition is
purely additive and nothing else imports the module. Recorded in §16.

**Branch naming.** All six workstreams were told to use `poc/<name>` and all six
found that git refuses `refs/heads/poc/<name>` while the branch `poc` exists — a
ref cannot be both a file and a directory. The branches are `poc-<name>`.

`backends/none.py` and `pipeline/render.py` are owned by **worker** but their
public signatures are fixed by §10.

---

## 15. Open questions this PoC does not close

Carried forward, not resolved. None may be quietly assumed away.

1. **Does the Omarchy idle service honour a logind inhibitor?** Unverified
   (§12). The PoC uses the stay-awake state file instead. Needs a real 150-second
   idle wait on hardware.
2. **Does one plugin folder with `kinds: ["service", "bar-widget"]` load in the
   running shell?** The validator accepts it; no first-party plugin does it
   (§13).
3. **Does `voxtype`'s `pause_media` pause a Teams browser tab?** Unchanged from
   the plan's §8. Needs a live call.
4. **Is the mic's native 16 kHz mono right for the pipeline, or should capture
   stay at 48 kHz?** The default source reports S16LE/16000/mono natively;
   the PoC captures at 48 kHz because diarization models expect 16 kHz *resampled
   from a wider band* rather than a 16 kHz microphone path, and because the app
   track is 48 kHz regardless. Unmeasured either way.
5. **Retention defaults**, **OQ1 (where the authoritative transcript lives)** and
   **OQ11 (deleting a voice profile)** remain open from the spec's §14.

The PoC records identifiable people. Nothing here changes the compliance posture
of spec §12: audio stays on this machine, recording is confirmed (D4) and visible
(D10), `history[]` in `session.json` is the audit trail, and no retention pass
exists yet — so the PoC accumulates audio indefinitely until §15.5 is decided.

---

## 16. Integration: what the merge reconciled

The six workstreams built in parallel against §0–§15 and merged into `poc` with
**no file conflicts** — the ownership map held. What follows is every deviation
they reported, and what was decided. Where this section and §0–§15 disagree,
this section is later and wins.

### 16.1 Contract changed, code kept

| # | Deviation | Decision |
|---|---|---|
| 1 | Capture can fall back from the application's own stream to the desktop-sink monitor, and `session.json` could not express it | **Contract changed.** `segments[].app_source` added (§3). Compliance-relevant, not cosmetic: a sink-monitor track holds audio from people who were never in the meeting. The daemon reads it through the frozen `describe()` seam, so no platform file changed. |
| 2 | `pipeline/__init__.py` was frozen but the worker was asked for `run()` | **Contract changed** (§14). Additive, nothing else imports it. |
| 3 | `munin setup --write-default-config`, called by `install.sh` step 7, was not in the CLI table | **Contract changed** (§11). The flag now exists and is wired to `setup.main(write_config_only=True)`. |
| 4 | Branches are `poc-<name>`, not `poc/<name>` | **Contract changed** (§14). Git will not allow the slash form while `poc` exists. |
| 5 | `install.sh` keybind backup is `<target>.munin-bak`, not `<target>.munin-backup-<ISO date>` | **Code kept.** It is never overwritten once written, so the pre-Munin original cannot be destroyed by a second install. A dated name would accumulate copies of a file that is already in another git repository. |
| 6 | `install.sh` step numbering differs from the §13 table | **Code kept.** Same seven steps and same effects; only the numbers in the progress output differ. |
| 7 | The keybind is `o.bind(..., "munin toggle")`, not the `{ exec = ... }` table form | **Code kept** — confirmed against `/usr/share/omarchy/default/hypr/helpers.lua`: a table is only read for `launch`/`webapp`/`tui`/`omarchy` keys, so the table form would bind a no-op. |
| 8 | `notify.ending_soon` says "Stops by itself in 1:00", not "at 2:00" | **Code kept.** The frozen parameter is `seconds_left`; rendering remaining time as an absolute mark would be wrong. |
| 9 | `state.json` is also refreshed on a 30 s heartbeat | **Code kept.** Nothing that changes second-by-second is written — the plugin still computes elapsed from `started_at` — and it is how a reader tells a live daemon from a stale file. |
| 10 | `min_free_mb` is MiB (1024²), not 10⁶ bytes | **Code kept**, and recorded here because the two differ by 5%. |

### 16.2 Code changed at integration

| # | Problem found | Fix |
|---|---|---|
| 1 | **The audit trail grew without bound.** `Worker.drain()` re-processed every `pending` session on every sweep, walking `pending → transcribing → pending` and appending **two rows to `history[]` each time**. `munin-work` runs on a 5 s timer and the PoC's *designed* end state is `pending`, so every session would have grown `session.json` forever (~34 000 rows/day). `history[]` is the audit trail and is never trimmed (§3, spec §12). | `drain()` skips a `pending` session that already carries a `pending_reason` while the backend reports itself unavailable, and retries the moment one becomes available. Verified: 50 sweeps leave the history at 5 rows. |
| 2 | `Daemon` held `omarchy-toggle-idle` and the Omarchy state-file path, above the portability boundary | Extracted to `munin/desktop/` (§12), enforced by `tests/test_portability.py`. |
| 3 | `ipc.socket_path()`, `daemon._state_file()` and `daemon.load_config()` carried fallbacks for "the spool workstream has not landed yet", which silently shadowed the real modules | Removed. `munin.paths` and `munin.config` are now the only source, and an unset `XDG_RUNTIME_DIR` surfaces as `DaemonUnreachable` (exit 3) instead of a `RuntimeError` traceback. |
| 4 | `cli.py` swallowed `NotImplementedError` from `doctor`, `setup` and `worker` and printed "not implemented in this build" — which, once those modules were real, would mislabel a genuine `NotImplementedError` from deeper in the stack | Removed; the CLI imports the real modules. |
| 5 | `setup.py` carried its own copy of `DEFAULT_CONFIG_TOML` | Deleted; `munin.config` is canonical. A test now asserts every comment line of the template survives a rewrite, so the two cannot drift. |
| 6 | `Worker.drain()` returned a transition count while its docstring promised a session count | Returns distinct sessions. |

### 16.3 Still open after integration

1. **The daemon cannot tell that a meeting ended from the capturer.** Measured
   on this machine: `pw-record` does not exit when its bound node disappears,
   and ffmpeg's Ogg-Opus muxer writes nothing until close — so neither process
   state nor file growth signals the end of a call. The honest signal is
   `PipewireCapturer.app_stream_present()` (a `pw-dump` query) or the plugin's
   PipeWire watch. **A daemon that polls `health()` alone will record until the
   user stops it.**
2. **No real Microsoft Teams call has been detected.** All three shapes
   (native, PWA, browser tab) are tested against fixtures built from real
   PipeWire and Hyprland object shapes with synthetic identities, but the exact
   `application.name`, window class and tab title come from spec §6.3, not from
   a measurement. If a shape fails, it is a row in `[[detection.apps]]`, not
   code (D13).
3. **Nothing has been installed.** `install.sh` has only been run `--dry-run`
   and against a synthetic machine, so §15.2 (does one folder declaring
   `kinds: ["service","bar-widget"]` load?), `omarchy bar put --before`, the
   `hyprctl reload` of the appended keybind and the systemd unit are all
   unverified on the real shell.
4. **The microphone has never recorded speech.** Live capture was verified in a
   silent room: the tracks are valid Opus, the app tone is ~78 dB down in the
   mic track, but that the mic *picks up a voice* is untested.
5. **Idle inhibition is unverified on hardware** (§15.1), and the three
   notifications have never been rendered on a real screen — only their argv is
   pinned by test.
6. **Retention is still undecided** (§15.5). The PoC accumulates audio
   indefinitely, and `app_source` now records that some of that audio may be
   wider than the meeting.


---

### 16.4 Review pass on the merged PoC

A full review of `poc` after integration. Everything below is a code change;
where it also moved a contract, the relevant section above has been updated in
place and this table says which.

**Daemon**

| # | Problem | Fix |
|---|---|---|
| 1 | `Spool.recover_for_daemon()` had **no production caller**. A daemon killed by SIGKILL, an OOM kill or a power cut left its session at `recording` forever: the worker only ever looks at `captured`/`pending`, so the audio was stranded outside the pipeline, and `state.json` kept telling the bar a recording was live. | `Daemon.run()` calls it before the first `state.json` write, guarded, the way `Worker.main()` already called `recover()`. `_refresh_idle_state()` additionally refuses to publish `recording`/`ending` while `self.session is None`, mapping to `captured` or `failed` by whether audio exists — defence for debris recovery could not reach. |
| 2 | With `detection.source = "daemon"`, an **ad-hoc recording auto-stopped after `grace_seconds`**: the poller's else branch ended a call for a session that had no detected pid at all. | The else branch acts only on a watched pid, and `on_call_ended` refuses a recording whose session has no detected application. |
| 3 | `_playback_handle` was cleared only on an auto-stop, so a handle outlived the call it named. The next recording bound a dead node, and `pw-record` answers a dead `--target` with the **desktop-sink monitor** — the whole machine's audio, from people who saw no prompt (§3, `app_source`). | `_forget_detected()` clears the handle on every path where the call stops being live, including `on_call_ended` outside a recording; `_targets()` refuses a handle without a live detection. A manual stop during a *live* call keeps it, so a resume rebinds the same stream. |
| 4 | A daemon/worker race on `session.json` raised `StateError` that neither side caught: it **killed `munin-work`** (which has no unit to restart it) and turned `munin start --resume` into an internal error. | `Worker.drain()` skips a session that lost the race and re-reads it next sweep; `Daemon.handle_start()` falls through to a fresh session, which is what a closed resume window does anyway. |
| 5 | `shutdown()` called `inhibit_idle(False)` even when the daemon had never recorded, so a `systemctl --user restart` or a logout **switched off a stay-awake the user had set by hand**. | The daemon releases only a hold it took (`_idle_held`). §12's rule is unchanged; it is now actually honoured on the non-recording path. |
| 6 | `systemd/munin.service` had no `KillMode`, so the default control-group kill SIGTERMed `pw-record` and `ffmpeg` alongside the daemon on every stop and every logout — defeating `capture/linux.py`'s deliberate stop ordering and risking an Ogg file with no trailer. | `KillMode=mixed` and `TimeoutStopSec=60`. |
| 7 | Nothing polled `Capturer.failed_tracks()`. An encoder OOM-killed five minutes into an hour was invisible until `stop`, and ffmpeg writes nothing until close — so the whole hour was gone. The sink-monitor fallback was equally silent. | `tick()` polls capture health every 5 s: a dead microphone finishes the session and notifies (D14), a dead app track notifies once and keeps the microphone. The fallback to a wider capture now notifies at `start`, while the user can still stop it. Two notifications added (§8). |

**Plugin**

| # | Problem | Fix |
|---|---|---|
| 8 | `callEventArgv` never sent `--handle`, so on the **default configuration** a plugin-detected call recorded a silent app track: mic audio and nothing else, unrecoverable. It also sent the rule id in `--app` and never sent `--app-id`, so every recording's provenance read `teams-pwa / unknown`. | Both fixed; §6 and §11 updated. `playback_handle` is `object.serial` only — the `|| n.id` fallback is gone, since a node id is a different namespace from a serial. |
| 9 | `finishResolve` reported a call even when `identify()` returned null, so **condition 1 alone raised the D4 prompt**: a Discord call, a Signal call or any WebRTC page. | The event is sent only for an identified call, matching the daemon's own poller. |
| 10 | `ownersFromNodes` classified any non-sink stream as a capture stream, **video included**, so a screen-share plus a playing tab satisfied condition 1. | Nodes with no `audio` interface are skipped, as `panels/audio/Panel.qml` already does. |
| 11 | `Model.identify` was case-sensitive and had no `binary` stage, unlike `detect/base.identify`, and the plugin never saw the user's config — so D13 did not hold on the default path. | Casefolded, `binary` stage added, and the daemon now publishes the resolved table as `detection_rules` in `state.json` (§7.2). |
| 12 | `captured` never expired and `barSpins("captured")` was true, so the PoC's **designed end state** left an infinite rotation animation repainting the bar forever, claiming work nothing was doing. | Only `transcribing` spins. |
| 13 | `updated_at` was parsed and never used, so a killed daemon left the bar showing a live, counting recording indefinitely — and the plugin kept spawning `munin event` at a dead socket. | `Model.isStale` (90 s) gates `daemonRunning` and `effectiveState`; §7.2 documents the rule. |
| 14 | The panel offered no **Keep recording** during `ending`, the one place §8 says that action must live. | A second panel button and the `k` key, calling `munin event call-started`; the panel also shows the grace deadline. |

**Install, CLI and doctor**

| # | Problem | Fix |
|---|---|---|
| 15 | `omarchy plugin enable` already *places* a bar-widget, at the section's default anchor (after `omarchy.tray`), so the following `bar put --before` was too late and the widget landed on the wrong side. The test shim hid it by making `enable` a no-op. | The placement rides along with `enable`; the shim now places on enable the way the real registry does. §13 step 4 updated. |
| 16 | A malformed `config.toml` made **`munin doctor` and `munin setup` refuse to run** — the two commands a user reaches for because the config is broken. | `cli._config()` falls back to defaults and warns; `doctor.check_config` reports the parse error as a failing check (exit 5). |
| 17 | The keybinding backup was written **inside the dotfiles repository**, never refreshed, and never removed by `--uninstall`. | It goes to `$XDG_STATE_HOME/munin/bindings.lua.<timestamp>.bak`, fresh each time the block is added, and both it and the legacy in-place backup are removed on uninstall. §13 updated. |
| 18 | `--uninstall` removed the keybind block but never reloaded Hyprland, leaving `SUPER + SHIFT + R` bound to a binary the same run deleted. | `uninstall_keybind` reloads. |
| 19 | `munin doctor` reported `detection: ok — no call in progress` when the detector **could not reach PipeWire at all**: `scan()` swallows a `pw-dump` failure by design, and an empty scan and a dead probe are the same empty list. Detection is the whole basis of D4. | The check reads `describe()["pw_error"]` and fails the row with the `pw-dump` error. |
| 20 | Step 4 was fatal while steps 3 and 6 only warned, so an install started **without a reachable Omarchy shell** (ssh, a bare TTY, a first boot) aborted with the venv and plugin in place but no keybind, no unit and no data root. | Step 4 warns, prints the command to run once the shell is up, and steps 5-7 complete. |

None of D1-D21 was reopened, and no frozen file was edited.

### 16.5 Live run, and the ad-hoc app track

The merged PoC was run end to end on this machine from the repository venv
against a scratch data root (no install): `munin-rec` on its real socket, then
`munin start`, `status --json`, `stop`, `munin-work --once`, `start --resume`,
`event call-started` / `call-ended`, `toggle`, and a `SIGTERM` to the daemon
mid-recording. Everything behaved as §4, §7 and §11 say. One gap showed up that
no test could have: **an ad-hoc `munin start` recorded a silent app track**
(`app_source: "silent"`, RMS −∞ dB), because §5 mapped "no `CaptureTarget` for
the app" to silence and an ad-hoc start has no detected call to hand over. That
is the keybind-during-a-Teams-tab case, which is the PoC's main use, so it was
changed rather than documented:

1. `handle_start` on an ad-hoc start runs **one detector scan** first. An
   identified call is adopted (its stream is bound, its identity goes into
   `session.json.app`, `source` stays `adhoc`); failing that, a *single*
   unidentified live call is adopted; two or more is ambiguous and none is.
2. With nothing to adopt, the app target is the new platform-neutral
   `CaptureTarget(handle=SYSTEM_OUTPUT_HANDLE)` from `capture/base.py` — spec
   §16.2's "fallback" row on every platform. On Linux that is the sink monitor
   from the outset (no stream lookup, no probe, no warning). `session.json`
   records `app_source: "sink-monitor"`, and the daemon notifies (§8) because
   the mix may hold audio from outside the meeting (spec §12).
3. `[capture] adhoc_app_source = "system-output" | "silent"` (§9) keeps the old
   behaviour available.

Measured after the change, tone playing through `pw-play` and a quiet room:
app track −21.1 dB RMS, mic track −57.1 dB RMS, both ~8.7 s for an 8 s
recording; before it the app track was −∞ dB. Counterargument recorded: the
output mix is exactly the material D2 wanted to keep out of the meeting track
(music, notification sounds), and adopting a single unidentified call trusts
condition 1 alone. Accepted because a meeting track with nothing on it cannot
be repaired afterwards, the session says which kind of track it holds, and the
user is told while they can still stop.

### 16.6 A private sink per recording, because Chromium has five streams

**The measurement that forced this.** On 2026-09-15, during a real Microsoft
Teams call in the Electron client (Chromium), the process held **five**
identical `Stream/Output/Audio` nodes named "Playback" at the same time, and the
node the detector had seen at call start was **gone 12 s later**. The app track
therefore fell back to the desktop-sink monitor: every application's audio,
which is the one outcome spec §6.1 exists to prevent ("bound to the specific
sink-input rather than the whole system output, so music and notification sounds
stay out of the mix"). Binding by stream cannot work for a Chromium-based
application, and all three Teams shapes on this machine are Chromium.

**The design.** Do not follow the application's streams; give the recording a
sink and move the application onto it. New `app_source` value `"process-sink"`,
chosen whenever the app `CaptureTarget` carries a `pid` (both the detected and
the adopted-call paths do). Per segment:

1. `pactl load-module module-null-sink sink_name=munin-app-<pid> sink_properties=device.description=Munin_meeting_audio`.
2. `pactl load-module module-loopback source=munin-app-<pid>.monitor latency_msec=30 source_dont_move=true` — so the user still hears the call. Deliberately **not** pinned with `sink=`: measured, an unpinned loopback follows a default-sink change (its sink-input moved to a new default and back within 1.5 s), which is what switching to headphones mid-meeting needs. `source_dont_move` pins the other end, because a loopback that wandered off our monitor would put the meeting into the speakers twice and the recording nowhere.
3. Every output stream of the process is moved with `pactl move-sink-input <index> munin-app-<pid>`, remembering the sink it came from. A process's streams are found by pid: `application.process.id` on the sink-input when present, otherwise through `client` → the Client object's `application.process.id`, otherwise its `pipewire.sec.pid`. All three are needed — measured, a *native* PipeWire client (`pw-play`) publishes no `application.process.id` on the stream at all, and for a *PulseAudio* client `pipewire.sec.pid` is the pipewire-pulse daemon (1125 here), not the application. A bounded two-hop parent walk also claims a child process's streams, because Chromium's audio process is a child of the window process.
4. A 1.5 s watcher thread keeps moving streams the application creates later. The daemon's health tick is 5 s, which would leave up to five seconds of a new Chromium stream going to the speakers and not into the recording. Measured in the live check: one 8 s Chromium capture needed **two** moves for one stream.
5. `pw-record --target munin-app-<pid> -P "{ stream.capture.sink = true, node.dont-reconnect = true }"` records the sink's monitor.

**Teardown, and the order is the point.** Streams back to the sink they came
from (or, if that sink is gone, to the current default) → unload the loopback →
unload the null sink. Unloading the sink first would leave the application
playing into something that no longer reaches the speakers. Every step tolerates
failure, logs it, and reports it in `warnings` and `describe()`; a failure to
clean up never fails a recording.

**Crash recovery.** `munin-rec` SIGKILLed mid-meeting never reaches `stop()`.
`munin.capture.recover_capture()` runs at daemon startup, finds `munin-app-*`
null sinks and their loopbacks in `pactl list modules short`, moves any stream
still sitting on one back to the default sink, then unloads loopbacks before
sinks. Verified live: after a SIGKILL the tone player was stranded on
`munin-app-<pid>`; recovery moved it back and unloaded both modules.

**Fallbacks.** Missing `pactl`, a failed `load-module`, or a recorder that dies
on the private sink drops to `stream` (when the handle still names a live node),
then `sink-monitor`, then `silent` — with the existing warnings and the
"Recording all desktop audio" notification. Isolation is an improvement, never a
precondition for recording.

**`app_stream_present()`** in this mode answers whether the *process* still holds
any output stream, not whether one node survives, and `None` when PipeWire
cannot be asked. A per-node answer would report the meeting over every time
Chromium recycled a stream.

**Measured on this machine, 2026-09-15** (440 Hz "meeting", 880 Hz "music"
playing at the same time; tone magnitudes by Goertzel, not a filter bank):

| Run | App track 440 Hz | App track 880 Hz | Rejection |
|---|---|---|---|
| `pw-play` as the meeting, capturer driven directly | −21.1 dB | −100.7 dB | **79.6 dB** |
| The same through `munin-rec` (`munin event` + `munin start`) | −21.1 dB | −98.8 dB | **77.8 dB** |
| A real Chromium tab as the meeting, `pw-play` as the music | −15.6 dB | −108.3 dB | **92.6 dB** |

The microphone track in each run was −57 dB of quiet room with neither tone on
it. `pactl move-sink-input` did not refuse the Chromium stream.

**Counterargument, and it is a real one.** This reroutes live audio the user is
listening to and puts a 30 ms loopback in the path. A bug here is *audible*: at
worst the meeting goes silent in the user's ears, which is worse than a widened
recording and worse than no recording at all. Three things are the answer, and
none of them is "it should be fine": the teardown order never leaves the
application on a sink that does not reach the speakers; recovery cleans up a
killed daemon's modules at the next startup; and PipeWire itself moves an
orphaned stream to the default sink when the private sink is unloaded (measured
— the failure mode is a quiet track, never silent speakers). Two smaller costs
are accepted: 30 ms of added latency on the user's own monitoring, and a sink
named `munin-app-<pid>` visible in every volume mixer on the machine while a
meeting is being recorded. Its description is deliberately the generic "Munin
meeting audio" and never the meeting's title.

**Not verified.** A real Teams call *through* this mode (the measurement above
used a Chromium tab playing a tone, not a call), whether five concurrent
Chromium streams all move cleanly under load, and the behaviour when the user
changes the default sink while a recording is running (the loopback was measured
following a default-sink change, but not during a capture).


### 16.7 Splitting a session that holds two meetings (D26)

**The case.** The user walks out of one meeting and into the next without
stopping the recording. Nothing failed; one session now holds two meetings,
which §12 of the spec makes a compliance problem rather than an inconvenience —
one transcript over two sets of participants, one retention clock, one access
boundary — and which diarization makes a quality problem, because it clusters
speakers over whatever it is handed. Nothing in §0–§15 covered it: a
`call-started` arriving while the daemon was recording was ignored, and inside
the grace period it was read as the first meeting's stream returning, which is
precisely what glues the two together.

Spec §6.4 describes the behaviour. This section is the contract change.

**§4, the state machine.** One state and three transitions are added, and a
third writer, `munin` (the CLI), joins `munin-rec` and `munin-work`:

| From | To | Written by | When |
|---|---|---|---|
| `captured`/`pending` | `split` | `munin` | `munin split` has cut this session into two new sessions. Terminal. |
| — | `captured` | `munin` | A session *derived* from another one: one half of a split, whose audio was finished before it existed. |

`split` is terminal — nothing transitions out of it — and it is outside the set
the worker sweeps (`captured`, `pending`, `transcribing`), which together are
what stop a session that has been cut in two from also being transcribed as one.
The ownership rule of §4 is otherwise unchanged: the daemon still never writes
`pending`, `transcribing` or `done`, and the worker still never writes
`recording`, `ending` or `captured` on a session it is capturing. The narrow
exception is that a session nobody is capturing — a derived half at its birth,
a parent being retired — is written by `munin`.

**§3, `session.json`.** Two optional keys, both `null` on every session that was
neither split nor derived. Schema version stays `1`: a reader of this build that
meets them keeps them, and an older reader round-trips them through `extra`.

| Field | Rule |
|---|---|
| `split_from` | `{"session": "<parent id>", "kind": "offline"\|"live", "part": 1\|2, "offset_seconds": float\|null}` on a session cut out of another. `offset_seconds` is the cut point on the parent's audio timeline, `null` for a live split, which cut nothing. |
| `split_into` | `["<id>", …]`, the sessions this one was cut into, oldest first. Two ids on a `split` parent; one on a `captured` session that was closed by a live split, where it reads "continued as". |

Everything else on a derived half is **inherited, not recomputed**: `source`,
`app`, `platform`, `capture_method`, `host` and `munin_version` are facts about
how the audio was made, and cutting it does not make them less true. `source`
therefore still only ever holds `adhoc` or `detected`. `segments[]` is
renumbered from 1 and contiguous, with filenames from
`munin.capture.base.segment_filenames(index)` as §3 requires; `checksums`,
`started_at`, `stopped_at` and `duration_seconds` are recomputed, because those
are what the cut actually changes. `app_source` is carried across per segment —
a sink-monitor track stays a sink-monitor track in both halves.

**The parent is never modified in place.** Its audio and its `checksums` are the
record of what the machine captured. The halves are new sessions; the parent
keeps every byte and leaves the queue. The cost is one duplicated copy of the
audio (a two-hour meeting at 24 kbps is ~43 MB) until retention removes one,
which is §15.5's open decision and not a new one.

**§7.1, IPC.** One command:

| `cmd` | Args | `data` on success |
|---|---|---|
| `split` | `title?: str` | the `start` payload, plus `"closed": "<abs path>"`, `"closed_id": str`, `"closed_duration_seconds": float` |

It finishes the running session exactly as `stop` does and starts the next one
in the same call, so the two cannot be separated by a failure between them.
`not_recording` when nothing is being captured, and one new error code:
`too_soon`, when the recording is under five seconds old. That is what a
notification clicked twice looks like from here, and without it the second click
leaves a session of a second or two behind, with a directory, a record and an
inbox entry of its own. The CLI maps it to exit 4, with the other preconditions.

If the second recording cannot be started (no space, capture failed), the error
says so *and* names the session that was saved: "split failed" would otherwise
read as "the last hour is gone", when in fact the first meeting is captured and
queued exactly as `stop` would have left it.

**§8, notifications.** One row:

| When | Urgency | Title / body | Primary action (`--exec`) | Secondaries live in |
|---|---|---|---|---|
| A different call goes live while recording | `normal`, `-t 60000` | "New meeting detected" / "&lt;app&gt; call, &lt;time&gt;, while recording. Click to split here." | `munin split --now` | Panel: *Split here* while `recording`, and the `SUPER + CTRL + SHIFT + R` keybind. There is no *Same meeting* button: a split is start-shaped, so doing nothing already means "no" (spec §6.3), and a button that does what ignoring the prompt does is one more thing to explain. |

Sent once per distinct call per session, not once per detection poll. A call is
distinct when `(pid, window_title)` differs from the recording session's; a
`call-started` carrying no pid never qualifies, which is what keeps the panel's
*Keep recording* button — a bare `munin event call-started` — from reading as a
second meeting. An ad-hoc session with no identified application is never
offered a split, for the same reason `on_call_ended` declines the mirror case:
there is nothing to compare against, and the call is as likely to be this
meeting joined late.

**The plugin.** Two changes, both in `Model.js` (the plugin still speaks only
the CLI, D20): the secondary panel button is *Split here* while `recording`
(`munin split --now`), keeping *Keep recording* during `ending`, where it is the
contracted action; and a session in `split` renders with the settled glyph and
the line "split in two", not the waiting glyph, which would read as a transcript
the worker still owes. `split` also joins `SESSION_STATES`, which is the list
that actually decides: a session state missing from it arrives at the widget as
`unknown` and is painted as a *failure* — the same trap `pending` fell into in
the PoC (§16.4).

`munin mix --all` skips a `split` parent for the same reason the worker does: the
halves are the meetings, and mixing all three would put the merged recording in
the upload folder beside them.

**§11, the CLI.** One row:

| Command | Args | Does |
|---|---|---|
| `split` | `[session] [at]` `--now` `--clock HH:MM` `--title TEXT` `--title-first TEXT` | Cut a recording that holds two meetings into two sessions (spec §6.4, D26). `--now` splits the running recording through the socket; otherwise the cut is made in the file, reading the spool directly like `mix`, so it works with the daemon down. Defaults to the most recent session, so `munin split 27:32` is the common case. |

Exit codes are §11's, unchanged: 2 for a cut point that is not a time or a
session that does not exist, 4 for a session in a state that cannot be split, a
cut point outside the audio, or one already split, 1 for a cut that was
attempted and failed, 3 only for `--now` with no daemon.

**The audio cut.** `ffmpeg -ss <start> -i <track> [-t <length>] -c copy`, one
run per track per piece, into `<name>.part` and renamed on success. A stream
copy, so nothing is re-encoded and a two-hour session cuts in about a second;
the consequence is that a half's `duration_seconds` is the *intended* length and
the file may differ by up to one Opus packet (20 ms). Measured against the
synthetic fixture, the halves land within 0.1 s of their planned lengths. The
cut point counts captured audio, not wall clock, so a resumed session's gap
takes no time — the same axis §10 gives the renderer and the mix. `--clock`
converts a time of day into that axis using the segments' own timestamps.

**Two axes meet at every segment boundary, and they disagree.** A segment's
`started_at` is stamped before the encoders are up, so its wall-clock span runs
a fraction of a second longer than its `duration_seconds`. Every piece therefore
carries its audio length explicitly rather than deriving it from its two
timestamps — the halves have to sum to the parent on the axis the transcript
uses — and a cut that lands within 0.25 s of a segment's edge is snapped to the
edge. Without the snap, ordinary input (a `--clock` on a segment's start minute,
an offset typed from a duration `munin list` rounded to the second) asks ffmpeg
for a piece of a millisecond, which writes a zero-byte file and fails the whole
split. A cut that snaps onto the recording's own start or end is refused
instead: a half with no audio in it is not a meeting.

**A session recovered after a crash is measured before it is cut.** Recovery
(spec §11) writes no `duration_seconds` and sets `stopped_at` to the moment the
*next* daemon started, so its wall-clock span can be hours wider than its audio.
Segment lengths are never inferred from the clock; a missing one is measured
with `ffprobe` and used in memory only, and the parent's record is left exactly
as the daemon wrote it.

**Ids.** Both halves are named by §2's rules, from their own start minute and
title. Part 1 keeps the parent's title and starts in the parent's minute, so the
id it wants is the one the parent holds and it takes the `-2` collision suffix —
an id artefact, not a part number, which is why `munin split` prints `part 1` and
`part 2` in front of the ids it made.

**A half can be split again.** A half is born `captured`, which is a splittable
state, so a recording that turned out to hold three meetings is cut twice. The
second cut treats the half as any other parent: the half moves to `split`, two
new sessions are derived from it, and `split_from` points at the half, not at
the original — the provenance is a chain, and following it back to the capture
is two hops instead of one. Nothing special is done to support this and nothing
prevents it; it falls out of halves being ordinary sessions, which is the point
of deriving them rather than rewriting the parent. The cost is the audio: three
meetings cut twice leave the original, the first cut's two halves and the second
cut's two, and the retention question (§15.5) gets correspondingly larger.

**Ordering, so an interrupted split cannot lose a meeting.** Each half's
*directory* is filled and checksummed first; then the parent is moved to
`split`; then each half's `session.json` is written and linked into `inbox/`,
and the parent is unlinked. **A directory without a `session.json` is not a
session** to anything that reads this store — `iter_sessions` skips it — so
until the parent is claimed there is nothing for `munin-work` to find and
nothing a rollback can delete out from under it. `Spool.derive` therefore
returns an *unsaved* session, which is the one place in the spool where that is
true and is why it is spelled out here. A failure anywhere before the parent
moves leaves it `captured` (or `pending`) and queued, and removes the
half-written directories. A second `munin split` on a parent already in `split`
is refused and names the two sessions it produced.

The parent's move is a compare-and-swap against the state *on disk*, not the one
the CLI read when it started: `munin-work` sweeps every 5 s and `munin start
--resume` can take a captured session back to `recording` (§4, D15), and a cut
of a long meeting takes about a second. Losing that race removes the halves and
leaves the parent exactly as the other process left it — two halves queued *and*
a resumed parent would be the original problem twice over. What remains
unprotected is only a failure *after* the parent has moved (a disk that fills
while the two small JSON files are written): the audio and the parent's record
survive, and the halves are directories on disk waiting for a `session.json`
that has to be written by hand.

The daemon's own provenance write takes the closed session's lock and re-reads
inside it, for the same reason: the worker may have claimed it (`captured ->
pending`) in the milliseconds since it was captured, and writing back a snapshot
from before that would roll its state and its history row off the record.

**Counterargument, and it is a real one.** This makes the CLI a writer of
session state, which §4 had kept to two daemons, and it doubles the audio on
disk for every split. The alternative considered was cutting the parent in place
— cheaper, and one record instead of three — and it was rejected because the
`checksums` field then describes a file the user no longer has, which is the
only integrity claim the session store makes (§12). The narrower alternative, a
`munin-work`-owned split, was rejected because the worker has no user in front
of it and a split is a judgement about what a meeting *was*.

**Not verified.** A real meeting application renaming its own window mid-call
(the false-positive path: synthetic evidence only, spec §14 item 13); a live
split during an actual Teams call, where the sub-second gap between closing one
capture and opening the next meets a real private sink and its loopback (§16.6);
and the interaction with `calendar_event_id` at M9, where both halves would
inherit one id and §11's duplicate-capture rule would read them as duplicates
(spec §14 item 14).
