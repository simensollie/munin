# Munin: cross-platform digital meeting recorder and transcription pipeline

**Status:** Design, pending approval
**Date:** 2026-09-14
**Revised:** 2026-09-14 — Teams-first scope, voice register, transcription
backends, install flow. Open questions 4, 5 and 6 answered on the machine.
**Owner:** Simen Sollie
**Store:** `~/munin/` (the session directory is authoritative, D24)
**Sketches:** [`../../design/munin-plugin-sketches.html`](../../design/munin-plugin-sketches.html)

> Public copy. Customer names, colleague names and internal project references
> have been replaced with generic descriptors. Counts and measurements are
> unchanged.

---

## 1. Problem

Meetings are currently recorded with Plaud on a MacBook and transcribed by Plaud's
cloud. Transcripts land in `pensieve/raw/` via the `plaud` CLI and are distilled
into the wiki by hand (with Claude). That is the arrangement being replaced;
Munin writes its transcripts under `~/munin/` and nowhere else (D24).

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

- Record digital meetings with one confirmation and no other manual steps:
  Linux now, macOS next, Windows after (§16).
- Produce transcripts at least as good as Plaud's, and materially better on
  domain vocabulary.
- Keep speaker names consistent across meetings, including ad-hoc calls the
  calendar never knew about.
- Keep customer meeting audio on infrastructure under our control.
- Survive any single machine being offline.
- Be installable and verifiable in one command each (§15).

## 3. Non-goals

- **Physical / in-room meetings.** Out of scope. Those stay on Plaud.
- **Real-time captions.** This is a batch pipeline.
- **Replacing Plaud's AI summaries.** `pensieve` ingest does its own distillation
  with Claude and never consumes Plaud's `Summary.md`.
- **A general GUI.** Daemon plus CLI. Two exceptions: the Omarchy bar plugin
  (§9.2) and a loopback admin surface for the voice register and backends (§9.5).
- **Real-time speaker identification.** Voice matching runs in the worker after
  capture, never during the meeting.

## 4. Constraints and settled decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Digital meetings only | User scope decision; also puts diarization in its best acoustic regime (§7.4) |
| D2 | Two-track capture: own mic separate from application audio | Makes self-attribution exact with no model involved (§7.4) |
| D3 | Pluggable backend; recorder never knows who transcribes | Lets the worker live on a different machine, and lets the backend change without touching capture (§8) |
| D4 | **Suggest, never auto-record.** Trigger on meeting-app audio; calendar is enrichment only | A recorder that asks is defensible (§12); app audio detects ad-hoc calls the calendar never knew about |
| D5 | Self-hosted Whisper, not Plaud upload | Plaud upload is only reachable via an undocumented consumer endpoint in a different auth realm (Appendix A) |
| D6 | Two ASR models, routed by detected language | Corpus is 112 English-dominant vs 92 Norwegian-dominant files |
| D7 | Output timestamps become `HH:MM:SS` | Deliberate break from Plaud's `MM:SS`, which produces `[103:06 - 103:07]` on long meetings |
| D8 | Glossary is generated, not hand-maintained | Sources already exist (§7.3); a hand-list would rot |
| D9 | ASR may be remote; diarization and voice matching are always local | `/v1/audio/transcriptions` has no speaker field (§8.1) |
| D10 | Recording state is always visible in the bar | Consent and self-awareness; also the only reliable stop control |
| D11 | Mixed audio is retained for manual Plaud upload | Plaud summaries stay available as a separate, opt-in process (§10) |
| D12 | Scripts use a `munin-` prefix, never `omarchy-` | Graphical-session PATH puts `/usr/share/omarchy/bin` first, so an `omarchy-*` override from `~/.local/bin` silently loses |
| D13 | Teams first; other platforms are configuration, not code | Identity and activity are detected separately (§6.3), so Zoom, Meet and Slack are rows in a table rather than new code paths |
| D14 | Auto-stop always notifies | A recording that stops silently is indistinguishable from a crash. The notification carries a Resume action (§6.3) |
| D15 | Resume inside 10 minutes continues the same session | A meeting that broke and came back is one meeting. Past the window, a new session (§6.3) |
| D16 | Voice register holds a name and an embedding, nothing more | Speaker names stay consistent across meetings, including ad-hoc calls with no invite (§7.4) |
| D17 | Naming a speaker and enrolling a voice are separate actions | A small register matches better than a large one: every profile is another candidate the matcher must discriminate against |
| D18 | All Munin data lives under `~/munin/` | One named root, so a directory in `$HOME` says what it is and what put it there (§7.5) |
| D19 | Three transcription backends — `local`, `ssh`, `api` — in an ordered fallback chain | One interface covers a GPU desktop, a headless mini PC and a shared gateway (§8) |
| D20 | The shell plugin never captures | A shell hot-reload or crash must never kill a recording; capture lives in a systemd daemon (§9.1) |
| D21 | Munin implements its own capture; no dependency on `voxtype` | `voxtype meeting` covers part of the Linux capture layer, but is Linux-only. Building on it would mean writing the same layer twice more for macOS and Windows, after the pipeline had shaped itself around another tool's data model (§16) |
| D22 | Models are not kept warm by default, and the worker defers to a busy GPU | The reference desktop's 3070 is a shared resource, and the pipeline is asynchronous. ~6 GB of permanently held VRAM buys about a minute per job that nobody is waiting for (§7.2, §8) |
| D23 | The `local` backend never imports the model stack; it runs it in a separate interpreter as a subprocess | Munin is stdlib-only and installs against the system Python, which on the reference desktop is 3.14; the pinned pyannote/faster-whisper/torch set needs 3.12. A process boundary keeps the two lifecycles independent, matches how every other external tool is invoked, and costs only interpreter startup, since D22 already reloads the model per job (§8.1) |
| D24 | The session directory is the only place a transcript is written; no copy goes to `pensieve` | A second copy in a personal vault made work records live in two stores with two retention policies and one access-control boundary between them (§12). The tree is `grep -r`-searchable, so the copy bought convenience that was already there (§7.5) |

## 5. Architecture

```
  DETECT                CAPTURE                 SPOOL             BACKEND
┌──────────────┐   ┌──────────────────┐   ┌──────────────┐   ┌──────────────────┐
│ process holds│   │ Linux: PipeWire  │   │ <session>/   │   │ 1. local  (CUDA) │
│ playback AND │──▶│  t1 = mic        │──▶│   mic.opus   │──▶│ 2. ssh    (LAN)  │
│ capture      │ ▲ │  t2 = app sink   │   │   app.opus   │   │ 3. api    (HTTP) │
│      AND     │ │ │ macOS: BlackHole │   │   mixed.mp3  │   │  ordered chain   │
│ it is Teams  │ │ │  (phase 2)       │   │  session.json│   └────────┬─────────┘
└──────┬───────┘ │ └──────────────────┘   └──────┬───────┘            │
       │         │                               │           ASR remote-or-local
       ▼         │                               │           diarization ALWAYS local
  notification   │                               │           voice match ALWAYS local
  + bar glyph    │                               ▼                    │
       │         │                    ~/munin/recordings/…            │
       └─ user ──┘                               │                    ▼
          says yes                               └──▶ …/<session>/transcript.txt
                                    ▲
                        M365: title, attendees, agenda — enrichment only,
                        never a trigger.  Voice register: names, locally.
```

Three processes, deliberately separable:

- **`munin-rec`** — daemon on each machine. Detects calls, notifies, captures on
  confirmation, writes sessions to the spool. Never transcribes.
- **`munin-work`** — worker. Drains any spool it can reach, runs the pipeline,
  writes the transcript. Runs where the compute is. May call a remote ASR
  endpoint but always diarizes locally.
- **`munin`** — CLI. `start`, `stop`, `toggle`, `status`, `list`, `retry`,
  `voice`, `review`, `export`, `glossary`, `models`, `setup`, `doctor`.
- **`munin admin`** — loopback web surface for the register, review queue,
  sessions and backends (§9.5).
- **`local.munin`** — the Omarchy shell plugin: detects, prompts, renders. Never
  captures (§9.2).

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

### 6.2 macOS and Windows (phases 2 and 3)

Neither is on the critical path — the MacBook still has Plaud today — but both
are planned, so the capture interface is narrow by design and nothing above it
knows which platform produced a session. See §16 for the full split.

- **macOS** has no monitor source. Per-application audio comes from
  ScreenCaptureKit (macOS 13+), which needs the screen-recording TCC permission
  in addition to the microphone one. A BlackHole aggregate device is the
  fallback, but it requires manual setup and a virtual device, so it is the
  worse plan rather than the first one.
- **Windows** has per-process loopback capture through WASAPI, which is a closer
  match to Munin's "bind to this specific stream" requirement than anything
  macOS offers.

Detection gets *easier* on both: Teams is a native application there, so the
three-shape problem in §6.3 collapses to a process match.

### 6.3 Trigger and confirmation

**Detection, not automation.** The daemon never starts recording on its own.

Detection is two independent conditions, and keeping them independent is what
makes other platforms configuration rather than code:

1. **A call is live.** One process holds a playback stream *and* a capture
   stream at the same time. This single condition removes the entire
   false-positive class: a browser playing video has the first but not the
   second. Read from `Quickshell.Services.Pipewire` inside the plugin, so there
   is no `pw-dump` polling.
2. **It is Teams.** Teams arrives in three shapes, and only one of them
   identifies itself to PipeWire:

| Shape | PipeWire reports | Window reports | Identified by |
|---|---|---|---|
| Native `teams-for-linux` | `application.name` = Teams | class `teams-for-linux` | PipeWire alone |
| Installed PWA | `Chromium` | class `chrome-teams.microsoft.com__-Default` | Window class |
| Browser tab | `Chromium` | title contains `Microsoft Teams` | Window title |

Match the PipeWire client first, since the native app is unambiguous and costs
nothing, then fall back to the Hyprland window owning that stream's PID and test
class, then title. Adding Zoom, Meet or Slack later is a row in that table;
condition 1 is already true for all of them.

**Three notifications, no more.** The Omarchy 4 notification daemon sets
`actionsSupported: true` (Appendix D), so each carries real buttons. The keybind
and the bar module work regardless, and are offered alongside rather than
instead — a notification can be missed, a keybind cannot.

| When | Says | Actions |
|---|---|---|
| Call detected | Meeting detected, with title and attendee count from the calendar | Record · Not this one · Never for this meeting |
| Streams gone 1 min | Meeting looks finished, stops by itself at 2:00 | Stop and transcribe · Keep recording |
| Auto-stopped | Recording stopped, *n* min captured, transcribing now | Resume · Open session |

The asymmetry is deliberate. Starting requires an explicit yes; stopping happens
on a timer if nothing is said. A missed start prompt costs one recording; a
missed stop prompt would record the rest of the afternoon.

**Resume continues, it does not restart** (D15). Resuming within 10 minutes
reopens the same session directory and writes `mic.002.opus` alongside the first
pair; `session.json` carries a `segments[]` list and the worker concatenates them
into one transcript with a gap marker. Past the window, a resume starts a fresh
session.

**Ad-hoc recording is a first-class path**, not a fallback:

```
munin start [title]      # or SUPER + SHIFT + R, or click the bar indicator
munin stop
munin status
```

**Calendar is enrichment only** (§7.6). No match means an ad-hoc call: the title
falls back to a prompt, then to a timestamp. The calendar never starts or stops
anything.

Recording inhibits idle/lock for its duration (`shell.json` sets screensaver at
150 s and lock at 300 s, which would otherwise fire mid-meeting).
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

The two ASR models total ~6 GB in fp16, ~7 GB with pyannote alongside during
diarization. Model load dominates inference at this volume, so holding both
resident (`keep_warm`) removes roughly a minute per job.

**`keep_warm` defaults to `false` (D22).** The latency argument holds only on a
GPU that is otherwise idle. On the reference desktop (RTX 3070, 8 GB) resident
models would hold ~6 GB around the clock for a workload that is busy about four
minutes a day, leaving nothing for games, other local models or video work. The
pipeline is asynchronous and spooled, so nobody waits on the minute it costs.
Set `keep_warm = true` only on a machine dedicated to transcription, where the
VRAM has no competing claim.

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
unknown-token list, human-reviewed once, then topped up after each run.

#### 7.3.1 `glossary.toml`

The output is a single file at `~/munin/glossary.toml` that the user owns and
can edit by hand. `munin glossary build` regenerates it, but any entry a human
has touched is marked `reviewed = true` and is never overwritten — the generator
adds and proposes, it does not overrule.

A complete annotated example is in
[`examples/glossary.toml`](../../../examples/glossary.toml), built to mirror the
damage classes documented in Appendix B. The schema:

| Key | On | Meaning |
|---|---|---|
| `canonical` | `[[term]]` | The correct spelling. This is what goes in the prompt and what appears in transcripts |
| `kind` | `[[term]]` | `product`, `customer`, `person`, `standard`, `domain`, `tool` |
| `weight` | `[[term]]` | 0-100. Decides who makes the `prompt_budget_terms` cut when the budget is tight |
| `languages` | `[[term]]` | Which language routes bias this term (§7.1) |
| `corrections` | `[[term]]` | Wrong forms observed in the corpus, mapped to `canonical` |
| `action` | `[[term]]` | `always` replace, `review` by LLM, or `never` — record only |
| `domains` | `[[term]]` | Attendee email domains that promote this term |
| `email` | `[[term]]` | For `kind = "person"`; also feeds tier 3 speaker assignment (§7.4) |
| `text` | `[[protect]]` | A string that is never rewritten and never offered as a correction target |
| `when_attendee_domain` / `when_title_matches` | `[[context]]` | Match condition for promoting terms into this meeting's prompt |
| `promote` | `[[context]]` | Canonical terms to move to the front of the queue when the condition matches |

Three parts of that schema exist because blind replacement is dangerous:

- **`action = "review"`** for corrections that are also real words. `ISO 2700`
  may be a truncated `ISO 27001` or a mangled `ISO 42001`; a deterministic rule
  cannot tell, and guessing wrong in an audit transcript is worse than leaving
  it.
- **`[[protect]]`** for correct-but-unusual words that a rule would otherwise
  swallow. The surname `Havik` sits one edit away from `avvik`, which has 53
  corrections attached to it.
- **`[[context]]`** so the 40-term budget is spent on the customer actually in
  the meeting rather than split across every customer in the register.

`munin glossary` subcommands:

```
munin glossary build            # regenerate, preserving reviewed entries
munin glossary review           # walk unreviewed proposals, one by one
munin glossary lint             # unreachable rules, collisions, protect conflicts
munin glossary test <session>   # show what would change in an existing transcript
```

`lint` is the one to run after hand-editing: it catches a `corrections` entry
that is also a `canonical` elsewhere, a `[[protect]]` string that some rule
would have rewritten, and rules that can never fire because an earlier one
consumes the same text.

**Injection is two-stage, because `initial_prompt` is capped at 224 tokens**
(half of Whisper's 448-token decoder context; anything longer is silently
truncated):

1. **Decode-time bias**: ~40 terms selected per meeting from calendar context.
   An invite naming a customer promotes that customer's terms.
2. **Post-correction**: the full mapping applied to the output text afterwards,
   deterministic replacements first, then an LLM review pass for the rest.

Stage 2 is where the acceptance criteria in §13 are actually met.

### 7.4 Speaker attribution

Four tiers, in order. Each is only consulted when the one above it does not
answer.

| Tier | Method | Error |
|---|---|---|
| 1 · You | Energy on track 1 and not on track 2 | None. True by construction — no model involved |
| 2 · Enrolled | Embedding match against the voice register, above threshold | Low and measurable; confidence is stored per segment |
| 3 · Invited | Closed-set assignment against the calendar attendee list | Moderate. "Which of these four", not "who is this" |
| 4 · Unknown | Positional label kept: `SPEAKER_02` | Honest. Better a positional label than a confident wrong name |

Digital-only capture helps tiers 2 and 3: Teams audio is per-participant headset
mics, already noise-suppressed and echo-cancelled upstream. That is the
AMI-headset acoustic condition (pyannote ~12-14% DER) rather than the far-field
single-room-mic condition where DER degrades badly.

**The voice register** (§7.4.1) is what makes tier 2 possible, and is the reason
a speaker keeps the same name in an ad-hoc call with no invite, in a meeting
where the attendee list is wrong, and in a transcript written six months later.

#### 7.4.1 Voice register

A profile is a name and an embedding. Nothing else is required and nothing else
is stored:

| Field | Holds |
|---|---|
| `name` | What appears in transcripts |
| `email` | Optional; links to the M365 identity for invite matching |
| `embedding` | Centroid plus per-sample vectors |
| `model` | e.g. `ecapa-tdnn@1.0` — embeddings mean nothing to a different model |
| `samples` | The enrolment clips and which sessions they came from |
| `accuracy` | Rolling mean confidence over recent matches |
| `status` | `active` or `disabled` |

Stored as one JSON file per person under `~/munin/voices/`, local to the machine.

**Naming and enrolling are separate actions** (D17). Naming assigns a label to
one transcript and stores nothing. Enrolling writes a profile so the person is
recognised from then on. Keeping them apart is what keeps the register small
enough to stay accurate — every profile is another candidate the matcher must
discriminate against, so six good profiles beat sixty thin ones. `accuracy` is
the field that surfaces this: a profile built from one short clip will quietly
mislabel people, and a rolling 0.68 says so before the transcripts do.

**Retain the enrolment clips, not only the centroid.** Changing the embedding
model invalidates every profile at once; retained clips turn that from
re-enrolling everyone into `munin voice reembed --all`.

Enrolment happens from the review queue (§9.5), where a cluster has just been
identified — which is also the best available sample. Nothing is enrolled
automatically, and a cluster under roughly 30 seconds offers no enrolment at all,
being too thin to build a profile that matches reliably.
### 7.5 Output and storage

All Munin data lives under `~/munin/` (D18). One named root, so a directory in
`$HOME` says what it is and what put it there.

```
~/munin/
├── recordings/2026/09/2026-09-14T1325-weekly-quality-sync/
│   ├── session.json      state, title, source, calendar id, segments, checksums
│   ├── mic.opus          track 1 — you, 24 kbps mono
│   ├── app.opus          track 2 — everyone else
│   ├── mixed.mp3         64 kbps, for manual Plaud upload (§10)
│   ├── transcript.txt    [HH:MM:SS - HH:MM:SS] Speaker: text
│   ├── transcript.json   word timings, confidences, speaker turns
│   └── markers.json      anything marked during the meeting
├── by-title/             symlinks, regenerated on write
├── by-participant/       symlinks, calendar-matched sessions only
├── inbox/                captured, not yet transcribed
├── voices/               the register (§7.4.1), plus samples/
├── config.toml           backends, detection, paths (§8)
├── glossary.toml         domain vocabulary and corrections (§7.3.1)
├── munin.log
└── latest -> recordings/2026/09/…
```

Two properties are the point: the directory name alone says when and what without
opening anything, and `grep -r` searches every transcript with no index to
maintain. `session.json` is the only file the worker writes state into, so a
half-finished session is always identifiable after a crash.

**The session directory is the only place a transcript is written (D24).**
There is no second copy. An earlier draft mirrored every transcript into
`~/pensieve/raw/`; that is removed, because `pensieve` is a personal vault and
these transcripts are company records (§12). Anything that wants a flat list
reads the tree, which `grep -r` already searches with no index to maintain.

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
  in April files and the full display name from May onward; munin uses the voice
  register (§7.4.1) first and the calendar display name second.

### 7.6 Microsoft 365 enrichment

The calendar never starts a recording (D4). It answers what a recording cannot
answer for itself.

| Pulled | From | Used for |
|---|---|---|
| Subject, start, end, organiser | Outlook calendar event | Session title and directory name, replacing the timestamp fallback |
| Attendees and response status | Outlook calendar event | The closed set for tier 3 assignment (§7.4) |
| Body / agenda text | Outlook calendar event | Decode-time glossary bias (§7.3 stage 1). An invite naming a customer promotes that customer's vocabulary |
| Recurring series id | Outlook calendar event | Makes "never record this meeting" stick to the series rather than one instance |
| Actual join / leave times | Teams attendance report | Narrows the candidate set per segment rather than per meeting |

Sessions match events by time overlap. **The attendance report is an upgrade, not
a dependency:** Graph gates online-meeting artifacts behind tenant-admin consent
rather than user consent, so assume the invite list is what you get and request
the report separately. This answers open question 4.
## 8. Transcription backends

Three backends satisfy one interface, ordered into a fallback chain in
`~/munin/config.toml` (D19). The recorder is identical in all three.

| Backend | Runs on | 60-min meeting | Suits |
|---|---|---|---|
| `local` | This machine — faster-whisper on CUDA, whisper.cpp on Vulkan | ~5-8 min on a 3070, ~30-40 min on an Intel Arc iGPU (est.) | The desktop. No network, no dependency on another machine being awake |
| `ssh` | Another machine you own; audio pushed, transcript pulled back | ~5-8 min plus ~20 s transfer (est.) | The mini PC records, the desktop transcribes. 11 MB of Opus makes transfer irrelevant |
| `api` | Any OpenAI-audio-compatible endpoint, gateway or hosted | ~5-6 min (est.) | A shared cluster, or borrowing capacity you do not own |

```toml
[transcribe]
backend  = "local"
fallback = ["ssh", "api"]     # tried in order; session stays spooled if none answer

[transcribe.local]
device = "cuda"               # cuda | vulkan | cpu
model_no = "NbAiLab/nb-whisper-large"
model_en = "openai/whisper-large-v3"
keep_warm = false             # D22: VRAM free between jobs; costs ~1 min load per job
defer_when_busy = true        # D22: wait for the GPU rather than compete with it
busy_vram_free_mb = 7000      # below this much free VRAM, the session stays spooled

[transcribe.ssh]
host = "desktop.lan"
remote_bin = "~/.local/bin/munin"
transport = "rsync"

[transcribe.api]
endpoint = "https://gateway.internal/v1/audio/transcriptions"
api_key_cmd = "secret-tool lookup service munin-api"   # never the key itself

[diarize]
device = "cuda"               # always local, whatever the ASR backend
```

CTranslate2 has no Metal backend (`device="mps"` raises `ValueError: unsupported
device mps`), so an Apple-silicon machine is reached as `api` (whisper.cpp Metal
or mlx-whisper behind a gateway) rather than as `local`.

**Order of implementation: `local` first**, because it is unblocked. `ssh` next,
because it is the availability floor — until it exists, every meeting depends on
one desktop being powered on. `api` last, when the cluster proposal is approved
(§12).

Load is trivially small in all cases: median meeting 28 minutes, roughly 1.4
meetings per day across the existing corpus.

### 8.1 The API backend splits the pipeline

The OpenAI audio API returns text and optionally word timestamps. It has no
speaker field, so diarization and voice matching cannot live behind a gateway and
**always run locally**, whatever produced the words. This is acceptable because
diarization is the cheaper half and track 1 already provides self-attribution for
free (§7.4) — but it means `api` is not a way to run Munin on a machine with no
compute at all.

Two capabilities decide whether a given endpoint is usable, and `munin doctor`
(§15) tests both against a fixture rather than against a meeting:

- Does it pass through **`prompt`**? Required for §7.3 stage 1. Without it,
  glossary biasing is lost and only post-correction remains.
- Does it pass through **`timestamp_granularities[]=word`** with
  `response_format=verbose_json`? Required to align ASR output with diarization
  turns.

If either is missing, the fallback is a direct connection to the whisper server
behind the gateway, bypassing it for this one workload.
### 8.2 Sharing the GPU with the user

The `local` backend runs on a machine that is also driving a compositor, a
browser and sometimes a game. At the reference 3070 rate a median meeting is
~3-4 minutes of saturated GPU, so the contention window is small, but it is
unpredictable and lands whenever a meeting ends. Two settings keep Munin from
being the process that ruins an evening (D22):

- `keep_warm = false` holds VRAM only while a job runs, not around the clock.
- `defer_when_busy = true` makes the worker check free VRAM before claiming a
  session, and leave it spooled if the card is committed elsewhere. Deferral is
  not a failure: §11 already retries with backoff, and the design principle is
  that transcripts arrive late, never missing.

Capture is unaffected either way. `munin-rec` never transcribes (D20) and never
touches the GPU, so a saturated card cannot cost a recording.

`[diarize]` is always local (D9), so `ssh` and `api` still put pyannote on the
local GPU for about a minute per session. Offloading ASR reduces the window, it
does not remove it.

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

### 9.2 The shell plugin

**Open question 5 is answered.** Omarchy 4 has a real plugin system, not an
undocumented `plugins` array. Plugins live in `~/.config/omarchy/plugins/<id>/`
as a `manifest.json` plus QML, are discovered at startup, hot-reload on save, and
are validated with `omarchy plugin validate <folder>`. Declared `kinds` include
`bar-widget`, `service`, `panel`, `overlay` and `bar`.

Munin ships one plugin, `local.munin`, with two kinds:

```
~/.config/omarchy/plugins/local.munin/
├── manifest.json     schemaVersion 1, kinds: ["service", "bar-widget"]
├── Service.qml       PipeWire watch, window titles, notifications, IPC
├── BarWidget.qml     extends qs.Ui Panel — bar button, dropdown, IPC target
├── Model.js          state machine, session list, formatting
└── components/       TrackMeter.qml, SessionRow.qml
```

Three findings shape this:

- **`Quickshell.Services.Pipewire` is available inside QML** — nodes, streams,
  `isSink`, `isStream`. Detection (§6.3) needs no `pw-dump` polling.
- **`qs.Ui`'s `Panel` base** gives the bar button, the popup lifecycle and an
  `IpcHandler` with `open`/`close`/`toggle` for free. `omarchy.audio` is the
  model to follow; `PanelHero`, `PanelSectionHeader`, `PanelSlider` and
  `PanelSeparator` are the house components.
- **`bar/indicators/ScreenRecording.qml` is the precedent** for a recording
  indicator, down to the glyph (`󰻂`, U+F0EC2) and the process-probe idiom.

**The plugin never captures** (D20). A shell hot-reload or crash must never kill
a recording, so the plugin detects, prompts and renders while `munin-rec`
captures.

Placement: `omarchy bar put local.munin --before omarchy.tray`, which writes
`shell.json` and hot-reloads. Note that `shell.json` is deployed as a copy, never
a symlink, because `omarchy-shell-config` writes with an atomic rename.

States rendered, one at a time:

| State | Shows | Meaning |
|---|---|---|
| idle | (hidden) | No call |
| detected | dim mic glyph | A call is live, Munin is not recording |
| recording | red dot, pulsing, elapsed | Capturing |
| ending | red dot, steady, elapsed | Streams gone, grace period running |
| transcribing | ⟳ with queue depth | Captured and safe; worker running or waiting |
| done | ✓ for 30 s | Transcript landed |
| failed | ! until acknowledged | Audio retained; a failure that hides itself is a lost meeting |
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

### 9.5 Admin surface

A local tool, `munin admin`, bound to loopback. The bar panel links to it and
never embeds it: a register and a fallback chain want a table, not a dropdown.

| Tab | Holds |
|---|---|
| Voices | The register (§7.4.1): person, status, samples, match confidence, meetings heard in, last heard. Add sample, rename, disable, re-embed, delete |
| Review | Unnamed diarization clusters from recent sessions, with enough audio and text to recognise who it was. Naming one re-renders that transcript in place |
| Sessions | Every session, its state, and a retry for anything failed |
| Transcription | The backends (§8), their order, reachability, and a Test that runs a ten-second fixture end to end |
| Settings | Detection matches, grace period, resume window, paths |

The same actions exist on the CLI, and the CLI is what the keybind, the
notification buttons and the panel all call:

```
munin voice list
munin voice enrol "Ola Nordmann" --from <session> --speaker 2
munin voice add-sample ola --from <session> --speaker 1
munin voice rename ola "Ola Nordmann"
munin voice disable ola
munin voice delete ola
munin voice reembed --all
munin review
munin glossary build | review | lint | test <session>
```

`munin voice reembed --all` is why enrolment clips are retained (§7.4.1): a
change of embedding model otherwise means re-enrolling every person by hand.


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

Exported filenames carry the session directory name, so a file in the upload
folder pairs unambiguously with the session that produced it.
Upload state lives in session metadata, so the outstanding set is always
queryable. The upload itself is a manual drag into Plaud's web importer; munin
does not automate it (Appendix A).

## 11. Failure handling

The design principle is that transcripts arrive late, never missing.

| Failure | Behaviour |
|---|---|
| No sink reachable | Session stays in spool, retried with backoff. Nothing is lost. |
| GPU busy or out of VRAM | Session stays spooled and is retried once the card frees up (`defer_when_busy`, §8.2). A CUDA OOM mid-job is treated as an unreachable sink, not a worker crash. |
| Worker crashes mid-file | Session state is `transcribing`; reset to `pending` on worker start. |
| Meeting runs past scheduled end | Capture continues until the application stream has been silent for 2 minutes. |
| Calendar unreachable | Ad-hoc mode: capture still triggers on application audio, title falls back to timestamp. |
| Disk fills | Refuse to start a new capture below a configured threshold and alert, rather than truncating. |
| Duplicate capture (two machines in the same meeting) | Sessions carry the calendar event ID; the worker keeps the longest and discards the rest. |

Spool sessions are never deleted by the worker, only marked `done`. A separate
retention pass handles cleanup (§10).

## 12. Compliance and privacy

This records identifiable people, including customers, in a regulated context.
Flagging explicitly rather than burying it:

- **Audio and transcripts are personal data under GDPR.** Self-hosting improves
  the story — no external processor, nothing leaving the machine — but does not
  remove the obligation. A basis and a retention period are needed, not implied.
- **The voice register is local and minimal by design** (§7.4.1). It holds a name
  and an embedding, on this machine, and nothing is transferred. Under EU and
  Norwegian law a stored voiceprint used to identify a person is a special
  category of personal data, so the register is worth naming explicitly in
  whatever record of processing covers this tool, and worth keeping small — which
  D17 already pushes towards for accuracy reasons.
- **Retention is the open decision, not collection.** Transcripts are small and
  worth keeping; audio is ~11 MB an hour and its value drops sharply once the
  transcript is verified. A default of audio 90 days, transcripts indefinitely
  keeps the store bounded without losing anything reached for in practice.
- **Recording is always confirmed** (D4) and **always visible** (D10), and
  auto-stop announces itself (D14). That combination is what makes the tool
  defensible to the people in the meeting.
- **The internal cluster proposal scopes testing to synthetic data only, no real
  customer content.** The `api` backend sits outside that scope and needs its own
  section in that proposal, not a footnote.
- **ISO 27001.** A transcript store accumulating customer commercial detail is a
  new asset with its own access control and retention requirements.
- **Personal vault, work content.** Settled by D24. `pensieve` is personal and
  transcripts of customer meetings are company records, so Munin no longer
  writes into it. The authoritative copy is the session directory under
  `~/munin/`, which is the store that ISO 27001 access control and retention
  apply to. One store, one retention policy, one place to answer a deletion
  request from.


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
  transcript is written into the session directory and nowhere else.
- **Backends**: the same fixture through `local`, `ssh` and `api` must produce
  identical text.
- **Detection**: native app, PWA and browser tab each trigger; a browser playing
  audio with no microphone does not; a Teams tab with no call does not.
- **Voices**: precision and recall against hand-labelled references. A thin
  profile must degrade to tier 3, never mislabel.

## 14. Open questions

Answered since the first draft, by reading the machine (Appendix D):

- ~~5. What does `shell.json`'s `plugins` array accept?~~ Omarchy 4 has a full
  plugin system with a manifest schema and `omarchy plugin validate` (§9.2).
- ~~6. Does the notification daemon support actions?~~ Yes.
  `notifications/Service.qml` sets `actionsSupported: true`.
- ~~4. Is a Teams-native attendee roster available?~~ Yes, as an attendance
  report, but usually gated behind tenant-admin consent (§7.6).
- ~~7. Is `SUPER + SHIFT + R` free?~~ Yes, confirmed by the user.
- ~~13. Should Munin build on `voxtype meeting`?~~ No. Decided: Munin implements
  its own capture (D21). `voxtype meeting` covers two-source capture, echo
  cancel, source-based and ECAPA-TDNN diarization, and local/remote whisper
  modes — but it is Linux-only, and depending on it would put the least portable
  part of Munin on a foundation that cannot cross to macOS or Windows. One idea
  worth taking from it regardless: GTCRN enhancement plus transcript dedup on
  the mic track, which is not otherwise in this spec.
- ~~8. Microphone contention with `voxtype`?~~ No contention. `voxtype` does not
  hold the microphone persistently — the default source sits `SUSPENDED` with
  zero capture streams until push-to-talk is pressed, capped at 60 s
  (`max_duration_secs`). Two concurrent readers on the same source were verified
  working, each negotiating its own format (44.1 kHz stereo and 16 kHz mono off
  one 16 kHz mono device), both receiving audio, no errors. See Appendix D.
- ~~1. Where does the authoritative transcript live once work meetings are
  involved?~~ The session directory under `~/munin/`, and nowhere else (D24).
  The `pensieve` copy is removed. Retention design now has a single store to
  apply to, which is what made this block M12.

Still open:

2. Can the home desktop reach the shared cluster, or is that office-network
   only? Determines whether the `api` backend is usable from home.
3. Cluster node specs are still unknown, pending a hardware scoping session.
7. Does `o.bind` expose a raw `exec` action in Omarchy 4? The keybind itself is
   free, but the action form is still unverified.
8. **`voxtype` pauses MPRIS players while recording** (`pause_media = true`).
   Chromium registers an MPRIS instance per tab playing media, so pressing
   push-to-talk during a browser-tab Teams call may pause the meeting audio.
   Needs testing against a live call; the mitigation is `pause_media = false`.

9. Does the LiteLLM gateway pass through `prompt` and word timestamp
   granularity (§8.1)? `munin doctor` tests this once the backend is configured.
10. How does the `ssh` backend authenticate unattended? The worker runs from a
    systemd user unit with no terminal, so a passphrase-protected key needs an
    already-unlocked agent, or a dedicated command-restricted key with no
    passphrase. The second is less elegant and more likely to still work in a
    year.
11. Does deleting a voice profile also unname that person in transcripts already
    written? Defensible either way; decide once and write it down.
12. Spool transport for the `ssh` backend — rsync over ssh is assumed, but NFS
    and a pull model have different failure modes under §11.

## 15. Install and setup

Installing should not require reading any of the above.

```
git clone <repo> ~/dev/munin && ~/dev/munin/install.sh
```

| # | Step | Touches |
|---|---|---|
| 1 | Check `ffmpeg`, `pipewire`, Python 3.12+, `uv`; offer `omarchy pkg add` for anything missing | nothing yet |
| 2 | Install CLI, daemon and worker | `~/.local/bin/munin*` |
| 3 | Install and validate the shell plugin | `~/.config/omarchy/plugins/local.munin/` |
| 4 | Put the widget in the bar | `omarchy bar put local.munin --before omarchy.tray` |
| 5 | Bind the key, unbinding first if Omarchy owns it | `~/.config/hypr/bindings.lua`, backed up first |
| 6 | Copy the systemd user unit, print the enable command rather than running it | `~/.config/systemd/user/munin.service` |
| 7 | Create the data root and a default config | `~/munin/`, `~/munin/config.toml` |

**Models are not downloaded by the installer.** `munin models pull` is a separate,
explicit 6.2 GB. An installer that silently spends that much of someone's
connection is one people stop trusting, and the CLI works without it against a
remote backend.

`munin setup` then asks the four questions that have no sensible default:
transcription backend (and offers the model download), Microsoft 365 sign-in via
device code, which microphone, and a ten-second test recording played back to
confirm track separation.

`munin doctor` re-runs every check and is **worth building first**. Every open
question above ends up as a line in its output — mic contention with `voxtype`,
whether the gateway honours `prompt`, whether attendance reports are granted,
free disk, token expiry, which backends are reachable. It turns "is this set up
correctly" into one command, and makes a broken install self-describing when
someone returns to it in six months.

`install.sh --uninstall` removes the binaries, plugin, bar entry, keybind and
unit, and leaves `~/munin/` where it is, printing the path. Nothing recorded is
ever removed by an uninstaller.


## 16. Portability

Linux now, macOS next, Windows after (D21). The cost of that ordering is paid
once, at the capture boundary, and only if the boundary stays narrow.

### 16.1 What ports and what does not

| Portable — written once | Per platform |
|---|---|
| Session format, `~/munin/`, spool and state machine | Capture: two independent streams |
| Pipeline: language routing, ASR, diarization, voice register, glossary, render | Detection: is a call live, and is it Teams |
| Backends: `local`, `ssh`, `api` | Status indicator |
| M365 enrichment | Notifications with actions |
| CLI, `munin setup`, `munin doctor` | Idle inhibit |
| `munin admin` — a loopback web UI, identical everywhere | |

The admin surface being a local web app rather than a native one is worth
keeping for this reason alone: the register, review queue, session list and
backend configuration are the largest UI in the project, and they port for free.

**The rule:** nothing above `capture/` and `detect/` may reference a platform.
If the pipeline ever needs to know it is on Linux, the interface is wrong.

### 16.2 Capture, per platform

| | Linux | macOS | Windows |
|---|---|---|---|
| Track 1 — you | PipeWire default source | CoreAudio input device | WASAPI capture endpoint |
| Track 2 — them | PipeWire `.monitor` bound to the app's sink-input | ScreenCaptureKit per-application audio (macOS 13+) | WASAPI process loopback |
| Fallback | — | BlackHole aggregate device | System loopback, then filter |
| Permission | none | Screen Recording **and** Microphone (TCC) | Microphone |
| Phase | 1 | 2 | 3 |

Windows is the best-matched of the three: process loopback binds to a single
process, which is exactly what D2 asks for. macOS is the worst — the
screen-recording permission is a surprising thing to be asked for in order to
record audio, and it needs explaining in `munin setup` rather than arriving as a
bare system prompt.

### 16.3 Detection, per platform

The two conditions in §6.3 stay the same everywhere; only their evidence changes.

| | Linux | macOS | Windows |
|---|---|---|---|
| A call is live | PipeWire: one process holds playback + capture | CoreAudio process audio state | WASAPI session state per process |
| It is Teams | PipeWire client, else Hyprland window class/title | Bundle id, else window title | Process `ms-teams.exe`, else window title |

**Detection is simpler off Linux**, because Teams ships a native client on both.
The three-shape problem in §6.3 exists because this machine runs Teams as a
browser tab; elsewhere the process name settles it, and the window-title path is
needed only for the browser case.

### 16.4 Status indicator, per platform

| Linux | macOS | Windows |
|---|---|---|
| Omarchy Quickshell plugin (§9.2) | Menu-bar extra | Tray icon |
| Notification actions via the Omarchy daemon | `UNUserNotificationCenter` | Toast notifications with actions |

All three need the same states and the same actions, so the indicator is a thin
shim over `munin status` in every case. The dropdown panel is the part that does
not port: on macOS and Windows the equivalent is opening `munin admin`, which is
one menu item rather than a second implementation.

### 16.5 Consequence for phase 1

Phase 1 must not accumulate Linux assumptions above the boundary. Concretely:

- `capture/base.py` is written before `capture/linux.py`, not extracted from it
  afterwards.
- `detect/base.py` likewise — the two-condition structure in §6.3 *is* the
  interface, and per-platform code only supplies evidence for each condition.
- `session.json` records which platform and capture method produced it, so a
  transcript stays reproducible on a machine that could not have recorded it.
- `munin doctor` has a per-platform check table from the start, even while only
  one column is populated.

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

## Appendix D: Omarchy conventions (verified on the machine, 2026-09-14)

Omarchy 4.0.0.alpha. Read from `~/dev/dotfiles/omarchy` and confirmed against the
running system.

**Verified:**

- **The shell has a real plugin system.** `~/.config/omarchy/plugins/<id>/` with
  `manifest.json` + QML, discovered at startup, hot-reloading on save, validated
  by `omarchy plugin validate`. Kinds: `bar-widget`, `service`, `panel`,
  `overlay`, `menu`, `bar`. Managed with `omarchy plugin add|clone|enable|list`.
  Built-in widgets are cloned with `omarchy plugin clone <id>`, never edited in
  `/usr/share/omarchy/`.
- **Notification actions are supported.** `notifications/Service.qml` sets
  `actionsSupported: true`.
- **`Quickshell.Services.Pipewire` is usable from plugin QML** — nodes, streams,
  `isSink`, `isStream`. No `pw-dump` polling needed.
- **`qs.Ui` provides the panel furniture**: `Panel` (bar button + popup + an
  `IpcHandler` with open/close/toggle), `BarIndicator`, `BarWidget`, `PanelHero`,
  `PanelSectionHeader`, `PanelSlider`, `PanelSeparator`, `PanelActionButton`.
  `omarchy.audio` is the reference implementation for a bar widget with a
  dropdown.
- **`bar/indicators/ScreenRecording.qml` is the precedent** for a recording
  indicator, including the `󰻂` glyph and the `bar.run(...)` action idiom.
- **Style tokens** in `Commons/Style.qml`: `cornerRadius: 0`, border alpha 0.4,
  fill alphas 0.04 / 0.08 / 0.18 for normal / hover / selected.
- Bar is Quickshell, not Waybar. Layout in the `bar` subtree of
  `~/.config/omarchy/shell.json`, managed with `omarchy bar put|move|set`.
- `shell.json` is deployed as a **copy**, never a symlink, because
  `omarchy-shell-config` writes with `jq > tmp; mv tmp` and the atomic rename
  would replace a symlink with a regular file.
- Notifications go through `omarchy-notification-send -g <glyph> "<msg>"`.
- Keybindings: `o.bind(keys, description, action_table)` in
  `~/.config/hypr/bindings.lua`, loaded after Omarchy defaults, so
  `hl.unbind(...)` first for any key Omarchy owns.
- Hyprland config is Lua with no build step; `hyprctl reload` then
  `hyprctl configerrors` is the apply path.
- Daemons: systemd `--user` units, copied into place, enabled by hand, never
  committed as `*.target.wants/` symlinks. `voxtype.service` is the template.
- **PipeWire allows concurrent capture on one source.** Verified on the
  microphone (`POROSVOC PNC201 4MIC`, `s16le 1ch 16000Hz`): two readers attached
  to the same source simultaneously, each negotiating its own format, both
  receiving audio, source `RUNNING`, no errors. `voxtype` holds the microphone
  only during push-to-talk, so Munin's long-lived capture and voxtype's bursts
  coexist. Open question 8 is closed.
- **PipeWire needs `XDG_RUNTIME_DIR`.** Without it `pactl` and `pw-dump` return
  nothing at all rather than failing loudly. The systemd unit in §9.1 already
  sets `Environment=XDG_RUNTIME_DIR=%t`; anything else calling PipeWire from a
  non-session context needs it too.
- `~/.local/bin` for scripts, but the graphical-session PATH puts
  `/usr/share/omarchy/bin` first, so `omarchy-*` names cannot be shadowed from a
  user directory (D12).
- `omarchy refresh` uses `cp -f`, which writes through symlinks and will
  overwrite dotfiles repo contents with Omarchy defaults.
- Idle: `shell.json` sets `screensaver: 150`, `lock: 300`.
- **No Teams client is installed.** Only Chromium, with Chrome desktop entries
  present. Teams runs as a browser tab today, which is what drives the
  three-shape detection in §6.3.

**Not verified, must still be checked:**

- Whether `o.bind` has a raw `exec` action in Omarchy 4 (open question 7).
  `SUPER + SHIFT + R` itself is free — confirmed by the user.
- Whether the LiteLLM gateway passes `prompt` and word timestamp granularity
  (open question 9).
