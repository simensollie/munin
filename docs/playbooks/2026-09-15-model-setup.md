# Playbook: model setup on the transcription desktop

Manual stand-in for `munin models pull` (spec §15), which does not exist yet.
Run it once on the machine that will hold the `local` backend. Its byproduct is
the specification for that command: every step below is something `munin models
pull` and `munin doctor` will eventually do without being asked.

**Target:** Arch / Omarchy desktop, NVIDIA RTX 3070 (8 GB), driver already
installed. Every step verifies before it changes anything, so it is safe to run
on a working desktop.

On the `poc` branch `src/munin/pipeline/asr.py` and `pipeline/diarize.py` are
stubs that raise `NotImplementedError`. This playbook sets up what they will
load; it does not make them work.

**Time:** about 30 minutes, most of it download.
**Disk:** ~7.2 GB final, with a transient peak near 13 GB during step 5.

Facts here were verified against upstream on 2026-09-15 and are cited. Anything
that could not be verified is marked **UNVERIFIED** and is a thing to measure,
not a thing to trust.

---

## Step 0. Verify the GPU stack

No CUDA toolkit is needed. faster-whisper talks to the driver through
CTranslate2 and gets cuBLAS and cuDNN from Python wheels, so the only system
requirement is the driver itself.

```bash
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
```

Expect one row naming the 3070 with 8192 MiB. Requirements:

| Need | Why |
|---|---|
| Driver new enough for CUDA 12 | CTranslate2 4.x dropped CUDA 11 |
| `ffmpeg` on PATH | Hard prerequisite of pyannote.audio 4.x, which decodes audio through torchcodec |
| `uv` | Spec §15 step 1 |

```bash
pacman -Qs 'nvidia-(open-)?dkms|nvidia-utils' | head
command -v ffmpeg uv
```

Install only what is missing:

```bash
omarchy pkg add ffmpeg uv        # or: sudo pacman -S ffmpeg uv
```

If `nvidia-smi` is absent, stop and sort the driver out first. That is a reboot
and outside the scope of this playbook.

## Step 1. Create the environment

Models are Munin data, so they live under `~/munin/` with everything else (D18).

```bash
mkdir -p ~/munin/models
cd ~/munin
uv venv --python 3.12 ~/munin/.venv
source ~/munin/.venv/bin/activate
```

Python 3.12: pyannote.audio 4.x requires `>=3.10`, faster-whisper requires
`>=3.9`, and spec §15 asks for 3.12+. 3.12 satisfies all three and is what
torch wheels are best tested against.

**This venv is the backend's, not Munin's.** `install.sh` builds its own venv at
`~/.local/share/munin/venv` from the system `python3`, which on the reference
desktop is 3.14. Munin declares no Python dependencies, so 3.14 is fine for it
and unusable for the pinned model set. The two never merge: under D23 the
`local` backend invokes `~/munin/.venv/bin/python` as a subprocess rather than
importing `faster_whisper` into the worker. Install Munin first; it creates the
`~/munin/` root this playbook writes into, and it fails in seconds rather than
after a 30-minute download.

## Step 2. Install the stack

Order matters. Install pyannote first, because it pulls torch, and recent torch
cu12 wheels already bundle cuDNN 9. Installing the standalone NVIDIA wheels
first can leave two copies of cuDNN on the library path.

```bash
uv pip install 'pyannote.audio==4.0.7' 'faster-whisper==1.2.1'
```

Versions verified current on 2026-09-15: pyannote.audio 4.0.7 (released
2026-06-30), faster-whisper 1.2.1 (released 2025-10-31, pinning
`ctranslate2>=4.0,<5`).

Do not mix a system torch with pip's torchcodec. pyannote pinned
torch/torchaudio/torchcodec as a matched set from 4.0.2 onward specifically to
avoid segfaults from a mismatch.

Now check whether CTranslate2 can already see cuDNN:

```bash
python - <<'PY'
from faster_whisper import WhisperModel
m = WhisperModel("tiny", device="cuda", compute_type="float16")
print("cuda ok:", next(m.transcribe(__import__("numpy").zeros(16000, dtype="float32"))[0], None) is None)
PY
```

**If that raises a cuDNN or cuBLAS loading error**, and only then, add the
standalone wheels and put them on the library path:

```bash
uv pip install nvidia-cublas-cu12 'nvidia-cudnn-cu12==9.*'
export LD_LIBRARY_PATH=$(python -c 'import os, nvidia.cublas.lib, nvidia.cudnn.lib; print(os.path.dirname(nvidia.cublas.lib.__file__) + ":" + os.path.dirname(nvidia.cudnn.lib.__file__))')
```

`LD_LIBRARY_PATH` must be set **before** the process starts, not from inside
it, so never an `os.environ[...]` assignment in the process that needs it.
Under D23 the process that needs it is the backend subprocess, not
`munin-work`, so the `local` backend sets it in the child's environment when it
spawns. That is better than an `Environment=` line in the systemd unit, which
would put a cuDNN path on the library path of a worker that never loads cuDNN.

## Step 3. Hugging Face cache and token

```bash
export HF_HOME=~/munin/models
uv pip install 'huggingface_hub[cli]'
hf auth login          # paste a token with read scope
```

Put `HF_HOME` in the config, not just the shell, or the worker will download
everything a second time into `~/.cache/huggingface`.

The diarization model is gated. Open its page while signed in and accept the
conditions:

- https://huggingface.co/pyannote/speaker-diarization-community-1

Acceptance is auto-approved, so there is no review wait. The embedding model it
uses internally (`pyannote/wespeaker-voxceleb-resnet34-LM`) is **not** gated, so
this is the only acceptance needed.

## Step 4. English ASR

```bash
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cuda', compute_type='float16')"
```

That resolves to `Systran/faster-whisper-large-v3`, the canonical pre-converted
CTranslate2 build: 3.09 GB, already float16, verified present and current.

## Step 5. Norwegian ASR

`NbAiLab/nb-whisper-large` ships CTranslate2 weights at the repo root, so
faster-whisper can load it by name with no conversion. Two caveats decide the
approach:

- The shipped build is **float32, 6.17 GB**. There is no `--quantization` flag
  in NB's export script.
- The CT2 files are **undocumented**. The model card describes ggml, ONNX,
  PyTorch, Flax and TF, and never mentions CTranslate2 or faster-whisper. They
  could disappear in a future revision without notice.

Convert locally to float16. It halves disk, halves cold-start load time (which
now matters, because `keep_warm` defaults to `false` under D22), and leaves you
holding a build that cannot be pulled out from under you.

```bash
ct2-transformers-converter \
  --model NbAiLab/nb-whisper-large \
  --output_dir ~/munin/models/nb-whisper-large-ct2-fp16 \
  --copy_files tokenizer.json preprocessor_config.json \
  --quantization float16
```

Point the config at the directory rather than the repo id:

```toml
model_no = "~/munin/models/nb-whisper-large-ct2-fp16"
```

Conversion downloads the transformers-format weights (~6 GB) on top of what is
already cached, which is where the transient disk peak comes from. Clear it
afterwards with `hf cache delete`, or leave it if disk is not tight.

**Generation mismatch, worth knowing:** `nb-whisper-large` is built on
`openai/whisper-large` (v2 generation, 80-mel), not large-v3 (128-mel). The two
routed models are not siblings. This has no effect on correctness, since routing
is per file (§7.1) and each model gets its own preprocessor, but it does mean
per-model behaviour will differ in ways that show up in the M6 evaluation
harness.

## Step 6. Diarization

**Use `speaker-diarization-community-1`, not `3.1`.** Spec §7.2 names 3.1, which
upstream now labels `legacy`; its repo has not been touched since 2024-05-10.
community-1 was released 2025-09-29 alongside pyannote.audio 4.0.0 and is better
on every benchmark that matters here:

| Benchmark (DER) | 3.1 | community-1 |
|---|---|---|
| AMI IHM | 18.8 | **17.0** |
| AliMeeting | 24.5 | **20.3** |
| DIHARD 3 | 21.4 | **20.2** |
| CALLHOME | 28.5 | **26.7** |
| VoxConverse | 11.2 | 11.2 |
| REPERE | **7.9** | 8.9 |

AMI IHM is the row to read: §7.4 already places Munin in the AMI-headset
acoustic regime, so that is the closest published proxy for a digital meeting.

It also needs one gate acceptance instead of two, and it hands back speaker
embeddings for free (step 7).

```bash
python - <<'PY'
import torch
from pyannote.audio import Pipeline
p = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1")
p.to(torch.device("cuda"))
print("loaded")
PY
```

Two API changes from the 3.1 snippet that is still pasted all over the web:

- The argument is **`token=`**, not `use_auth_token=`. The old name was removed
  in 4.0.0, so the 3.1 model card's snippet fails outright on 4.x. Omitting it
  and letting `huggingface_hub` read `HF_TOKEN` works, though pyannote does not
  document that it does.
- `pipeline(...)` returns a `DiarizeOutput` dataclass, not an `Annotation`. Use
  `output.speaker_diarization` before `.itertracks(...)`. A `legacy=True`
  constructor flag restores the old return type.

Input is mono 16 kHz. Other rates and channel counts are downmixed and resampled
automatically, so `app.opus` can be handed over directly, but feeding 16 kHz
mono avoids the resample.

## Step 7. Voice register embeddings come free

Relevant to D16 and D17. On 4.x the diarization output carries
`speaker_embeddings`, a `(num_speakers, dimension)` array in
`output.speaker_diarization.labels()` order, already computed during clustering:

```python
for i, speaker in enumerate(output.speaker_diarization.labels()):
    embedding = output.speaker_embeddings[i]
```

That removes a separate embedding pass from the pipeline, and guarantees the
register lives in the same space as the clustering that produced the turns.

Two consequences for the spec:

- §7.4.1's `model` field (`ecapa-tdnn@1.0` in the current draft) should record
  the wespeaker model community-1 uses internally, not ECAPA. Mixing a
  speechbrain ECAPA embedding space with pyannote's wespeaker clustering means
  similarity thresholds do not transfer between them. Pick one space and stay
  in it.
- The embedding dimension is not stated on any model card. **UNVERIFIED**.
  Read it off the array at runtime before writing a schema that fixes it.

## Step 8. Smoke test on synthetic audio

Never use a real meeting for this. Record two people reading a scripted
exchange, or read both parts yourself with a pause between:

> **Ola Nordmann:** Vi må se på avviksmodulen før neste revisjon.
> **Kari Nordmann:** Enig. Jeg tar en gjennomgang av dokumentstyringen først.
> **Ola Nordmann:** Fine, let us switch to English for the integration part.

```bash
ffmpeg -f pulse -i default -ac 1 -ar 16000 -t 60 ~/munin/models/smoke.wav
```

Then run both stages and report timings:

```bash
python - <<'PY'
import time, torch
from faster_whisper import WhisperModel
from pyannote.audio import Pipeline

wav = "smoke.wav"
for name, path in [("en", "large-v3"), ("no", "nb-whisper-large-ct2-fp16")]:
    t = time.time()
    m = WhisperModel(path, device="cuda", compute_type="float16")
    segs, info = m.transcribe(wav, beam_size=5)
    segs = list(segs)
    print(f"{name}: lang={info.language} {len(segs)} segments in {time.time()-t:.1f}s")
    del m
    torch.cuda.empty_cache()

t = time.time()
p = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1").to(torch.device("cuda"))
out = p(wav)
print(f"diarize: {len(out.speaker_diarization.labels())} speakers in {time.time()-t:.1f}s")
print("peak VRAM:", torch.cuda.max_memory_allocated() // 1024**2, "MiB")
PY
```

Pass criteria: language detection picks `no` and `en` correctly, diarization
finds two speakers, nothing OOMs.

## Step 9. Measure the VRAM budget

This is the step that resolves a real open question, so do not skip it.

The only trustworthy published figure is faster-whisper's own benchmark, which
happens to have been measured on a 3070 Ti 8 GB with CUDA 12.4, large-v2, 13
minutes of audio:

| Precision | Peak VRAM |
|---|---|
| float16, beam 5 | 4,525 MB |
| float16, beam 5, `batch_size=8` | 6,090 MB |
| int8, beam 5 | 2,926 MB |
| int8, beam 5, `batch_size=8` | 4,500 MB |

No large-v3 figure is published; large-v3 is the same 1.55B parameters, so
~4.5 GB is a reasonable read but **UNVERIFIED**.

**This contradicts spec §7.2.** The "~6 GB resident" figure there is the sum of
two weight files. Runtime VRAM for one model is ~4.5 GB, so two resident is ~9 GB
and does not fit an 8 GB card at all. D22 already defaults `keep_warm` to
`false`, which turns out to be the only configuration that works here, not
merely the polite one. Load one model at a time and evict between passes.

**The open risk is pyannote.** No official VRAM figure exists for either
pipeline, and there is an unresolved upstream report of a large regression in
4.x: https://github.com/pyannote/pyannote-audio/issues/1963 claims >9.5 GB peak
under 4.0.3 against ~2.6 GB under 3.3.2, on a 72-minute file, localised to the
`discrete_diarization` reconstruction after clustering. `batch_size` does not
help. The issue is open with no maintainer response. Reported against both
community-1 and 3.1.

That report is on a 24 GB A5000, so it did not OOM there. On 8 GB it would.
**Measure it on a full-length file before committing to this stack:**

```bash
# 60+ minutes of synthetic audio, then watch the peak
python - <<'PY'
import torch, time
from pyannote.audio import Pipeline
p = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1").to(torch.device("cuda"))
t = time.time()
out = p("long-smoke.wav")
print(f"{time.time()-t:.0f}s, peak {torch.cuda.max_memory_allocated() // 1024**2} MiB")
PY
```

If it OOMs, in order of preference:

1. Run ASR and diarization strictly sequentially with an explicit unload
   between. Costs a reload, buys the whole card for each stage.
2. Drop the ASR model to `int8_float16` for the Norwegian path, freeing ~1.5 GB.
3. Pin `pyannote.audio==3.3.x` with the 3.1 pipeline. Costs the old API
   (`use_auth_token=`, bare `Annotation` return, no free `speaker_embeddings`),
   the worse DER in step 6, and the second gate acceptance.

## What this changes in the spec

Carry these back rather than leaving them here:

| Where | Change |
|---|---|
| §7.2 table | `pyannote/speaker-diarization-3.1` → `speaker-diarization-community-1`; note nb-whisper-large is v2-generation |
| §7.2 sizes | The fp16 column is weight size, not runtime VRAM. Add the measured ~4.5 GB per ASR model |
| §7.4.1 | `model` field records the wespeaker model, not `ecapa-tdnn` |
| §8 | `busy_vram_free_mb = 7000` was arithmetic on the wrong number. Set it from step 9 |
| §14 | Add: does pyannote 4.x fit in 8 GB on a full-length file (issue #1963)? |
| §15 | `munin models pull` is ~7.2 GB, not 6.2 GB, with a transient conversion peak |
| Appendix C | `NbAiLab/nb.whisperX` is a dead 2023 fork of whisperX, last commit 2023-12-17. Drop it as a starting point; upstream `m-bain/whisperX` is alive |
