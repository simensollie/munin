#!/usr/bin/env python3
"""Render docs/design/munin-plugin-sketches.html to individual JPGs.

Extracts each mockup from the sketch sheet, renders it standalone with
headless Chromium at 2x, and trims to content. Requires chromium and
ImageMagick. Run from the repo root.
"""
import pathlib, re, subprocess, os, sys

SRC = pathlib.Path("docs/design/munin-plugin-sketches.html")
OUT = pathlib.Path("docs/design/sketches")
TMP = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "munin-render"
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

html = SRC.read_text()
css = html[html.index("<style>"): html.index("</style>") + 8]
fontlink = re.search(r'<link rel="stylesheet" href="https://fonts\.googleapis[^>]+>', html).group(0)

def block(open_tag, occurrence=1):
    """Return the balanced <div>...</div> starting at the nth occurrence of open_tag."""
    idx, seen = -1, 0
    while seen < occurrence:
        idx = html.index(open_tag, idx + 1)
        seen += 1
    i, depth = idx, 0
    for m in re.finditer(r'<div\b|</div>', html[idx:]):
        if m.group(0) == '<div': depth += 1
        else:
            depth -= 1
            if depth == 0:
                return html[idx: idx + m.end()]
    raise SystemExit("unbalanced: " + open_tag)

def first_with(tag, needle):
    n = len(re.findall(re.escape(tag), html))
    for k in range(1, n + 1):
        b = block(tag, occurrence=k)
        if needle in b: return b
    raise SystemExit(f"no {tag} containing {needle}")

term_with = lambda needle: first_with('<div class="term">', needle)
tree_with = lambda needle: first_with('<div class="tree">', needle)

SKETCHES = [
    ("01-bar-states",        block('<div class="shell states">'),                          760),
    ("02-bar-in-place",      block('<div class="shell frame">', 1),                         900),
    ("03-prompts",           block('<div class="shell notif-stage">', 1),                   560),
    ("04-dropdown-panel",    block('<div class="shell frame">', 2),                         720),
    ("05-meeting-context",   block('<div class="shell notif-stage" style="padding-bottom:22px">', 1), 460),
    ("06-voice-register",    block('<div class="admin">', 1),                               940),
    ("07-review-queue",      block('<div class="shell admin" style="margin-top:14px">', 1),  780),
    ("08-backends",          block('<div class="admin">', 2),                               940),
    ("09-setup-wizard",      term_with("munin setup"),                                       800),
    ("10-doctor",            term_with("munin doctor"),                                      820),
    ("11-storage-layout",    tree_with("~/munin/"),                                          820),
    ("12-config-toml",       tree_with("[transcribe]"),                                      760),
]

BG = "#0f1114"
for name, markup, width in SKETCHES:
    page = TMP / f"{name}.html"
    page.write_text(f"""<!doctype html><html data-theme="dark"><head><meta charset="utf-8">
{fontlink}
{css}
<style>
  html,body {{ margin:0; background:{BG}; }}
  .stage {{ width:{width}px; padding:28px; }}
  .stage > * {{ margin:0 !important; }}
</style></head><body><div class="stage">{markup}</div></body></html>""")
    png = TMP / f"{name}.png"
    subprocess.run([
        "chromium", "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=2",
        f"--window-size={width + 56},2400",
        "--virtual-time-budget=6000",
        f"--screenshot={png}", f"file://{page.resolve()}"
    ], check=True, capture_output=True)
    jpg = OUT / f"{name}.jpg"
    subprocess.run([
        "magick", str(png), "-trim", "+repage",
        "-bordercolor", BG, "-border", "36",
        "-background", BG, "-flatten",
        "-quality", "92", str(jpg)
    ], check=True)
    print(f"{jpg}  {jpg.stat().st_size // 1024} KB")
