# Design sketches

UI sketches for the Munin Omarchy shell plugin and its admin surface.

- [`munin-plugin-sketches.html`](munin-plugin-sketches.html) — the full sketch
  sheet: bar states, notifications, dropdown panel, Teams detection, M365
  enrichment, voice register, transcription backends, install flow. Open it in a
  browser.
- [`sketches/`](sketches/) — the same mockups as individual JPGs, for embedding
  in issues, PRDs and slides.

| File | Shows |
|---|---|
| `01-bar-states.jpg` | The seven bar-widget states, idle through failed |
| `02-bar-in-place.jpg` | Where the widget sits in the Omarchy bar while recording |
| `03-prompts.jpg` | The three notifications: detected, ending, auto-stopped |
| `04-dropdown-panel.jpg` | The dropdown: tracks, session, actions, recent sessions |
| `05-meeting-context.jpg` | Calendar context and agenda-derived glossary biasing |
| `06-voice-register.jpg` | `munin admin` — the voice register |
| `07-review-queue.jpg` | Post-meeting review of unnamed diarization clusters |
| `08-backends.jpg` | Transcription backends and the fallback chain |
| `09-setup-wizard.jpg` | `munin setup` |
| `10-doctor.jpg` | `munin doctor` |
| `11-storage-layout.jpg` | `~/munin/` on disk |
| `12-config-toml.jpg` | `~/munin/config.toml` |

All names, meeting titles and agendas in the sketches are synthetic.

Regenerate the JPGs after editing the HTML:

```bash
python3 tools/render-sketches.py
```
