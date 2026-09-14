# Munin

Cross-platform digital meeting recorder and transcription pipeline.

Two-track capture (own microphone separate from application audio) on Linux and
macOS, feeding a self-hosted Whisper + pyannote pipeline. Records only on
explicit confirmation; the calendar enriches a session but never starts one.

Design spec: [`docs/superpowers/specs/2026-09-14-meeting-recorder-design.md`](docs/superpowers/specs/2026-09-14-meeting-recorder-design.md)
Implementation plan: [`docs/superpowers/plans/2026-09-14-implementation-plan.md`](docs/superpowers/plans/2026-09-14-implementation-plan.md)

Design sketches: [`docs/design/`](docs/design/) ([sketch sheet](docs/design/munin-plugin-sketches.html))

Status: a **Linux proof of concept** exists on branch `poc`. It installs on an
Omarchy machine, detects or is told that a call is live, asks before recording,
captures two independent Opus tracks, writes a conformant session directory and
shows state in the bar. **Transcription is deferred**: no model is downloaded
and none is run. The worker, the backend interface and the transcript renderer
exist so that adding a real backend later is one module, and captured sessions
are left in the spool as `pending` with the reason recorded in `session.json`.
macOS and Windows are designed for but not built.

Frozen contracts for the PoC: [`docs/superpowers/specs/2026-09-14-poc-contracts.md`](docs/superpowers/specs/2026-09-14-poc-contracts.md)
(§16 records what the integration merge reconciled, and what is still open).

> This is a public copy of an internal design document. Customer names,
> colleague names and internal project references have been replaced with
> generic descriptors; all counts and measurements are unchanged.

## Development

Runtime code is stdlib-only; the external tools it drives (`pw-record`,
`pw-dump`, `ffmpeg`, `hyprctl`, `omarchy-notification-send`) are subprocesses,
never imports. The only dev dependency is pytest.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The test suite is deterministic and offline: no PipeWire, no audio hardware, no
network, nothing that touches the real `~/munin`. Detection runs against
committed JSON fixtures of real PipeWire and Hyprland object shapes, with every
identity replaced by a synthetic one.

Other checks:

```bash
omarchy plugin validate plugin/local.munin   # the Omarchy shell plugin
node tests/plugin/test_model.mjs             # the plugin's pure logic
bash -n install.sh                           # installer syntax
shellcheck install.sh                        # if installed
.venv/bin/python -m compileall -q src
```

Installing on an Omarchy machine (never runs `sudo`; prints what it cannot do):

```bash
./install.sh --dry-run    # print the plan, touch nothing
./install.sh              # venv, plugin, keybind, systemd unit, data root
munin doctor              # 23 checks; exit 5 if any fail
```

Three processes, kept separate on purpose: `munin-rec` captures and never
transcribes, `munin-work` drains the spool, `munin` is the CLI.

## License

MIT. See [LICENSE](LICENSE).
