#!/usr/bin/env python
"""Render the README screenshots (docs/assets/*.png) from the synthetic
demo index — real product output, fictional data, terminal-style frames.

Requires Pillow (in the `dev` extra).  Frames are deterministic: the demo
index is rebuilt from voyager.demo each run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from PIL import Image, ImageDraw, ImageFont          # noqa: E402

ASSETS = REPO / "docs" / "assets"
COLS = 100
PAD = 24
TITLE_H = 44
LINE_H = 22
CAPTION_H = 56
BG = (24, 26, 32)
FG = (214, 219, 228)
DIM = (130, 138, 150)
ACCENT = (98, 160, 234)
TITLE_BG = (45, 48, 56)


def font(size: int = 15, bold: bool = False) -> ImageFont.ImageFont:
    for cand in (("consolab.ttf" if bold else "consola.ttf"),
                 "Menlo.ttc", "DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    return ImageFont.load_default()


def run(db: Path, args: list[str]) -> str:
    r = subprocess.run([sys.executable, "-m", "voyager.cli", "--db",
                        db.as_posix()] + args,
                       capture_output=True, text=True,
                       encoding="utf-8", cwd=str(REPO), timeout=120)
    return (r.stdout or r.stderr or "").strip()


def wrap(line: str, width: int = COLS) -> list[str]:
    out = []
    while len(line) > width:
        cut = line.rfind(" ", 0, width)
        cut = cut if cut > 0 else width
        out.append(line[:cut])
        line = "    " + line[cut:].lstrip()
    out.append(line)
    return out


def render(title: str, caption: str, text: str, out: Path) -> None:
    lines: list[tuple[str, tuple]] = []
    for raw in text.splitlines():
        for i, piece in enumerate(wrap(raw.rstrip())):
            lines.append((piece, FG if i == 0 else DIM))
    h = TITLE_H + PAD + LINE_H * len(lines) + PAD + CAPTION_H
    w = PAD * 2 + COLS * 10
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, TITLE_H], fill=TITLE_BG)
    for i, c in enumerate((232, 96, 86)):
        d.ellipse([14 + i * 22, TITLE_H // 2 - 6, 26 + i * 22, TITLE_H // 2 + 6],
                  fill=(c, 130, 90) if i else (c, 96, 86))
    d.text((86, 12), title, font=font(16, bold=True), fill=FG)
    y = TITLE_H + PAD
    f = font(15)
    for textline, color in lines:
        d.text((PAD, y), textline, font=f, fill=color)
        y += LINE_H
    d.line([PAD, h - CAPTION_H + 8, w - PAD, h - CAPTION_H + 8],
           fill=TITLE_BG, width=2)
    d.text((PAD, h - CAPTION_H + 20), caption, font=font(15, bold=True),
           fill=ACCENT)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print("wrote", out)


def main() -> None:
    from voyager import demo
    scratch = Path(os.environ.get("VOYAGER_SCRATCH_ROOT",
                                  Path(tempfile.gettempdir()) if False
                                  else Path.home() / ".voyager"))
    # keep the screenshot index OUT of the user's home: scratch first
    index = scratch / "rc1-screens" / "demo.db"
    index.parent.mkdir(parents=True, exist_ok=True)
    tid = demo.build(index)
    db = ["--db", str(index)]

    render(
        "voyager — search",
        "Search every AI coding conversation locally",
        run(index, ["search", "authentication"]),
        ASSETS / "search.png")

    render(
        "voyager — thread timeline",
        "Understand how your coding decisions evolved",
        run(index, ["thread", "timeline", tid]),
        ASSETS / "timeline.png")

    env = dict(os.environ, VOYAGER_NO_SYNC="1")
    r = subprocess.run([sys.executable, "-m", "voyager.cli", "--db",
                        index.as_posix(),
                        "continue", "--thread", tid, "--json"],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=str(REPO), timeout=120, env=env)
    ctx = (r.stdout or "").strip()
    head = "\n".join(ctx.splitlines()[:18])
    render(
        "voyager — continue",
        "Move context across agents",
        f"$ voyager continue --thread {tid}\n\n{head}\n  …",
        ASSETS / "continue.png")


if __name__ == "__main__":
    main()
