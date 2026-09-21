# Munin: implementation plan

**Status:** Draft, pending approval
**Date:** 2026-09-14
**Revised:** 2026-09-14 — Teams-first scope, Omarchy plugin, voice register,
transcription backends, install flow
**Owner:** Simen Sollie
**Implements:** [`../specs/2026-09-14-meeting-recorder-design.md`](../specs/2026-09-14-meeting-recorder-design.md)
**Sketches:** [`../../design/munin-plugin-sketches.html`](../../design/munin-plugin-sketches.html)

> Public copy, same rules as the spec. Customer names, colleague names and
> internal project references are replaced with generic descriptors.

---

## 1. What this plan is

The spec settles *what* Munin is. This plan sequences the build.

Guiding order: **a session that is captured but not yet transcribed is
recoverable; a meeting that was never captured is gone.** Capture ships before
the pipeline, and the pipeline before the polish.

Three of the spec's open questions were answered by reading the machine, which
removed the largest unknown: Omarchy 4 has a real plugin system, notification
actions work, and PipeWire is reachable from plugin QML. The plugin is therefore
ordinary work, not a research project.

## 2. Stack

| Choice | Value | Why |
|---|---|---|
| Plugin | QML, in `~/.config/omarchy/plugins/local.munin/` | The only option; `qs.Ui` supplies the panel, PipeWire and IPC |
| Daemon, worker, CLI | Python 3.12+, `uv`, console scripts | `faster-whisper`, `pyannote.audio`, `speechbrain` and `mlx-whisper` are all Python |
| Admin surface | Python, stdlib HTTP + server-rendered HTML, loopback only | A register and a backend list are tables; no build step, no framework to maintain |
| Audio I/O | `pw-dump`/`pw-cli` in the daemon, `ffmpeg` subprocess for encode | The documented interface; Python PipeWire bindings are thin and unstable |
| Config | TOML at `~/munin/config.toml` | One file, hand-editable, matches `glossary.toml` |
| Tests | `pytest`, committed synthetic two-track fixtures | §13 of the spec is mostly fixture-driven |

Deviation from the house stack (.NET / Angular / Next.js), justified by the ML
dependency surface. Counterargument: it adds a third runtime to maintain on
personal infrastructure and nothing else in the estate is Python, so operational
familiarity is lower. Accepted because wrapping four Python libraries in
subprocesses would remove the benefit without removing the dependency.

## 3. Layout

```
munin/
  pyproject.toml
  install.sh
  src/munin/
    cli.py            start stop toggle status list retry voice review
                      export glossary models setup doctor
    daemon.py         munin-rec: detection, prompts, capture
    worker.py         munin-work: spool drain, pipeline, output
    admin/            loopback web surface (§9.5 of the spec)
    capture/          base.py first, then linux.py, macos.py, windows.py
    detect/           base.py, linux.py, macos.py, windows.py
    spool.py          session layout, segments, state machine, locking
    pipeline/         language.py asr.py diarize.py attribute.py
                      voices.py glossary.py render.py
    backends/         local.py ssh.py api.py
    calendar.py       M365 enrichment
  plugin/local.munin/ manifest.json, Service.qml, BarWidget.qml, Model.js
  systemd/munin.service
  tests/
  docs/
    superpowers/{specs,plans}/
    design/           sketches (HTML + JPG)
```

## 4. Milestones

| # | Milestone | Delivers | Blocked by | Exit criterion |
|---|---|---|---|---|
| M1 | Linux two-track capture | `capture/base.py` and `detect/base.py` first, then the Linux implementations; `munin-rec` captures on command and writes a session | — | A real Teams call yields `mic.opus` + `app.opus` with correct separation, and nothing above the boundary names a platform |
| M2 | Spool, CLI, segments | State machine, `start/stop/toggle/status/list`, resume-into-same-session, disk guard; `munin split` and the `split` state (D26, added 2026-09-21) | M1 | Ad-hoc path works with no calendar and no detection; a two-meeting recording cuts into two sessions and the parent keeps its audio |
| M3 | Shell plugin | Detection, three notifications, bar states, dropdown panel; the split prompt and its panel button (D26, added 2026-09-21) | M0, M2 | Joining a Teams call prompts; the bar shows state throughout; a second call going live mid-recording offers a split |
| M4 | Pipeline on `local` | Language routing, ASR, diarization, attribution tiers 1/3/4, output format | M1 | A captured meeting yields a conformant transcript |
| M4a | GPU courtesy | `keep_warm = false` path, `defer_when_busy` VRAM check before claiming a session, CUDA OOM treated as an unreachable sink (D22, spec §8.2) | M4 | A session defers while the GPU is committed elsewhere and drains when it frees |
| M5 | `munin doctor` and `install.sh` | One-command install, wizard, check | M3, M4 | A clean machine reaches a working recording without reading the spec |
| M6 | Evaluation harness | Held-out Plaud set, hand-corrected references, WER and glossary recall; the §7.6 encoder candidates measured on the same set | M4 | Baseline numbers for Plaud vs Munin on the same audio, and a WER-backed answer on `voip`/DTX capture |
| M7 | Glossary | User-written `glossary.toml` (D8), two-stage injection, `munin glossary suggest` for corruption discovery over own transcripts | M6 | §13's 100% glossary-term recall met and measured |
| M8 | Voice register + admin | Profiles, review queue, tier 2 matching, admin surface | M4 | A colleague is named correctly in an ad-hoc call with no invite |
| M9 | M365 enrichment | Titles, attendees, agenda biasing, series-level opt-out | M4 | Sessions carry real titles; agenda terms reach the decode prompt |
| M10 | `ssh` backend | Push audio, run remote worker, pull transcript | M4, OQ10 | Mini PC records, desktop transcribes, same text as `local` |
| M11 | `api` backend | Remote ASR, local diarization split | M4, OQ2, OQ9 | Same fixture through `api` matches `local` |
| M12 | Retention, and retiring the Plaud export | Retention pass; `export`/`list --not-uploaded` only if the export outlives M4, which D25 says it does not | M2 | Retention runs on a schedule; the mix, `munin mix` and the upload folder are gone, or there is a written reason they are not |
| M13 | macOS capture and indicator | ScreenCaptureKit behind `capture/base.py`, menu-bar extra | M1 | MacBook produces the same session layout as Linux |
| M14 | Windows capture and indicator | WASAPI process loopback, tray icon | M1 | Same session layout again; detection by process name |

**M0 is done.** `SUPER + SHIFT + R` is free, and `voxtype` does not contend for
the microphone: it holds the default source only during push-to-talk, and two
concurrent readers on one source were verified working. Both findings are in
Appendix D. What remains from that spike is one live-call test of voxtype's
`pause_media`, which pauses MPRIS players and may pause a Teams browser tab.

**M1 carries the portability burden.** §16 of the spec is a constraint on this
milestone, not a later concern: the base interfaces are written first, and if
the pipeline ever needs to know which platform it is on, the boundary is wrong.
Getting that wrong is cheap to fix in M1 and expensive to fix at M13.

**M5 lands early on purpose.** `munin doctor` is where every open question
becomes a line of output, which is what makes the rest of the build debuggable
and a broken install self-describing six months later.

## 5. Backend order

`local` first (unblocked), then `ssh`, then `api`.

`ssh` before `api` is a change from the spec's original sink ordering. `ssh` is
the availability floor — until it exists, every meeting depends on one desktop
being awake — while `api` needs the cluster proposal approved and two gateway
capabilities confirmed.

Counterargument: `ssh` carries an unattended-authentication problem (OQ10) that
`api` does not, and if the cluster lands soon the effort is spent on the
least-used path. Accepted anyway: "never lose a meeting" outranks latency, and
the backend interface makes each one small once the first exists.

## 6. Testing, per §13 of the spec

Written alongside each milestone, not after.

- **M1**: synthetic two-track fixtures — separation, sink-input binding, music
  playing during capture absent from track 2.
- **M2**: segment concatenation, resume window boundaries, grace period,
  duplicate-event handling, disk-full refusal.
- **M3**: detection matrix — native app, PWA and browser tab each trigger; a
  browser with audio and no microphone does not; a Teams tab with no call does
  not; a second call going live mid-recording offers a split once per distinct
  call, never once per poll.
- **M2 (split, D26)**: the halves' audio sums to the parent's and each tone
  survives its own track; the parent keeps its checksums and leaves the queue;
  a cut that fails part-way leaves the parent queued and no directory behind.
  What stays untested until real hardware is a live split during an actual
  call, and how often an application renames its own window mid-call (OQ13).
- **M4**: output conformance — monotonic timestamps, `HH:MM:SS`, filename
  convention; self-attribution exact on two-track fixtures.
- **M8**: voice matching — precision and recall against hand-labelled
  references; a thin profile must degrade to tier 3, not mislabel.
- **M6/M7**: WER overall, and 100% recall on the Appendix B glossary classes.
  This is the acceptance target for the whole project.
- **M10/M11**: the same fixture through every backend must produce identical
  text.

Fixtures are synthetic or own-voice. No real customer audio in the test corpus.

## 7. Decisions still needed

Numbers refer to the spec's §14.

1. **OQ11 — does deleting a voice profile unname past transcripts?** Defensible
   either way. Cheap now, awkward the first time someone asks.
2. **Retention defaults.** Audio is ~11 MB an hour and loses value once the
   transcript is verified; transcripts are small and worth keeping. Proposed:
   audio 90 days, transcripts indefinitely.
3. **Is Windows actually planned, or only possible?** The spec now assumes
   Linux → macOS → Windows (D21, §16). The portability work in M1 is cheap
   either way, but M14 is real scope that has never been costed.
4. **OQ10 — unattended SSH auth.** A command-restricted key with no passphrase
   is less elegant than an unlocked agent and more likely to still work in a
   year.
5. **Glossary location.** `examples/glossary.toml` is synthetic and belongs
   here. The real one is by definition a list of real customer, product and
   colleague names — exactly what cannot enter this public repo. Under D8 it is
   also hand-written, so it is the only copy of real user effort and losing it
   costs more than a rebuild. It lives at
   `~/munin/glossary.toml`; whether it is also version-controlled somewhere
   private needs deciding before M7.
6. **Capture encoder: `voip` mode and DTX (proposed, gated on M6).** §6.1
   encodes each track with `libopus -b:a 24k` and nothing else, so the encoder
   runs in `application=audio` with DTX off and spends full bitrate coding
   silence. Re-encoding the first real meeting (2026-09-17, 17:07, headset, mic
   device-muted between turns) through the same `s16 → libopus` pipe:

   | Settings | mic | app | both | vs now |
   |---|---|---|---|---|
   | now: `-b:a 24k` | 2.27 MB | 1.90 MB | 4.17 MB | — |
   | `-b:a 24k -application voip -dtx 1` | 1.19 MB | 1.31 MB | **2.50 MB** | −40% |
   | `-b:a 16k -application voip -dtx 1 -frame_duration 60` | 0.80 MB | 0.83 MB | **1.63 MB** | −61% |

   At the corpus rate (median 28 min, ~1.4 meetings a day, ~240 hours a year)
   that is ~3.5 GB a year now against ~2.1 GB, or ~1.4 GB at 16 kbps.

   **Already verified:** DTX is sample-exact. All three variants decode to
   49,306,892 samples, identical to the original, so the two-track alignment
   that tier 1 attribution depends on (§7.4) is untouched. The measurement is a
   re-encode of already-encoded audio, which is the right *shape* (the capture
   path is `s16` in, Opus out) but not the same as recording a meeting with the
   new settings.

   **Not verified, and why this is not simply changed:** these are the master
   copies. `voip` is "favour speech intelligibility" rather than "favour
   faithfulness to the input", and DTX decides on its own what is silence — a
   soft onset or a quiet far-end talker is exactly the material both could
   damage, and exactly the material WER is measured on. So: **M6 first.** Run
   the held-out set through the current settings and each candidate, and adopt
   the cheapest one that does not move WER. `-application voip -dtx 1` at the
   same 24 kbps is the likely answer and the smaller risk; 16 kbps is a real
   quality question, not a free win, and must not become a default without a
   number next to it.

   Independent of the outcome, the *derived* file already moved: `munin mix`
   writes Opus at 24 kbps (3.0 MB for that meeting) instead of the MP3 at
   64 kbps (8.2 MB) the first draft of §10 chose — smaller and better, with no
   risk to a master copy.

7. **The split's three open questions (OQ13, OQ14, OQ15), added 2026-09-21.**
   None blocks the split shipping; each blocks calling it finished.
   - **OQ13 — how often does a meeting application rename its own window
     mid-call?** The prompt reads `(pid, window_title)` as a call's identity, so
     a rename is a false prompt. `munin-rec` now logs the rename-shaped case
     ("renamed or next meeting" in `~/munin/munin.log`), so this is answered by
     using the thing for a week, not by reasoning about it.
   - **OQ14 — what does a split mean once `calendar_event_id` is populated?**
     Both halves inherit the parent's event id, which §11's duplicate-capture
     rule reads as one meeting captured twice. Needs a `split_from` carve-out
     **before M9 lands**, not after.
   - **OQ15 — is a split one record or three for retention?** The parent holds a
     full copy of audio that now also exists in the halves, and a half can be
     split again, so it is not even bounded at three. Part of the retention
     decision (item 2), not a separate one.

## 8. What to do first

1. Decide item 5 above (glossary location); it is cheap and blocks M7.
2. Start M1, base interfaces before Linux implementations (§16.5).
3. Test `voxtype`'s `pause_media` against a live Teams call when one is next
   convenient. Not blocking; the mitigation is one config line.
