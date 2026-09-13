"""Render the Voyager architecture diagram (docs/screenshots/architecture.png).

The README's job in one picture: 8 agents with 8 storage formats feed one
normalized local index, which is then queryable from the CLI, another agent,
or an MCP host. Drawn with Pillow so it stays reproducible (re-run after
adding a platform: `python scripts/make_diagram.py`).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots" / "architecture.png"

BG = (12, 12, 12)
PANEL = (22, 24, 28)
PANEL_EDGE = (58, 64, 74)
FG = (208, 208, 208)
DIM = (128, 134, 144)
WHITE = (242, 242, 242)
CYAN = (95, 208, 214)
GREEN = (22, 178, 124)
YELLOW = (245, 219, 111)
ARROW = (110, 116, 126)

FONT_CANDIDATES = [
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]

W, H = 1360, 820

# provider column: (name, storage format)
PROVIDERS = [
    ("Codex", "rollout JSONL"),
    ("Claude Code", "project JSONL + file history"),
    ("ZCode", "SQLite (session/message/part)"),
    ("DSH", "zstd JSONL"),
    ("Grok CLI", "chat_history.jsonl + summary"),
    ("Cursor", "state.vscdb (SQLite KV)"),
    ("Kiro", "workspace-session JSON"),
    ("Antigravity", "conversation SQLite (protobuf)"),
]

# right column: (capability, what it gives you)
OUTPUTS = [
    ("search", "substring + CJK across every agent"),
    ("repo timeline", "all agents on one project, in order"),
    ("show / export", "timeline, Markdown, lossless JSON"),
    ("resume / continue", "native CLI, or automatic handoff"),
    ("handoff", "context package for another agent"),
    ("MCP server", "agents query the index themselves"),
]


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        p = Path(path)
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size, index=1 if bold else 0)
            except OSError:
                continue
    return ImageFont.load_default()


def box(d: ImageDraw.ImageDraw, xy, fill, edge, radius=10, width=2):
    d.rounded_rectangle(xy, radius=radius, fill=fill, outline=edge, width=width)


def arrow(d: ImageDraw.ImageDraw, points, head_at_end=True):
    d.line(points, fill=ARROW, width=2, joint="curve")
    if not head_at_end:
        return
    (x1, y1), (x2, y2) = points[-2], points[-1]
    if x2 == x1:                       # vertical
        d.polygon([(x2, y2), (x2 - 5, y2 - 9 if y2 > y1 else y2 + 9),
                   (x2 + 5, y2 - 9 if y2 > y1 else y2 + 9)], fill=ARROW)
    else:                              # horizontal
        d.polygon([(x2, y2), (x2 - 9 if x2 > x1 else x2 + 9, y2 - 5),
                   (x2 - 9 if x2 > x1 else x2 + 9, y2 + 5)], fill=ARROW)


def render() -> Path:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_title = load_font(32)
    f_sub = load_font(19)
    f_col = load_font(18)
    f_name = load_font(20)
    f_small = load_font(16)
    f_diag = load_font(22)

    d.text((44, 34), "Voyager", font=f_title, fill=WHITE)
    d.text((160, 42), "one index across every AI coding agent", font=f_sub, fill=CYAN)
    d.text((44, 84),
           "8 platforms · 8 storage formats  →  one normalized local index  →  "
           "one query surface",
           font=f_sub, fill=DIM)

    # ---- left: providers ---------------------------------------------------
    lx, lw, lh, gap = 44, 430, 56, 14
    ly0 = 160
    d.text((lx, 132), "YOUR AGENTS  (read-only adapters)", font=f_col, fill=DIM)
    for i, (name, fmt) in enumerate(PROVIDERS):
        y = ly0 + i * (lh + gap)
        box(d, [lx, y, lx + lw, y + lh], PANEL, PANEL_EDGE)
        d.text((lx + 18, y + 8), name, font=f_name, fill=FG)
        d.text((lx + 18, y + 33), fmt, font=f_small, fill=DIM)

    # ---- middle: the index -------------------------------------------------
    mx, mw = 560, 330
    my, mh = 330, 210
    d.text((mx, 132), "ONE INDEX", font=f_col, fill=DIM)
    box(d, [mx, my, mx + mw, my + mh], (14, 40, 46), CYAN, radius=14, width=3)
    d.text((mx + 24, my + 20), "Voyager Index", font=f_diag, fill=WHITE)
    d.text((mx + 24, my + 54), "~/.voyager/index.db", font=f_small, fill=CYAN)
    for j, line in enumerate([
        "normalized Session + Event model",
        "SQLite + FTS5 (trigram) → CJK search",
        "raw provider event kept alongside",
        "incremental: sources tracked by mtime+size",
        "idempotent; vanishes → pruned",
    ]):
        d.text((mx + 24, my + 88 + j * 24), line, font=f_small, fill=FG)

    # ---- right: how you use it --------------------------------------------
    rx, rw, rh, rgap = 960, 356, 56, 24
    ry0 = 200
    d.text((rx, 132), "USE IT FROM", font=f_col, fill=DIM)
    for i, (name, what) in enumerate(OUTPUTS):
        y = ry0 + i * (rh + rgap)
        box(d, [rx, y, rx + rw, y + rh], (16, 32, 26), (26, 120, 86))
        d.text((rx + 18, y + 8), name, font=f_name, fill=GREEN)
        d.text((rx + 18, y + 33), what, font=f_small, fill=DIM)

    # ---- connectors: providers -> bus -> index -----------------------------
    bus_x = 508
    mid_y = my + mh // 2
    arrow(d, [(bus_x, ly0 + 20), (bus_x, ly0 + 7 * (lh + gap) + lh - 20)])
    for i in range(len(PROVIDERS)):
        y = ly0 + i * (lh + gap) + lh // 2
        arrow(d, [(lx + lw, y), (bus_x, y)], head_at_end=False)
    arrow(d, [(bus_x, mid_y), (mx, mid_y)])

    # ---- connectors: index -> bus -> outputs -------------------------------
    bus2_x = 916
    arrow(d, [(bus2_x, ry0 + 20), (bus2_x, ry0 + 7 * (rh + rgap) + rh - 20)])
    arrow(d, [(mx + mw, mid_y), (bus2_x, mid_y)], head_at_end=False)
    for i in range(len(OUTPUTS)):
        y = ry0 + i * (rh + rgap) + rh // 2
        arrow(d, [(bus2_x, y), (rx, y)])

    # ---- footnote ----------------------------------------------------------
    d.text((44, H - 58),
           "Everything stays on your machine — no account, no cloud, no telemetry. "
           "Voyager only ever reads agent storage.",
           font=f_small, fill=DIM)
    d.text((44, H - 34),
           "pip install voyager  ·  voyager scan  ·  voyager search \"…\"  ·  "
           "voyager continue",
           font=f_small, fill=YELLOW)

    _check_layout(ly0, lh, gap, ry0, rh, rgap, my, mh, ly0 + 7 * (lh + gap) + lh,
                  ry0 + 5 * (rh + rgap) + rh)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, optimize=True)
    print(f"saved {OUT} ({img.width}x{img.height}, {OUT.stat().st_size // 1024} KB)")
    return OUT


def _check_layout(ly0, lh, gap, ry0, rh, rgap, my, mh, left_bottom, right_bottom):
    """Fail loudly if a later edit makes the columns collide or overflow."""
    assert left_bottom < H - 70, f"provider column overlaps the footer: {left_bottom}"
    assert right_bottom < H - 70, f"output column overlaps the footer: {right_bottom}"
    assert ly0 + lh + gap > ly0 + lh, "provider boxes overlap"
    assert ry0 + rh + rgap > ry0 + rh, "output boxes overlap"
    assert my > 132 and my + mh < H - 70, "index box out of bounds"
    assert abs((ly0 + left_bottom) / 2 - (my + mh / 2)) < 40, "index box not centred"
    assert abs((ry0 + right_bottom) / 2 - (my + mh / 2)) < 40, "outputs not centred"


if __name__ == "__main__":
    render()
