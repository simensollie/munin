# Munin: cross-platform digital meeting recorder and transcription pipeline

**Status:** Design, pending approval
**Date:** 2026-09-14
**Owner:** Simen Sollie
**Feeds:** `pensieve` (`~/pensieve/raw/`)

> Public copy. Customer names, colleague names and internal project references
> have been replaced with generic descriptors. Counts and measurements are
> unchanged.

---

## 1. Problem

Meetings are currently recorded with Plaud on a MacBook and transcribed by Plaud's
cloud. Transcripts land in `pensieve/raw/` via the `plaud` CLI and are distilled
into the wiki by hand (with Claude).

Two things break this:

1. **Plaud has no Linux client.** The office machine will be a Beelink mini PC
   running Arch (Omarchy), and the home-office desktop also runs Arch. Meetings
   held from either machine cannot be recorded at all today.
2. **Plaud cannot be scripted into the loop.** The official CLI (`@plaud-ai/cli`,
   latest 0.3.12) is read-only. There is no upload command, and the documented
   API writes into a separate developer silo rather than the personal library
   the CLI reads. Details in Appendix A.

Separately, analysis of the existing 204-file corpus (Appendix B) shows Plaud's
transcription is actively damaging for this domain: half of all `ISO 42001`
mentions are transcribed as `ISO 4200`, one customer name appears under six
spellings, an in-house product name never appears correctly at all, and a
second customer name appears zero times in 756,000 words.

## 2. Goals

- Record digital meetings on Linux and macOS with no manual steps.
- Produce transcripts in `pensieve/raw/` that are at least as good as Plaud's,
  and materially better on domain vocabulary.
- Keep customer meeting audio on infrastructure under our control.
- Survive any single machine being offline.

## 3. Non-goals

- **Physical / in-room meetings.** Out of scope. Those stay on Plaud.
- **Real-time captions.** This is a batch pipeline.
- **Replacing Plaud's AI summaries.** `pensieve` ingest does its own distillation
  with Claude and never consumes Plaud's `Summary.md`.
- **A GUI.** Daemon plus CLI.
- **Voice enrollment / speaker identification from voiceprints.** See §10.

## 4. Constraints and settled decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Digital meetings only | User scope decision; also puts diarization in its best acoustic regime (§7.4) |
| D2 | Two-track capture: own mic separate from application audio | Makes self-attribution exact with no model involved (§7.4) |
| D3 | Pluggable sink; recorder never knows who transcribes | Lets the worker live on a different machine, and lets the backend change without touching capture |
| D4 | **Suggest, never auto-record.** Trigger on meeting-app audio; calendar is enrichment only | A recorder that asks is defensible (§12); app audio detects ad-hoc calls the calendar never knew about |
| D5 | Self-hosted Whisper, not Plaud upload | Plaud upload is only reachable via an undocumented consumer endpoint in a different auth realm (Appendix A) |
| D6 | Two ASR models, routed by detected language | Corpus is 112 English-dominant vs 92 Norwegian-dominant files |
| D7 | Output timestamps become `HH:MM:SS` | Deliberate break from Plaud's `MM:SS`, which produces `[103:06 - 103:07]` on long meetings |
| D8 | Glossary is generated, not hand-maintained | Sources already exist (§7.3); a hand-list would rot |
| D9 | ASR may be remote, diarization is always local | `/v1/audio/transcriptions` has no speaker field (§8.2) |
| D10 | Recording state is always visible in the bar | Consent and self-awareness; also the only reliable stop control |
| D11 | Mixed audio is retained for manual Plaud upload | Plaud summaries stay available as a separate, opt-in process (§10) |
| D12 | Scripts use a `munin-` prefix, never `omarchy-` | Graphical-session PATH puts `/usr/share/omarchy/bin` first, so an `omarchy-*` override from `~/.local/bin` silently loses |

## 5. Architecture

```
  DETECT                CAPTURE                 SPOOL              SINK
┌──────────────┐   ┌──────────────────┐   ┌──────────────┐   ┌──────────────────┐
│ app holds    │   │ Linux: PipeWire  │   │ <session>/   │   │ A. 3070  (CUDA)  │
│ sink-input   │──▶│  t1 = mic        │──▶│   mic.opus   │──▶│ B. cluster       │
│ AND mic      │ ▲ │  t2 = app sink   │   │   app.opus   │   │    (LiteLLM)     │
│ capture      │ │ │ macOS: BlackHole │   │   mixed.mp3  │   │ C. mini PC (Arc) │
└──────┬───────┘ │ │  (phase 2)       │   │   meta.json  │   └────────┬─────────┘
       │         │ └──────────────────┘   │   state      │            │
       ▼         │                        └──────┬───────┘   ASR remote-or-local
  notification   │                               │           diarization ALWAYS local
  + bar glyph    │                               │                    │
       │         │                               ▼                    ▼
       └─ user ──┘                       munin export      pensieve/raw/<title>-transcript.txt
          says yes                       (manual Plaud upload)
                                    ▲
                        M365 calendar: enrichment only
                        (title, attendees, agenda) — never a trigger
```

Three processes, deliberately separable:

- **`munin-rec`** — daemon on each machine. Detects calls, notifies, captures on
  confirmation, writes sessions to the spool. Never transcribes.
- **`munin-work`** — worker. Drains any spool it can reach, runs the pipeline,
  writes the transcript. Runs where the compute is. May call a remote ASR
  endpoint but always diarizes locally.
- **`munin`** — CLI. `start`, `stop`, `toggle`, `status`, `list`, `retry`,
  `sinks`, `export`, `glossary`.

## 6. Capture

### 6.1 Linux (primary)

PipeWire exposes both halves natively:

- **Track 1 (you):** the default audio *source* (your microphone).
- **Track 2 (them):** a `.monitor` source on the sink the meeting application is
  playing to, bound to the specific sink-input rather than the whole system
  output, so music and notification sounds stay out of the mix.

Application detection matches sink-input properties (`application.name`,
`application.process.binary`) against a configured list: Teams, Chrome/Chromium
(for Meet), Slack, Zoom.

Encoding is Opus, 24 kbps mono per track, via `ffmpeg`. A 60-minute meeting is
roughly 11 MB for both tracks, which makes shipping audio between machines free
relative to transcribing it.

### 6.2 macOS (phase 2)

macOS has no monitor source. Capture requires either a BlackHole aggregate
device or ScreenCaptureKit. Deferred: the MacBook still has Plaud today, so this
is not on the critical path. The capture interface is designed so this slots in
without touching anything else.

### 6.3 Trigger and consent

**Detection, not automation.** The daemon never starts recording on its own.

The signal that a call is in progress is an application holding **both** a
sink-input and a microphone capture stream at the same time. This single
condition removes the entire false-positive class: a browser playing video has
the first but not the second. Matched applications are configurable; the default
list is Teams, Chrome/Chromium, Slack, Zoom.

On detection, munin sends a notification via Omarchy's wrapper:

```bash
munin-notify() { omarchy-notification-send -g 🎙 "Meeting detected: $1 — SUPER+SHIFT+R to record"; }
```

Notification *actions* (clickable buttons) are not assumed. Whether the Omarchy 4
Quickshell notification daemon supports them is unverified (Appendix D), so the
controls are the keybind and the bar module, both of which work regardless.

**Ad-hoc recording is a first-class path**, not a fallback:

```
munin start [title]      # or SUPER + SHIFT + R, or click the bar indicator
munin stop
munin status
```

**Calendar is enrichment only.** Once a session exists, munin matches it against
M365 calendar events by time overlap to fill in the real title, attendee list and
agenda. No match means an ad-hoc call: the title falls back to a prompt, then to
a timestamp. The calendar never starts or stops anything.

Auto-stop when the application's streams have been gone for 2 minutes, with a
notification. Recording inhibits idle/lock for its duration (`shell.json` sets
screensaver at 150 s and lock at 300 s, which would otherwise fire mid-meeting).

## 7. Pipeline

### 7.1 Language routing

Run Whisper's `detect_language` on three 30-second samples (start, midpoint,
end of track 2) and take the majority. Three samples rather than one guards
against a meeting that opens in English small talk and continues in Norwegian.

Routing is per file, not per segment. The corpus shows meetings are dominantly
one language, and code-switched English terms inside Norwegian sentences
(`customer request featureen`, `multi-tenant løsning`) already transcribe
correctly without intervention.

### 7.2 Models

| Model | Role | fp16 |
|---|---|---|
| `NbAiLab/nb-whisper-large` | Norwegian | ~3.1 GB |
| `openai/whisper-large-v3` | English | ~3.1 GB |
| `pyannote/speaker-diarization-3.1` | diarization | ~1 GB |

Both ASR models stay resident (~6 GB). Model load dominates inference at this
volume, so keeping both warm is what makes single-digit-minute latency possible
on a queue that fires a few times a day.

No quantization and no distilled variants. `nb-whisper-large-distil-turbo-beta`
exists and is within ~1% WER at 6x the speed, but speed is not the binding
constraint (§8), so we take the accuracy.

### 7.3 Domain glossary and person list (generated)

The glossary is **built, not hand-written**. `munin glossary build` pulls
canonical terms from sources that already exist and stay current:

| Source | Yields |
|---|---|
| `pensieve` wiki filenames + `index.md` display names | products, features, customers, competitors |
| M365 calendar attendees (accumulated) | colleague and customer names, with emails |
| M365 directory (`search_people`) | the wider org |
| The issue tracker | project and feature names |
| The existing 204 Plaud transcripts | frequency-ranked proper nouns |

Discovering the *corruptions* is the second half and the valuable one. The corpus
contains both the correct and the corrupted form of most terms, so: take rare
tokens, compare against the canonical list, propose mappings. Edit distance alone
will not find a Norwegian common noun substituted for an English product name,
because that is a phonetic collision in a Norwegian mouth rather than a typo.
This is therefore a batch job for Claude over the frequency-ranked
unknown-token list, human-reviewed once, then topped up
after each run. Output is a reviewed `glossary.toml` under version control.

**Injection is two-stage, because `initial_prompt` is capped at 224 tokens**
(half of Whisper's 448-token decoder context; anything longer is silently
truncated):

1. **Decode-time bias**: ~40 terms selected per meeting from calendar context.
   An invite naming a customer promotes that customer's terms.
2. **Post-correction**: the full mapping applied to the output text afterwards,
   deterministic replacements first, then an LLM review pass for the rest.

Stage 2 is where the acceptance criteria in §13 are actually met.

### 7.4 Speaker attribution

Three tiers, in order:

1. **You are exact.** Track 1 is your microphone. Every segment with energy on
   track 1 and not on track 2 is you, by construction. No model, no error.
2. **Everyone else is diarized** on track 2 with pyannote, producing
   `SPEAKER_00`, `SPEAKER_01` and so on.
3. **Labels are assigned closed-set** against the calendar attendee list. The
   question is never "who is this out of everyone", only "which of these four",
   which is a far easier problem than the open-set task pyannote is benchmarked
   on. Where the assignment is not confident, the positional label is kept.

Digital-only capture helps here: Teams and Meet audio is per-participant headset
mics, already noise-suppressed and echo-cancelled upstream. That is the
AMI-headset acoustic condition (pyannote ~12-14% DER) rather than the far-field
single-room-mic condition where DER degrades badly.

Explicitly not doing voice enrollment. See §12.

### 7.5 Output

Written to `pensieve/raw/<sanitised calendar title>-transcript.txt`, matching the
existing convention (`:` and `/` replaced with `_`).

```
[00:00:08 - 00:00:17] Ola Nordmann: ...
[00:00:17 - 00:00:31] Kari Nordmann: ...
```

Changes from Plaud's format, each deliberate:

- `HH:MM:SS` instead of `MM:SS`. Plaud emits `[103:06 - 103:07]` past 100
  minutes; 25 of 204 files exceed an hour.
- Strictly monotonic segments. 52 segments across 35 existing files start before
  the previous one ended, and 5 have negative duration.
- Stable speaker labels. The corpus uses a bare first name for the same person
  in April files and the full display name from May onward; munin always uses
  the calendar's display name.

A sidecar `<name>-transcript.json` carries word-level timestamps and confidences
for anything that later wants them. The `.txt` stays the source of record, as
`pensieve/CLAUDE.md` specifies.

## 8. Deployment

### 8.1 Sinks

Three sinks, same models, different runtime. The recorder is identical in all
three.

| | **A: home desktop** | **B: shared cluster via LiteLLM** | **C: mini PC fallback** |
|---|---|---|---|
| Hardware | RTX 3070, 8 GB | Apple silicon node (spec TBC) | Intel Arc 140T iGPU |
| Runtime | faster-whisper (CTranslate2 + CUDA) | whisper.cpp Metal or mlx-whisper behind LiteLLM | whisper.cpp Vulkan |
| Model artifact | CTranslate2 | GGML or MLX | GGML |
| Interface | in-process | `POST /v1/audio/transcriptions` | in-process |
| Diarization on | CUDA | **local** (see 8.2) | CPU |
| 60-min meeting | ~5-8 min (est.) | ~5-6 min (est.) | ~30-40 min (est.) |
| Available | when desktop is on | datacenter uptime | always |

CTranslate2 has no Metal backend (`device="mps"` raises `ValueError: unsupported
device mps`), which is why B is a different artifact rather than the same one.

**Order of implementation: A first**, because it is unblocked. B when the
cluster proposal is approved (§12). C is the floor, so a meeting is never lost
because two machines are unreachable.

Load is trivially small in all cases: median meeting 28 minutes, roughly 1.4
meetings per day across the existing corpus. Even three meetings on a heavy day
is ~90 minutes of audio, under 10 minutes of GPU time on A or B.

### 8.2 LiteLLM changes the shape of sink B

The cluster exposes models through a LiteLLM gateway, so sink B is a thin HTTP
client rather than a bespoke service. Model selection is a string
(`nb-whisper-large`, `whisper-large-v3`), which means §7.1's language routing
becomes a choice of `model` parameter and nothing else.

**The consequence: the pipeline splits.** The OpenAI audio API returns text and
optionally word timestamps. It has no speaker field, so pyannote cannot live
behind the gateway. Diarization therefore always runs locally, on the mini PC CPU
or the 3070, regardless of where ASR happens. This is acceptable because
diarization is the cheaper half and track 1 already provides self-attribution for
free (§7.4).

Two capabilities to confirm with IT before committing to B:

- Does the gateway pass through **`prompt`**? Required for §7.3 stage 1. Without
  it, glossary biasing is lost and only post-correction remains.
- Does it pass through **`timestamp_granularities[]=word`** with
  `response_format=verbose_json`? Required to align ASR output with diarization
  turns.

If either is missing, the fallback is a direct connection to the whisper server
behind the gateway, bypassing LiteLLM for this one workload.

## 9. Desktop integration (Omarchy)

Conventions read from `~/dev/dotfiles/omarchy`; see Appendix D for what is
verified and what is not.

### 9.1 Daemon

A systemd `--user` unit, following the existing `voxtype.service` template
exactly (that precedent matters: voxtype is also an audio daemon on this
machine).

```ini
[Unit]
Description=Munin meeting recorder daemon
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart=%h/.local/bin/munin daemon
Restart=on-failure
RestartSec=5
Environment=XDG_RUNTIME_DIR=%t

[Install]
WantedBy=graphical-session.target
```

Deployed by copy (`cp` into `~/.config/systemd/user/`), enabled by hand.
Enablement is never committed, per the dotfiles README.

`o.launch_on_start` is the alternative but has no restart or lifecycle control,
so systemd is the right choice here.

### 9.2 Bar indicator

**Omarchy 4 does not use Waybar.** The bar is Quickshell, configured in
`~/.config/omarchy/shell.json` with modules referenced by id and an empty
`"plugins": []` extension point. That plugin schema is undocumented in the
dotfiles repo and must be read on the machine (`omarchy bar --help`,
`/usr/share/omarchy`) before this section can be finalised. See open question 5.

What the indicator must show regardless of mechanism:

| State | Glyph | Meaning |
|---|---|---|
| idle | (hidden) | not recording |
| detected | 🎙 dim | a call is in progress, munin is not recording |
| recording | ● red + elapsed | capturing |
| transcribing | ⟳ | queued or in flight to a sink |
| failed | ! | needs attention |

Click toggles start/stop. The recording state must be **visible at a glance**,
which is the point of D10: you should never be unsure whether you are recording.

The pre-Omarchy-4 Waybar precedent in git history (`custom/voxtype`,
`custom/screenrecording-indicator`) shows the house idiom for this kind of
indicator: a script emitting status JSON, refreshed by `SIGRTMIN+n` signal on
state change rather than polled on an interval. Munin should be signal- or
event-driven for the same reason: recording state changes rarely and must
update instantly.

**Operational caveat:** `shell.json` is deployed as a copy, not a symlink,
because `omarchy-shell-config` writes via atomic rename. Any bar change is made
with `omarchy bar ...` or by editing the live file, then copied back into the
dotfiles repo by hand.

### 9.3 Keybinding

In `~/.config/hypr/bindings.lua`, following the existing idiom and unbinding
first if Omarchy already owns the key:

```lua
hl.unbind("SUPER + SHIFT + R")   -- only if Omarchy binds it; verify with:
                                 --   omarchy menu keybindings --print
o.bind("SUPER + SHIFT + R", "Record meeting", { exec = "munin toggle" })
```

`SUPER + SHIFT + R` is not referenced in the current bindings file, so it is
probably an Omarchy default and needs checking. The `exec` action form is also
unverified in Omarchy 4's Lua API (no example exists in the repo); the pre-4.x
equivalent was `bindd = ..., exec, ...`.

### 9.4 Scripts

`~/.local/bin/munin*`, bash, `set -euo pipefail`, a comment block explaining why
before any code, state under `${XDG_CACHE_HOME:-$HOME/.cache}/munin`.

**Never an `omarchy-` prefix** (D12): the graphical-session PATH puts
`/usr/share/omarchy/bin` ahead of `~/.local/bin`, so an `omarchy-*` script in a
user directory silently loses to the packaged one, even though the login-shell
PATH is ordered the other way and makes it look like it works.

## 10. Plaud export (manual, opt-in)

Plaud's `Summary.md` output (topic grouping, owner-attributed actions, an
`AI-forslag` section flagging unresolved items) is the one thing munin does not
reproduce. Rather than rebuild it, keep the option of getting it from Plaud by
hand for meetings that warrant it.

Each session therefore retains a **mixed-down copy** alongside the two tracks:
`ffmpeg -i mic.opus -i app.opus -filter_complex amix=inputs=2 mixed.mp3`. Plaud
accepts MP3 and OPUS only, 5 hours maximum, so MP3 at 64 kbps mono is the safe
choice. That is roughly 28 MB per hour, so the full existing archive's worth is
a few GB.

```
munin export --since 2026-09-01 --to ~/plaud-upload
munin list --not-uploaded
munin mark-uploaded <session>
```

Exported filenames match the transcript naming convention exactly, so a file in
the upload folder pairs unambiguously with its transcript in `pensieve/raw/`.
Upload state lives in session metadata, so the outstanding set is always
queryable. The upload itself is a manual drag into Plaud's web importer; munin
does not automate it (Appendix A).

## 11. Failure handling

The design principle is that transcripts arrive late, never missing.

| Failure | Behaviour |
|---|---|
| No sink reachable | Session stays in spool, retried with backoff. Nothing is lost. |
| Worker crashes mid-file | Session state is `transcribing`; reset to `pending` on worker start. |
| Meeting runs past scheduled end | Capture continues until the application stream has been silent for 2 minutes. |
| Calendar unreachable | Ad-hoc mode: capture still triggers on application audio, title falls back to timestamp. |
| Disk fills | Refuse to start a new capture below a configured threshold and alert, rather than truncating. |
| Duplicate capture (two machines in the same meeting) | Sessions carry the calendar event ID; the worker keeps the longest and discards the rest. |

Spool sessions are never deleted by the worker, only marked `done`. A separate
retention pass handles cleanup (§10).

## 12. Compliance and privacy

This records identifiable people, including customers, in a regulated context. Flagging explicitly rather than burying it:

- **Audio and transcripts are personal data under GDPR.** Self-hosting improves
  the story (no external processor) but does not remove the obligation. A basis
  and a retention period are needed, not implied.
- **Participants should know.** Recording a meeting you are in is one thing;
  retaining transcripts of others' speech indefinitely on company infrastructure
  is a processing activity that should be disclosed.
- **No voice enrollment.** Voiceprints used to identify a person are biometric
  data and a special category under GDPR Article 9, a materially higher bar than
  transcripts. The tiered attribution in §7.4 gets most of the benefit without
  holding them. Revisit only as a deliberate, consented, documented change.
- **The internal cluster proposal scopes testing to synthetic data only, no
  real customer content.** Deployment B sits outside that scope and needs its
  own section in that proposal, not a footnote.
- **ISO 27001.** A transcript store accumulating customer commercial detail is a
  new asset with its own access control and retention requirements.
- **Personal vault, work content.** `pensieve` is personal; transcripts of
  customer meetings are arguably company records. Worth a deliberate decision on
  where the authoritative copy lives.

## 13. Testing

- **Capture**: synthetic two-track fixtures; assert track separation, correct
  sink-input binding, that music playing during capture does not appear.
- **Trigger**: fake calendar fixtures; assert start/stop boundaries, grace
  period, the no-audio guard, and duplicate-event handling.
- **Pipeline**: a held-out set of existing Plaud recordings (audio retrievable
  via `plaud audio <file_id>`) with hand-corrected reference transcripts.
  Assert WER overall, and separately assert **100% recall on the glossary terms**
  in Appendix B, which is the actual reason for doing this.
- **Attribution**: assert self-attribution is exact on two-track fixtures;
  measure DER against hand-labelled references.
- **Output**: assert monotonic timestamps, format conformance, and that the
  filename matches `pensieve`'s convention.
- **Sinks**: the same fixture through A, B and C must produce identical text.

## 14. Open questions

1. Where does the authoritative transcript live once work meetings are involved
   (§10)? Affects retention design.
2. Can the home desktop reach the shared cluster, or is that office-network
   only? Determines whether B is usable from home office.
3. Cluster node specs are still unknown, pending a hardware scoping session.
4. Is a Teams-native attendee roster available per meeting, which would sharpen
   closed-set assignment beyond the invite list?
5. What does `shell.json`'s `"plugins"` array accept? Blocks §9.2. Read
   `/usr/share/omarchy` and `omarchy bar --help` on the Linux box.
6. Does the Omarchy 4 notification daemon support notification actions? If yes,
   §6.3 can offer Record/Ignore buttons directly.
7. Does `o.bind` expose a raw `exec` action in Omarchy 4, and is
   `SUPER + SHIFT + R` free?
8. **Microphone contention with `voxtype`**, the existing push-to-talk daemon.
   Both want the mic. PipeWire permits multiple readers, but this needs
   confirming on hardware before it bites mid-meeting.
9. Does the LiteLLM gateway pass through `prompt` and word timestamp
   granularity (§8.2)?

---

## Appendix A: Plaud upload feasibility (investigated 2026-09-14)

The official `@plaud-ai/cli` (latest 0.3.12) is read-only: `login logout me files
file audio transcript summary search recent today update version`. No upload.

Plaud has three non-composing realms:

| Realm | Base | Can write? | Lands in |
|---|---|---|---|
| Consumer app | `api.plaud.ai` | Yes, undocumented | your personal library |
| Third-party OAuth (what the CLI uses) | `platform.plaud.ai/developer/api/open/third-party/` | No | n/a |
| Partner / Embedded | `platform-us.plaud.ai/developer/api/open/partner/` | Yes, documented | the developer platform, not your library |

The consumer web app does support import, and the protocol is recoverable from
its public bundle:

```
small:  POST /file/upload  (multipart: file, filename, scene=101,
                            start_time, session_id, serial_number [, duration])
large:  POST /file/get_upload_presigned_url  {filesize, file_type:"MP3"|"OPUS"}
        PUT  <part_url>  (raw body, no auth header)
        POST /file/merge_multipart   {upload_id, object_name, parts:[{Etag,PartNumber}]}
        POST /file/confirm_upload    {upload_id, object_name, scene:101, is_tmp:0,
                                      support_mul_summ:true, file_type, filename,
                                      start_time, session_id, serial_number}
```

Only MP3 and OPUS are accepted; the web app transcodes client-side with ffmpeg
first. A probe with the CLI's bearer token returned
`{"status":-3900,"msg":"invalid auth header"}` on both `api.plaud.ai` and
`api-euc1.plaud.ai`: the endpoint is live, but the CLI's token belongs to a
different auth realm.

Uploading would therefore require holding a consumer web session token
(`pld_tokenstr`) on a headless Linux box, with unverified refresh behaviour,
against an undocumented endpoint. Rejected in favour of self-hosting (D5).

## Appendix B: Plaud output baseline (204 files, 2026-04-07 to 2026-09-14)

25,447 lines, ~756,000 words. Median meeting 28.0 minutes, median 106 segments,
median 4 speakers, 92 Norwegian-dominant / 112 English-dominant.

Format is exactly `[MM:SS - MM:SS] <label>: <text>`, no header, no blank lines,
no word-level data, no language tag.

**Domain vocabulary damage** (the acceptance target for §7.3):

Terms are given as classes rather than names; counts are the real ones.

| Term class | Plaud renders it as |
|---|---|
| Product name: English word with a near-homophone in Norwegian | correct 108x; the Norwegian homophone in three casings 46x; a neighbouring three-letter acronym 6x |
| Product name: letters + `365` | **0 correct**; four distinct wrong expansions |
| Customer name: five-letter acronym | six spellings over 342 occurrences; most frequent wrong form 178x, an unrelated acronym 43x, correct form only 13x |
| Customer name: short Norwegian proper noun | correct 369x; a common-noun homophone 33x, two further variants 8x |
| Domain term: `avvik` (deviation) | correct 325x; `Avik` 38, `AVX` 9, `AVIX` 4, `Havik` 2 |
| Customer name: five-letter acronym | **0 occurrences** |
| `Claude Code` | `Cloud Code` 12, `klod` 8, `Klodd` 7 |
| `ISO 42001` | correct 8x, **`ISO 4200` 8x**; also `ISO 2700`, `ISO 3200`, `ISO 2389` |

**Diarization:** 30% of segments are generic `Speaker N`, rising to 46% in
September. Sentences split mid-clause across two speakers; phantom speakers
appearing once per file; text bleeding between labels; 73 segments containing
decoder repetition loops.

**Instability:** two engine generations are visible. April files contain
stretches with no capitalisation or punctuation. Spurious mid-sentence capitals
run 8% (April) to 17% (August) to 35% (September).

## Appendix C: Norwegian ASR basis

| Benchmark | NB-Whisper large | OpenAI large-v3 |
|---|---|---|
| Fleurs (Bokmål) | 6.6% WER | 10.4% |
| NST | 2.2% WER | 6.8% |
| Common Voice (Nynorsk) | 12.6% WER | 30.0% |

NB-Whisper is fine-tuned on 66,000 hours of NRK, Stortinget and National Library
audio. These are clean read-speech benchmarks; real meeting WER is higher for
every system and the gap likely compresses. Treat the direction as reliable and
the magnitude as not.

`NbAiLab/nb.whisperX` (a WhisperX fork with word-level timestamps and
diarization, maintained by the National Library) is a candidate starting point
for the Norwegian path rather than assembling the pipeline from scratch.

## Appendix D: Omarchy conventions (read from `~/dev/dotfiles/omarchy`, 2026-09-14)

**Verified:**

- Bar is Quickshell, not Waybar. Config `~/.config/omarchy/shell.json`, modules
  by id, `"plugins": []` present and empty. Commit `5217f42` removed the Waybar
  config ("waybar is no longer installed").
- `shell.json` is deployed as a **copy**, never a symlink, because
  `omarchy-shell-config` writes with `jq > tmp; mv tmp` and the atomic rename
  would replace a symlink with a regular file.
- Notifications go through the `omarchy-notification-send -g <glyph> "<msg>"`
  wrapper, not raw `notify-send`.
- Keybindings: `o.bind(keys, description, action_table)` in
  `~/.config/hypr/bindings.lua`, loaded after Omarchy defaults, so
  `hl.unbind(...)` first for any key Omarchy owns. Modifier strings are
  `" + "`-joined uppercase.
- Hyprland config is Lua with **no build step**; `hyprctl reload` then
  `hyprctl configerrors` is the apply path.
- Daemons: systemd `--user` units, copied into place, enabled by hand, never
  committed as `*.target.wants/` symlinks. `voxtype.service` is the template.
- `~/.local/bin` for scripts, but the graphical-session PATH puts
  `/usr/share/omarchy/bin` first, so `omarchy-*` names cannot be shadowed from
  a user directory.
- `omarchy refresh` uses `cp -f`, which writes through symlinks and will
  overwrite dotfiles repo contents with Omarchy defaults.
- Idle: `shell.json` sets `screensaver: 150`, `lock: 300`.
- ActivityWatch is already running (`aw-qt`, window and media-player watchers).
- `xdph.conf` sets `allow_token_by_default = true`.

**Not verified, must be checked on the Linux box** (these are open questions
5-8, listed here so the gaps are not mistaken for settled design):

- The `"plugins"` schema in `shell.json`.
- Whether the notification daemon supports actions/buttons.
- Whether `o.bind` has a raw `exec` action in Omarchy 4, and whether
  `SUPER + SHIFT + R` is free.
- Whether `voxtype` and munin can hold the microphone simultaneously.
