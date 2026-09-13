"""Render the Voyager architecture diagram.

Outputs (both from ONE layout definition, so they cannot drift apart):

    docs/screenshots/architecture.png    raster, embedded in the READMEs
    docs/screenshots/architecture.svg    vector, for further tweaking

The picture in one line: 8 agents with 8 storage formats feed one normalized
local index, which is then queryable from the CLI, another agent, or an MCP
host. Re-run after adding a platform:

    python scripts/make_diagram.py

Layout rules enforced by `_check_layout()` (see the assertions): the three
columns have equal outer margins and equal gaps, the left/right card stacks
have the same width and the same total height, the index box sits exactly on
the canvas centre line, and the header/footer text is centred.
"""

from __future__ import annotations

import html
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SHOTS = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
OUT_PNG = SHOTS / "architecture.png"
OUT_SVG = SHOTS / "architecture.svg"

# --- palette (dark terminal) ------------------------------------------------
BG = (12, 12, 12)
PANEL = (22, 24, 28)
PANEL_EDGE = (58, 64, 74)
PANEL_GREEN = (16, 32, 26)
PANEL_GREEN_EDGE = (26, 120, 86)
PANEL_CYAN = (14, 40, 46)
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
# fallback for glyphs the mono font lacks (→ · — …)
SYMBOL_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]
SVG_FONTS = ("Consolas, 'DejaVu Sans Mono', Menlo, 'Courier New', monospace")

# --- canvas -----------------------------------------------------------------
# width = 2*MARGIN + COL_W + GAP + MID_W + GAP + COL_W
W, H = 1424, 880
MARGIN = 56

# --- grid -------------------------------------------------------------------
COL_W = 372          # left and right column width (identical: equal weight)
MID_W = 392          # wide enough for the longest line + padding (see fits())
GAP = 88             # horizontal gap between two neighbouring columns
CARD_H = 56          # left card height
CARD_GAP = 12
RCARD_H = 72         # right cards are taller so both stacks end up equally tall
RCARD_GAP = 20

CARD_PAD = 20        # text inset inside the side cards
MID_PAD = 26         # text inset inside the index box

TITLE_Y, TITLE_SIZE = 52, 32
TAG_SIZE = 19
SUB_Y, SUB_SIZE = 96, 17
HEAD_Y, HEAD_SIZE = 142, 17
COL_TOP = 186
MID_H = 286

FOOT_SIZE = 15
FOOT_LINE_H = 22

# --- content (unchanged) ----------------------------------------------------
TAGLINE = "one index across every AI coding agent"
SUBTITLE = ("8 platforms · 8 storage formats  →  one normalized local index  →  "
            "one query surface")
HEAD_LEFT = "YOUR AGENTS  (read-only adapters)"
HEAD_MID = "ONE INDEX"
HEAD_RIGHT = "USE IT FROM"
MID_TITLE = "Voyager Index"
MID_PATH = "~/.voyager/index.db"
MID_LINES = [
    "normalized Session + Event model",
    "SQLite + FTS5 (trigram) → CJK search",
    "raw provider event kept alongside",
    "incremental: sources tracked by mtime+size",
    "idempotent; vanishes → pruned",
]
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
OUTPUTS = [
    ("search", "substring + CJK across every agent"),
    ("repo timeline", "all agents on one project, in order"),
    ("show / export", "timeline, Markdown, lossless JSON"),
    ("resume / continue", "native CLI, or automatic handoff"),
    ("handoff", "context package for another agent"),
    ("MCP server", "agents query the index themselves"),
]
FOOTER_1 = ("Everything stays on your machine — no account, no cloud, "
            "no telemetry. Voyager only ever reads agent storage.")
FOOTER_2 = ("pip install voyager  ·  voyager scan  ·  voyager search \"…\"  ·  "
            "voyager continue")


def _layout() -> dict:
    """Every coordinate of the picture, derived from the grid constants."""
    lx = MARGIN
    l_right = lx + COL_W
    mx = l_right + GAP
    m_right = mx + MID_W
    rx = m_right + GAP
    r_right = rx + COL_W
    assert r_right == W - MARGIN, "columns do not fill the canvas symmetrically"

    left_span = len(PROVIDERS) * CARD_H + (len(PROVIDERS) - 1) * CARD_GAP
    right_span = len(OUTPUTS) * RCARD_H + (len(OUTPUTS) - 1) * RCARD_GAP
    col_bottom = COL_TOP + max(left_span, right_span)
    col_centre = (COL_TOP + col_bottom) / 2

    mid_top = int(col_centre - MID_H / 2)
    foot2_y = H - MARGIN - FOOT_LINE_H
    foot1_y = foot2_y - 32

    def left_card(i):
        y = COL_TOP + i * (CARD_H + CARD_GAP)
        return y, y + CARD_H

    def right_card(i):
        y = COL_TOP + i * (RCARD_H + RCARD_GAP)
        return y, y + RCARD_H

    return {
        "lx": lx, "l_right": l_right, "mx": mx, "m_right": m_right,
        "rx": rx, "r_right": r_right,
        "bus_left": (l_right + mx) // 2, "bus_right": (m_right + rx) // 2,
        "col_centre_x": (lx + r_right) / 2,
        "left_span": left_span, "right_span": right_span,
        "col_bottom": col_bottom, "col_centre_y": col_centre,
        "mid_top": mid_top, "mid_bottom": mid_top + MID_H,
        "mid_centre_x": (mx + m_right) / 2,
        "head_left_cx": (lx + l_right) / 2,
        "head_mid_cx": (mx + m_right) / 2,
        "head_right_cx": (rx + r_right) / 2,
        "left_card": left_card, "right_card": right_card,
        "foot1_y": foot1_y, "foot2_y": foot2_y,
        "mid_y": int(col_centre),
    }


def _check_layout(L: dict) -> None:
    """Fail loudly if an edit breaks the balance of the picture."""
    assert L["left_span"] == L["right_span"], (
        f"left/right stacks differ in height: {L['left_span']} vs {L['right_span']}"
    )
    assert L["mx"] - L["l_right"] == L["rx"] - L["m_right"] == GAP, "gaps differ"
    assert L["lx"] == W - L["r_right"] == MARGIN, "outer margins differ"
    assert L["mid_centre_x"] == L["col_centre_x"] == W / 2, "index box off-centre"
    assert L["bus_left"] - L["l_right"] == L["rx"] - L["bus_right"], "buses uneven"
    assert L["mid_top"] > HEAD_Y + 24, "index box collides with the column headings"
    assert L["mid_bottom"] < L["foot1_y"] - 24, "index box runs into the footer"
    assert abs((L["mid_top"] + L["mid_bottom"]) / 2 - L["col_centre_y"]) < 1, (
        "index box is not vertically centred on the columns"
    )
    assert abs(L["col_centre_y"] - H / 2) <= 16, (
        f"the card stacks are not optically centred: their centre {L['col_centre_y']} "
        f"is {L['col_centre_y'] - H / 2:+.0f}px off the canvas centre"
    )
    assert L["col_bottom"] < L["foot1_y"] - 24, "columns run into the footer"
    assert L["foot2_y"] + FOOT_LINE_H == H - MARGIN, "bottom margin is off"
    assert TITLE_Y == MARGIN - 4, "top margin is off"
    _check_text_fits()


def _check_text_fits() -> None:
    """No label may overflow the box it sits in (the old middle box did)."""
    side = COL_W - 2 * CARD_PAD
    middle = MID_W - 2 * MID_PAD
    full = W - 2 * MARGIN
    for name, fmt in PROVIDERS:
        assert text_width(name, 19) <= side, f"provider name too wide: {name}"
        assert text_width(fmt, 14) <= side, f"provider format too wide: {fmt}"
    for name, what in OUTPUTS:
        assert text_width(name, 19) <= side, f"use-case name too wide: {name}"
        assert text_width(what, 14) <= side, f"use-case text too wide: {what}"
    for line in MID_LINES:
        assert text_width(line, 15) <= middle, f"index line too wide: {line}"
    for label in (HEAD_LEFT,):
        assert text_width(label, HEAD_SIZE) <= COL_W, f"heading too wide: {label}"
    for label in (HEAD_MID,):
        assert text_width(label, HEAD_SIZE) <= MID_W, f"heading too wide: {label}"
    for line, size in ((SUBTITLE, SUB_SIZE), (FOOTER_1, FOOT_SIZE),
                       (FOOTER_2, FOOT_SIZE)):
        assert text_width(line, size) <= full, f"centred line too wide: {line[:30]}"
    header = (text_width("Voyager", TITLE_SIZE) + 18
              + text_width(TAGLINE, TAG_SIZE))
    assert header <= full, "header is wider than the content area"


# ---------------------------------------------------------------------------
# fonts (Pillow) — per-glyph fallback so → · — never turn into blanks
# ---------------------------------------------------------------------------

_FONT_CACHE: dict = {}
_GLYPH_CACHE: dict = {}


def _truetype(candidates, size):
    for path in candidates:
        p = Path(path)
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return None


def font(size: int):
    if size not in _FONT_CACHE:
        f = _truetype(FONT_CANDIDATES, size) or ImageFont.load_default()
        _FONT_CACHE[size] = f
    return _FONT_CACHE[size]


def symbol_font(size: int):
    return _truetype(SYMBOL_CANDIDATES, size)


def has_glyph(f, ch: str) -> bool:
    """True when the font actually has a glyph (missing glyphs render empty)."""
    key = (id(f), ch)
    if key not in _GLYPH_CACHE:
        try:
            _GLYPH_CACHE[key] = f.getmask(ch).getbbox() is not None
        except Exception:
            _GLYPH_CACHE[key] = True
    return _GLYPH_CACHE[key]


def text_width(s: str, size: int) -> float:
    f = font(size)
    total = 0.0
    for ch in s:
        total += f.getlength(ch) if has_glyph(f, ch) else _fallback_len(ch, size)
    return total


def _fallback_len(ch: str, size: int) -> float:
    sym = symbol_font(size)
    return sym.getlength(ch) if sym else font(size).getlength("M")


def draw_text(d, x, y, s, size, fill):
    """Left-anchored text at (x, y) with per-glyph font fallback."""
    f, sym = font(size), symbol_font(size)
    cx = x
    for ch in s:
        use = f if (has_glyph(f, ch) or sym is None) else sym
        d.text((cx, y), ch, font=use, fill=fill)
        cx += use.getlength(ch)


def draw_text_center(d, cx, y, s, size, fill):
    draw_text(d, cx - text_width(s, size) / 2, y, s, size, fill)


def draw_text_pair_center(d, cx, y, left, left_size, left_fill,
                          right, right_size, right_fill, gap=18):
    """Two styled runs on one centred line, e.g. `Voyager` + tagline."""
    total = text_width(left, left_size) + gap + text_width(right, right_size)
    x = cx - total / 2
    draw_text(d, x, y, left, left_size, left_fill)
    draw_text(d, x + text_width(left, left_size) + gap, y + (left_size - right_size)
              + 6, right, right_size, right_fill)


# ---------------------------------------------------------------------------
# raster renderer
# ---------------------------------------------------------------------------

def _arrow(d, p0, p1, head=True):
    d.line([p0, p1], fill=ARROW, width=2)
    if not head:
        return
    (x0, y0), (x1, y1) = p0, p1
    if y0 == y1:                                   # horizontal
        s = 9 if x1 > x0 else -9
        d.polygon([(x1, y1), (x1 - s, y1 - 5), (x1 - s, y1 + 5)], fill=ARROW)
    else:                                          # vertical
        s = 9 if y1 > y0 else -9
        d.polygon([(x1, y1), (x1 - 5, y1 - s), (x1 + 5, y1 - s)], fill=ARROW)


def _box(d, x0, y0, x1, y1, fill, edge, radius=12, width=2):
    d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill,
                        outline=edge, width=width)


def render_png(L: dict) -> Path:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    cx = W / 2

    # ---- header (centred) --------------------------------------------------
    draw_text_pair_center(d, cx, TITLE_Y, "Voyager", TITLE_SIZE, WHITE,
                          TAGLINE, TAG_SIZE, CYAN)
    draw_text_center(d, cx, SUB_Y, SUBTITLE, SUB_SIZE, DIM)

    # ---- column headings (one row, centred over each column) ---------------
    draw_text_center(d, L["head_left_cx"], HEAD_Y, HEAD_LEFT, HEAD_SIZE, DIM)
    draw_text_center(d, L["head_mid_cx"], HEAD_Y, HEAD_MID, HEAD_SIZE, DIM)
    draw_text_center(d, L["head_right_cx"], HEAD_Y, HEAD_RIGHT, HEAD_SIZE, DIM)

    # ---- left: agents ------------------------------------------------------
    for i, (name, fmt) in enumerate(PROVIDERS):
        y0, y1 = L["left_card"](i)
        _box(d, L["lx"], y0, L["l_right"], y1, PANEL, PANEL_EDGE)
        draw_text(d, L["lx"] + CARD_PAD, y0 + 8, name, 19, FG)
        draw_text(d, L["lx"] + CARD_PAD, y0 + 31, fmt, 14, DIM)

    # ---- middle: the index -------------------------------------------------
    _box(d, L["mx"], L["mid_top"], L["m_right"], L["mid_bottom"],
         PANEL_CYAN, CYAN, radius=16, width=3)
    draw_text(d, L["mx"] + MID_PAD, L["mid_top"] + 32, MID_TITLE, 22, WHITE)
    draw_text(d, L["mx"] + MID_PAD, L["mid_top"] + 70, MID_PATH, 15, CYAN)
    for j, line in enumerate(MID_LINES):
        draw_text(d, L["mx"] + MID_PAD, L["mid_top"] + 112 + j * 30, line, 15, FG)

    # ---- right: how you use it --------------------------------------------
    for i, (name, what) in enumerate(OUTPUTS):
        y0, y1 = L["right_card"](i)
        _box(d, L["rx"], y0, L["r_right"], y1, PANEL_GREEN, PANEL_GREEN_EDGE)
        draw_text(d, L["rx"] + CARD_PAD, y0 + 13, name, 19, GREEN)
        draw_text(d, L["rx"] + CARD_PAD, y0 + 41, what, 14, DIM)

    # ---- connectors: agents -> bus -> index -> bus -> use cases ------------
    mid_y = L["mid_y"]
    first_l, last_l = L["left_card"](0), L["left_card"](len(PROVIDERS) - 1)
    d.line([(L["bus_left"], (first_l[0] + first_l[1]) // 2),
            (L["bus_left"], (last_l[0] + last_l[1]) // 2)], fill=ARROW, width=2)
    for i in range(len(PROVIDERS)):
        y0, y1 = L["left_card"](i)
        _arrow(d, (L["l_right"], (y0 + y1) // 2), (L["bus_left"], (y0 + y1) // 2),
               head=False)
    _arrow(d, (L["bus_left"], mid_y), (L["mx"], mid_y))

    first_r, last_r = L["right_card"](0), L["right_card"](len(OUTPUTS) - 1)
    d.line([(L["bus_right"], (first_r[0] + first_r[1]) // 2),
            (L["bus_right"], (last_r[0] + last_r[1]) // 2)], fill=ARROW, width=2)
    _arrow(d, (L["m_right"], mid_y), (L["bus_right"], mid_y), head=False)
    for i in range(len(OUTPUTS)):
        y0, y1 = L["right_card"](i)
        _arrow(d, (L["bus_right"], (y0 + y1) // 2), (L["rx"], (y0 + y1) // 2))

    # ---- footer (centred) --------------------------------------------------
    draw_text_center(d, cx, L["foot1_y"], FOOTER_1, FOOT_SIZE, DIM)
    draw_text_center(d, cx, L["foot2_y"], FOOTER_2, FOOT_SIZE, YELLOW)

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT_PNG, optimize=True)
    print(f"saved {OUT_PNG} ({img.width}x{img.height}, "
          f"{OUT_PNG.stat().st_size // 1024} KB)")
    return OUT_PNG


# ---------------------------------------------------------------------------
# vector renderer (same layout, same coordinates)
# ---------------------------------------------------------------------------

def _hex(rgb) -> str:
    return "#%02x%02x%02x" % rgb


class _Svg:
    def __init__(self):
        self.parts: list[str] = []

    def rect(self, x0, y0, x1, y1, fill, stroke, radius=12, width=2):
        self.parts.append(
            f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" '
            f'rx="{radius}" fill="{_hex(fill)}" stroke="{_hex(stroke)}" '
            f'stroke-width="{width}"/>'
        )

    def line(self, x0, y0, x1, y1, colour=ARROW, width=2):
        self.parts.append(
            f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" '
            f'stroke="{_hex(colour)}" stroke-width="{width}"/>'
        )

    def polygon(self, points, fill=ARROW):
        pts = " ".join(f"{x},{y}" for x, y in points)
        self.parts.append(f'<polygon points="{pts}" fill="{_hex(fill)}"/>')

    def text(self, x, y_top, s, size, fill, anchor="start"):
        """`y_top` matches the raster renderer; SVG wants the baseline."""
        ascent = font(size).getmetrics()[0]
        self.parts.append(
            f'<text x="{round(x, 1)}" y="{round(y_top + ascent, 1)}" '
            f'font-family="{SVG_FONTS}" font-size="{size}" '
            f'text-anchor="{anchor}" fill="{_hex(fill)}">'
            f'{html.escape(s)}</text>'
        )

    def text_center(self, cx, y_top, s, size, fill):
        self.text(cx, y_top, s, size, fill, anchor="middle")

    def render(self) -> str:
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" '
                f'height="{H}" viewBox="0 0 {W} {H}" '
                f'shape-rendering="geometricPrecision">\n'
                f'<rect width="{W}" height="{H}" fill="{_hex(BG)}"/>\n')
        return head + "\n".join(self.parts) + "\n</svg>\n"


def render_svg(L: dict) -> Path:
    s = _Svg()
    cx = W / 2

    total = text_width("Voyager", TITLE_SIZE) + 18 + text_width(TAGLINE, TAG_SIZE)
    x = cx - total / 2
    s.text(x, TITLE_Y, "Voyager", TITLE_SIZE, WHITE)
    s.text(x + text_width("Voyager", TITLE_SIZE) + 18,
           TITLE_Y + (TITLE_SIZE - TAG_SIZE) + 6, TAGLINE, TAG_SIZE, CYAN)
    s.text_center(cx, SUB_Y, SUBTITLE, SUB_SIZE, DIM)

    s.text_center(L["head_left_cx"], HEAD_Y, HEAD_LEFT, HEAD_SIZE, DIM)
    s.text_center(L["head_mid_cx"], HEAD_Y, HEAD_MID, HEAD_SIZE, DIM)
    s.text_center(L["head_right_cx"], HEAD_Y, HEAD_RIGHT, HEAD_SIZE, DIM)

    for i, (name, fmt) in enumerate(PROVIDERS):
        y0, y1 = L["left_card"](i)
        s.rect(L["lx"], y0, L["l_right"], y1, PANEL, PANEL_EDGE)
        s.text(L["lx"] + CARD_PAD, y0 + 8, name, 19, FG)
        s.text(L["lx"] + CARD_PAD, y0 + 31, fmt, 14, DIM)

    s.rect(L["mx"], L["mid_top"], L["m_right"], L["mid_bottom"],
           PANEL_CYAN, CYAN, radius=16, width=3)
    s.text(L["mx"] + MID_PAD, L["mid_top"] + 32, MID_TITLE, 22, WHITE)
    s.text(L["mx"] + MID_PAD, L["mid_top"] + 70, MID_PATH, 15, CYAN)
    for j, line in enumerate(MID_LINES):
        s.text(L["mx"] + MID_PAD, L["mid_top"] + 112 + j * 30, line, 15, FG)

    for i, (name, what) in enumerate(OUTPUTS):
        y0, y1 = L["right_card"](i)
        s.rect(L["rx"], y0, L["r_right"], y1, PANEL_GREEN, PANEL_GREEN_EDGE)
        s.text(L["rx"] + CARD_PAD, y0 + 13, name, 19, GREEN)
        s.text(L["rx"] + CARD_PAD, y0 + 41, what, 14, DIM)

    mid_y = L["mid_y"]
    first_l, last_l = L["left_card"](0), L["left_card"](len(PROVIDERS) - 1)
    s.line(L["bus_left"], (first_l[0] + first_l[1]) // 2,
           L["bus_left"], (last_l[0] + last_l[1]) // 2)
    for i in range(len(PROVIDERS)):
        y0, y1 = L["left_card"](i)
        s.line(L["l_right"], (y0 + y1) // 2, L["bus_left"], (y0 + y1) // 2)
    s.line(L["bus_left"], mid_y, L["mx"], mid_y)
    s.polygon([(L["mx"], mid_y), (L["mx"] - 9, mid_y - 5), (L["mx"] - 9, mid_y + 5)])

    first_r, last_r = L["right_card"](0), L["right_card"](len(OUTPUTS) - 1)
    s.line(L["bus_right"], (first_r[0] + first_r[1]) // 2,
           L["bus_right"], (last_r[0] + last_r[1]) // 2)
    s.line(L["m_right"], mid_y, L["bus_right"], mid_y)
    for i in range(len(OUTPUTS)):
        y0, y1 = L["right_card"](i)
        s.line(L["bus_right"], (y0 + y1) // 2, L["rx"], (y0 + y1) // 2)
        s.polygon([(L["rx"], (y0 + y1) // 2), (L["rx"] - 9, (y0 + y1) // 2 - 5),
                   (L["rx"] - 9, (y0 + y1) // 2 + 5)])

    s.text_center(cx, L["foot1_y"], FOOTER_1, FOOT_SIZE, DIM)
    s.text_center(cx, L["foot2_y"], FOOTER_2, FOOT_SIZE, YELLOW)

    OUT_SVG.write_text(s.render(), encoding="utf-8")
    print(f"saved {OUT_SVG} ({OUT_SVG.stat().st_size // 1024} KB)")
    return OUT_SVG


def main() -> None:
    L = _layout()
    _check_layout(L)
    print(f"canvas {W}x{H} · margins {MARGIN}px · columns "
          f"{COL_W}/{MID_W}/{COL_W} · gaps {GAP}px · stacks "
          f"{L['left_span']}px each")
    render_png(L)
    render_svg(L)


if __name__ == "__main__":
    main()
