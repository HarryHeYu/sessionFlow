#!/usr/bin/env python
"""Render docs/assets/demo.gif — a typed terminal replay of the synthetic
demo: search -> timeline -> continue -> doctor (detect + safe repair).

Pure Pillow, no screen recorder: every frame is deterministic, every line
of output is real CLI output captured from the synthetic demo index
(`voyager demo`'s story plus a deliberately corrupted FTS for the doctor
scene).  Scratch stays under VOYAGER_SCRATCH_ROOT when set.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from make_screenshots import (BG, DIM, FG, ACCENT, TITLE_BG, font,  # noqa: E402
                              run)

from PIL import Image, ImageDraw  # noqa: E402

W, H, PAD, TITLE_H, LINE_H = 760, 460, 20, 40, 19
PROMPT = (126, 211, 134)      # green $
OUT_MS = 550                  # dwell on a finished scene
TYPE_MS = 28                  # per typed character


def scenes() -> list[tuple[str, list[str]]]:
    """(window title, transcript lines) per scene; output is real."""
    from voyager import demo
    scratch = Path(os.environ.get("VOYAGER_SCRATCH_ROOT",
                                  Path.home() / ".voyager"))
    index = scratch / "rc1-gif" / "demo.db"
    index.parent.mkdir(parents=True, exist_ok=True)
    tid = demo.build(index)
    dbp = index.as_posix()

    search_out = run(index, ["search", "authentication"])
    timeline_out = "\n".join(
        run(index, ["thread", "timeline", tid]).splitlines()[:8])
    env = dict(os.environ, VOYAGER_NO_SYNC="1")
    cont = subprocess.run(
        [sys.executable, "-m", "voyager.cli", "--db", dbp,
         "continue", "--thread", tid, "--json"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(REPO), timeout=120, env=env)
    cont_head = "\n".join((cont.stdout or "").strip().splitlines()[:9])

    # doctor scene: a copy with a broken (derived-only) FTS index
    doc_db = scratch / "rc1-gif" / "doctor.db"
    shutil.copy(index, doc_db)
    con = sqlite3.connect(str(doc_db))
    con.execute("DELETE FROM event_fts WHERE rowid > 0")
    con.commit()
    con.close()
    doctor_bad = [ln for ln in run(doc_db, ["doctor"]).splitlines()
                  if "FTS_INCONSISTENT" in ln or "warning (1)" in ln]
    fix_out = run(doc_db, ["doctor", "--fix"])
    search_ok = run(doc_db, ["search", "refresh token"])

    return [
        ("voyager — demo",
         [f"$ voyager demo",
          "Seeded a synthetic demo index (4 agents, 1 WorkThread):",
          f"  {dbp}",
          "",
          "  Codex 09:00  implements authentication flow",
          "  Claude 14:00 reviews it   ZCode 17:00 debugs",
          "  DSH 20:00 hardens rotation",
          "",
          "  (synthetic data only — no real sessions touched)"]),
        ("voyager — search",
         [f'$ voyager search --db {dbp} "authentication"',
          search_out]),
        ("voyager — thread timeline",
         [f"$ voyager thread timeline {tid}",
          timeline_out]),
        ("voyager — continue",
         [f"$ voyager continue --thread {tid} --json",
          cont_head, "  …"]),
        ("voyager — doctor",
         [f"$ voyager doctor",
          *doctor_bad,
          "",
          f"$ voyager doctor --fix",
          *[ln for ln in fix_out.splitlines() if ln.strip()][2:4],
          "",
          f'$ voyager search "refresh token"',
          *search_ok.splitlines()[:2],
          "search works again — derived index rebuilt"]),
    ]


def flat(lines: list[str]) -> list[str]:
    """CLI output blocks arrive as one multi-line string each — expand them,
    or draw_frame's y-advance counts a 10-line block as one line and later
    lines collide with its tail."""
    out: list[str] = []
    for ln in lines:
        out.extend(ln.splitlines() or [""])
    return out


def frames(sc: list[tuple[str, list[str]]]):
    """Yield PIL frames: typed commands, line-revealed output."""
    done: list[str] = []                       # completed transcript lines
    for title, raw_lines in sc:
        lines = flat(raw_lines)
        header = done + [""]
        typed: list[str] = []
        for line in lines:
            if line.startswith("$"):
                base = len(typed)
                for n in range(1, len(line) + 1, 3):
                    img = draw_frame(title, header + typed[:base] + [line[:n]])
                    yield img
                typed.append(line)
                yield draw_frame(title, header + typed)
            else:
                typed.append(line)
                img = draw_frame(title, header + typed)
                yield img
                # keep a couple of identical frames for readability
                yield draw_frame(title, header + typed)
        done = header + typed
    yield draw_frame("sessionFlow", done + [
        "",
        "one searchable memory layer for every AI coding agent.",
        "github.com/HarryHeYu/sessionFlow"])


def draw_frame(title: str, lines: list[str]) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, TITLE_H], fill=TITLE_BG)
    d.text((14, 11), "● ● ●", font=font(11), fill=DIM)
    d.text((90, 11), title, font=font(15, bold=True), fill=FG)
    f = font(14)
    y = TITLE_H + PAD
    for line in lines[-(H - TITLE_H - 2 * PAD) // LINE_H:]:
        color = PROMPT if line.startswith("$") else FG
        d.text((PAD, y), line.rstrip(), font=f, fill=color)
        y += LINE_H
    d.rectangle([0, H - 30, W, H], fill=BG)   # mask any transcript tail
    d.text((PAD, H - 26), "synthetic demo data — sessionFlow local, no cloud",
           font=font(12), fill=DIM)
    return img


def main() -> None:
    fr = list(frames(scenes()))
    # drop consecutive duplicates to keep the GIF small
    uniq: list[Image.Image] = []
    for im in fr:
        if not uniq or list(im.convert("RGB").getdata()) != list(
                uniq[-1].convert("RGB").getdata()):
            # quantize BEFORE saving and force full-frame disposal: PIL's
            # delta-frame optimization blends partial updates otherwise
            uniq.append(im.convert("RGB").quantize(
                colors=64, method=Image.MEDIANCUT))
    out = REPO / "docs" / "assets" / "demo.gif"
    uniq[0].save(out, save_all=True, append_images=uniq[1:],
                 duration=TYPE_MS, loop=0, optimize=False, disposal=2)
    print(f"wrote {out}: {len(uniq)} frames, "
          f"{out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
