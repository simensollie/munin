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
| M2 | Spool, CLI, segments | State machine, `start/stop/toggle/status/list`, resume-into-same-session, disk guard | M1 | Ad-hoc path works with no calendar and no detection |
| M3 | Shell plugin | Detection, three notifications, bar states, dropdown panel | M0, M2 | Joining a Teams call prompts; the bar shows state throughout |
| M4 | Pipeline on `local` | Language routing, ASR, diarization, attribution tiers 1/3/4, output format | M1 | A captured meeting yields a conformant transcript |
| M4a | GPU courtesy | `keep_warm = false` path, `defer_when_busy` VRAM check before claiming a session, CUDA OOM treated as an unreachable sink (D22, spec §8.2) | M4 | A session defers while the GPU is committed elsewhere and drains when it frees |
| M5 | `munin doctor` and `install.sh` | One-command install, wizard, check | M3, M4 | A clean machine reaches a working recording without reading the spec |
| M6 | Evaluation harness | Held-out Plaud set, hand-corrected references, WER and glossary recall | M4 | Baseline numbers for Plaud vs Munin on the same audio |
| M7 | Glossary | `munin glossary build`, corruption discovery, two-stage injection | M6 | §13's 100% glossary-term recall met and measured |
| M8 | Voice register + admin | Profiles, review queue, tier 2 matching, admin surface | M4 | A colleague is named correctly in an ad-hoc call with no invite |
| M9 | M365 enrichment | Titles, attendees, agenda biasing, series-level opt-out | M4 | Sessions carry real titles; agenda terms reach the decode prompt |
| M10 | `ssh` backend | Push audio, run remote worker, pull transcript | M4, OQ10 | Mini PC records, desktop transcribes, same text as `local` |
| M11 | `api` backend | Remote ASR, local diarization split | M4, OQ2, OQ9 | Same fixture through `api` matches `local` |
| M12 | Plaud export, retention | `export`, `list --not-uploaded`, retention pass | M2, OQ1 | Outstanding upload set queryable; retention runs on a schedule |
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
  not.
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
   colleague names — exactly what cannot enter this public repo. It lives at
   `~/munin/glossary.toml`; whether it is also version-controlled somewhere
   private needs deciding before M7.

## 8. What to do first

1. Decide item 5 above (glossary location); it is cheap and blocks M7.
2. Start M1, base interfaces before Linux implementations (§16.5).
3. Test `voxtype`'s `pause_media` against a live Teams call when one is next
   convenient. Not blocking; the mitigation is one config line.
