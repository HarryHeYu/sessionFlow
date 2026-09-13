"""Render real voyager CLI output as a Windows-Terminal-style PNG.

Usage: python scripts/make_screenshot.py <input.txt> <output.png> [title]
Input: plain text captured from real runs; lines starting with '$ ' are
treated as commands. Long lines are wrapped. ASCII uses Consolas, CJK falls
back to Microsoft YaHei (per-glyph switching).
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (12, 12, 12)            # Windows Terminal default #0C0C0C
TABBAR = (32, 32, 32)
FG = (204, 204, 204)
DIM = (120, 120, 120)
CYAN = (95, 208, 214)
GREEN = (22, 178, 124)
YELLOW = (245, 219, 111)
WHITE = (240, 240, 240)

ASCII_FONT = Path("C:/Windows/Fonts/consola.ttf")
CJK_FONT = Path("C:/Windows/Fonts/msyh.ttc")
SIZE = 19
COLUMNS = 112          # terminal columns


def fonts():
    return (ImageFont.truetype(str(ASCII_FONT), SIZE),
            ImageFont.truetype(str(CJK_FONT), SIZE))


def is_wide(ch: str) -> bool:
    return ord(ch) > 0x2E80


def line_width(ch, line):
    return sum(ch.getlength(c) if not is_wide(c) else ch.getlength("中")
               for c in line)


def wrap_line(line, ascii_f, cjk_f, limit_px):
    """Wrap by display width in pixels (CJK counts double)."""
    out, cur, w = [], "", 0.0
    for c in line:
        cw = ascii_f.getlength(c) if not is_wide(c) else cjk_f.getlength("中")
        if w + cw > limit_px and cur:
            out.append(cur)
            cur, w = "", 0.0
        cur += c
        w += cw
    out.append(cur)
    return out


def pick_font(ch, ascii_f, cjk_f):
    return cjk_f if is_wide(ch) else ascii_f


def colorize(line, in_cmd):
    """Return (color, bold) for a line."""
    if line.startswith("$ "):
        return CYAN, False
    if "sessions indexed" in line or line.startswith("scan complete"):
        return GREEN, False
    if line.startswith("===") or line.startswith("--"):
        return DIM, False
    if line.startswith("[2"):                      # list rows / brief stamps
        return YELLOW, False
    if line.startswith("    "):
        return FG, False
    if line.startswith("agent activity") or line.startswith("hits across") \
            or line.startswith("session(s)") or "sessions)" in line[:40]:
        return DIM, False
    if in_cmd:
        return WHITE, False
    return FG, False


def render(in_path: Path, out_path: Path, title: str):
    ascii_f, cjk_f = fonts()
    char_w = ascii_f.getlength("M")
    limit_px = char_w * COLUMNS
    lines = []
    for raw in Path(in_path).read_text(encoding="utf-8").splitlines():
        raw = raw.rstrip()
        if not raw:
            lines.append("")
            continue
        lines.extend(wrap_line(raw, ascii_f, cjk_f, limit_px))

    line_h = int(SIZE * 1.45)
    pad = 22
    bar_h = 42
    width = int(limit_px) + pad * 2

    img = Image.new("RGB", (width, bar_h + pad * 2 + line_h * len(lines)), BG)
    d = ImageDraw.Draw(img)
    # tab bar
    d.rectangle([0, 0, width, bar_h], fill=TABBAR)
    tab_w = int(char_w * (len(title) + 6))
    d.rounded_rectangle([10, 7, 10 + tab_w, bar_h - 6], 6, fill=BG)
    tf = ImageFont.truetype(str(ASCII_FONT), 13)
    d.text((10 + 12, (bar_h - 6 - 13) // 2 + 3), f"∎ {title}",
           font=tf, fill=(210, 210, 210))
    # window buttons hint
    d.text((width - 70, (bar_h - 13) // 2 + 2), "—  ▢  ✕",
           font=tf, fill=(150, 150, 150))

    y = bar_h + pad - 6
    for line in lines:
        color, _ = colorize(line, line.startswith("$ "))
        x = pad
        for c in line:
            f = pick_font(c, ascii_f, cjk_f)
            d.text((x, y), c, font=f, fill=color)
            x += f.getlength(c) if not is_wide(c) else cjk_f.getlength("中")
        y += line_h

    img.save(out_path, optimize=True)
    print(f"saved {out_path} ({img.width}x{img.height}, "
          f"{out_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    render(Path(sys.argv[1]), Path(sys.argv[2]),
           sys.argv[3] if len(sys.argv) > 3 else "voyager")
