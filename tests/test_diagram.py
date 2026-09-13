"""The README diagram: layout invariants + committed assets.

`scripts/make_diagram.py` owns the layout; these tests keep it *balanced* —
equal column widths, even outer margins, a centred index box, left/right
stacks of identical height, no label overflowing its box — and keep the
committed PNG/SVG consistent with it. They need Pillow (the `dev` extra) and
skip in the core-only CI job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_diagram.py"


@pytest.fixture(scope="module")
def diagram():
    pytest.importorskip("PIL", reason="Pillow not installed (dev extra)")
    spec = importlib.util.spec_from_file_location("make_diagram", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_layout_is_balanced_and_text_fits(diagram):
    layout = diagram._layout()
    diagram._check_layout(layout)      # asserts balance, centring and fit

    assert layout["left_span"] == layout["right_span"], "stacks differ in height"
    assert layout["lx"] == diagram.W - layout["r_right"] == diagram.MARGIN
    assert layout["mx"] - layout["l_right"] == layout["rx"] - layout["m_right"]
    assert layout["mid_centre_x"] == layout["col_centre_x"] == diagram.W / 2
    assert abs(layout["col_centre_y"] - diagram.H / 2) <= 16, "block not centred"


def test_committed_assets_match_the_layout(diagram):
    from PIL import Image

    assert diagram.OUT_PNG.is_file(), "run: python scripts/make_diagram.py"
    assert diagram.OUT_SVG.is_file(), "run: python scripts/make_diagram.py"
    assert Image.open(diagram.OUT_PNG).size == (diagram.W, diagram.H)
    svg = diagram.OUT_SVG.read_text(encoding="utf-8")
    assert f'viewBox="0 0 {diagram.W} {diagram.H}"' in svg
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")


def test_diagram_still_shows_every_platform_and_use_case(diagram):
    assert len(diagram.PROVIDERS) == 8, "README claims 8 platforms"
    assert len(diagram.OUTPUTS) == 6
    assert all(fmt for _, fmt in diagram.PROVIDERS)
    assert "voyager scan" in diagram.FOOTER_2
    assert "no telemetry" in diagram.FOOTER_1
