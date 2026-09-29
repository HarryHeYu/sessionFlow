"""Payload budget tests: the handlers must stay under the provider cap.

Every provider that speaks the Claude-compatible envelope caps the injected
string at 10,000 characters, and Codex truncates silently when a payload exceeds
it -- keeping the head and the tail and eliding the middle, which for tiered-v1
removes the newest L1 turns.  These tests pin the shared cap so that never
happens again:

  * measurement is in UTF-16 code units, the way a JavaScript provider counts
  * a fitting context is passed through untouched and nothing is spilled
  * an oversized context is cut to the budget and the full copy is spilled to
    Voyager's own directory (never %TEMP%, which external processes clean)
  * the capped payload is always within budget, even with a long spill path
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from voyager.integrations import hook_payload
from voyager.integrations.hook_payload import (
    MAX_ADDITIONAL_CONTEXT_CHARS,
    cap_with_note,
    payload_len,
    spill_bundle,
    truncate_to_budget,
)


@pytest.fixture(autouse=True)
def isolated_spill_dir(tmp_path, monkeypatch):
    """Never write spills into the real ~/.voyager/context during tests."""
    d = tmp_path / "spill"
    monkeypatch.setenv("VOYAGER_CONTEXT_DIR", str(d))
    return d


# --- measurement -----------------------------------------------------------

def test_payload_len_counts_utf16_code_units():
    assert payload_len("abc") == 3
    # an astral character is one code point but two UTF-16 units
    assert payload_len("\U0001F600") == 2
    assert payload_len("a\U0001F600b") == 4
    # BMP characters stay 1:1
    assert payload_len("中文") == 2


def test_truncate_to_budget_never_exceeds_the_budget():
    text = "x" * 100
    assert payload_len(truncate_to_budget(text, 10)) <= 10
    # all-astral text: slicing by a code-point count would remove twice as much
    astral = "\U0001F600" * 50
    cut = truncate_to_budget(astral, 10)
    assert payload_len(cut) <= 10
    assert payload_len(cut) >= 10 - 1, "should not over-truncate"
    assert truncate_to_budget(text, 0) == ""
    assert truncate_to_budget(text, -5) == ""


# --- capping ---------------------------------------------------------------

def test_a_fitting_context_is_untouched_and_not_spilled(isolated_spill_dir):
    ctx = "small context"
    payload, spilled = cap_with_note(ctx, "codex")
    assert payload == ctx
    assert spilled is None
    assert not isolated_spill_dir.exists() or not any(isolated_spill_dir.iterdir())


def test_an_oversized_context_is_capped_and_spilled(isolated_spill_dir):
    ctx = "y" * (MAX_ADDITIONAL_CONTEXT_CHARS * 2)
    payload, spilled = cap_with_note(ctx, "codex")

    assert payload_len(payload) <= MAX_ADDITIONAL_CONTEXT_CHARS
    assert spilled is not None and spilled.is_file()
    # the full bundle is on disk, uncut
    assert spilled.read_text(encoding="utf-8") == ctx
    # the note tells the model where the rest is
    assert "truncated for the hook payload" in payload
    assert str(spilled) in payload


def test_the_cap_holds_even_with_a_long_spill_path(isolated_spill_dir, monkeypatch):
    deep = isolated_spill_dir / ("d" * 120) / ("e" * 120)
    monkeypatch.setenv("VOYAGER_CONTEXT_DIR", str(deep))
    ctx = "z" * (MAX_ADDITIONAL_CONTEXT_CHARS * 3)
    payload, spilled = cap_with_note(ctx, "codex")
    assert payload_len(payload) <= MAX_ADDITIONAL_CONTEXT_CHARS


def test_spills_land_under_voyager_not_temp(isolated_spill_dir):
    spill_bundle("hello", "codex")
    files = list(isolated_spill_dir.glob("codex-sessionstart-*.md"))
    assert files, "a spill must land in the configured Voyager directory"
    # the invariant that matters: the destination is the Voyager context dir,
    # not the process TEMP directory
    assert files[0].parent == isolated_spill_dir
    assert hook_payload.spill_dir() == isolated_spill_dir


def test_spill_pruning_keeps_the_newest(isolated_spill_dir, monkeypatch):
    monkeypatch.setattr(hook_payload, "SPILL_KEEP", 3)
    isolated_spill_dir.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        p = isolated_spill_dir / ("codex-sessionstart-%d-1.md" % (1000 + i))
        p.write_text("x" * 10, encoding="utf-8")
        import os
        os.utime(p, (1000 + i, 1000 + i))
    hook_payload._prune_spills(isolated_spill_dir)
    left = sorted(p.name for p in isolated_spill_dir.glob("*.md"))
    assert len(left) == 3
    assert left[-1].endswith("1005-1.md"), left


# --- the handlers actually use it ------------------------------------------

def test_codex_emit_caps_a_huge_context(monkeypatch, isolated_spill_dir):
    from voyager.integrations import codex_session_start as ch

    huge = "c" * (MAX_ADDITIONAL_CONTEXT_CHARS * 2)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ch.emit({"status": "context_ready", "context": huge}) == 0

    payload = json.loads(out.getvalue())
    delivered = payload["hookSpecificOutput"]["additionalContext"]
    assert payload_len(delivered) <= MAX_ADDITIONAL_CONTEXT_CHARS
    assert delivered != huge


def test_zcode_emit_caps_a_huge_context(monkeypatch, isolated_spill_dir):
    from voyager.integrations import zcode_session_start as zh

    huge = "z" * (MAX_ADDITIONAL_CONTEXT_CHARS * 2)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert zh.emit({"status": "context_ready", "context": huge}) == 0
    payload = json.loads(out.getvalue())
    assert payload_len(
        payload["hookSpecificOutput"]["additionalContext"]
    ) <= MAX_ADDITIONAL_CONTEXT_CHARS


def test_cursor_emit_caps_a_huge_context(monkeypatch, isolated_spill_dir):
    from voyager.integrations import cursor_session_start as uh

    huge = "u" * (MAX_ADDITIONAL_CONTEXT_CHARS * 2)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert uh.emit({"status": "context_ready", "context": huge}) == 0
    payload = json.loads(out.getvalue())
    assert payload_len(payload["additional_context"]) <= MAX_ADDITIONAL_CONTEXT_CHARS


def test_claude_handler_shares_the_same_implementation():
    """The Claude handler aliases the shared helpers, so the caps cannot drift."""
    from voyager.integrations import claude_session_start as cl

    assert cl.MAX_ADDITIONAL_CONTEXT_CHARS == MAX_ADDITIONAL_CONTEXT_CHARS
    assert cl._payload_len is payload_len
    assert cl._truncate_to_budget is truncate_to_budget


# --- tiered documents keep the NEWEST L1 turns ------------------------------

def _tiered(preamble_pad=0, turns=40, turn_chars=400):
    head = ("format: tiered-v1\n\n[L0 Thread State]\n[WorkThread]\nid: thr_x\n"
            "[Runtime State]\nrepository: E:/repo\n"
            "[Historical Evidence]\nfull history is not inlined.\n" + "p" * preamble_pad)
    body = "".join("[prov:sess #%d] user: TURN-%03d %s\n" % (i, i, "t" * turn_chars)
                   for i in range(turns))
    return head + "\n[L1 Active Working Context]\n" + body


def test_tiered_cap_keeps_both_ends_of_the_l1(isolated_spill_dir):
    """Head (one newest turn per session) and tail (the newest turns) both matter,
    so the cap drops the middle instead of either end."""
    from voyager.integrations.hook_payload import cap_tiered_with_note

    doc = _tiered()
    assert payload_len(doc) > MAX_ADDITIONAL_CONTEXT_CHARS
    payload, spilled = cap_tiered_with_note(doc, "codex")

    assert payload_len(payload) <= MAX_ADDITIONAL_CONTEXT_CHARS
    assert spilled is not None
    for section in ("[L0 Thread State]", "[Runtime State]", "[Historical Evidence]",
                    "[L1 Active Working Context]"):
        assert section in payload, section
    assert "TURN-000" in payload, "the head (per-session minimum) must survive"
    assert "TURN-039" in payload, "the newest turns must survive"
    assert "elided" in payload, "the middle must be marked as elided"
    assert str(spilled) in payload


def test_tiered_cap_cuts_only_at_turn_boundaries(isolated_spill_dir):
    """Both ends must start and end on a complete turn; only whole turns survive."""
    from voyager.integrations.hook_payload import cap_tiered_with_note

    payload, _ = cap_tiered_with_note(_tiered(), "codex")
    after = payload.split("[L1 Active Working Context]", 1)[1]
    head_part, _, rest = after.partition("elided")
    for chunk, label in ((head_part, "head"), (rest, "tail")):
        lines = [l for l in chunk.split("\n") if l.startswith("[prov:sess #")]
        assert lines, label
        # every kept turn in the fixture is exactly one line of the same width,
        # so a complete turn has the same width as its siblings
        widths = {len(l) for l in lines}
        assert len(widths) == 1, (label, sorted(widths))


def test_a_fitting_tiered_document_is_untouched(isolated_spill_dir):
    from voyager.integrations.hook_payload import cap_tiered_with_note

    doc = _tiered(turns=2, turn_chars=10)
    payload, spilled = cap_tiered_with_note(doc, "codex")
    assert payload == doc
    assert spilled is None


def test_a_non_tiered_document_falls_back_to_a_head_cut(isolated_spill_dir):
    from voyager.integrations.hook_payload import cap_tiered_with_note

    doc = "HEAD" + "x" * (MAX_ADDITIONAL_CONTEXT_CHARS * 2) + "TAIL"
    payload, spilled = cap_tiered_with_note(doc, "codex")
    assert payload_len(payload) <= MAX_ADDITIONAL_CONTEXT_CHARS
    assert payload.startswith("HEAD")
    assert spilled is not None


def test_handlers_keep_both_ends_of_the_window_end_to_end(monkeypatch, isolated_spill_dir):
    from voyager.integrations import codex_session_start as ch

    doc = _tiered()
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ch.emit({"status": "context_ready", "context": doc}) == 0
    delivered = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert payload_len(delivered) <= MAX_ADDITIONAL_CONTEXT_CHARS
    assert "TURN-000" in delivered and "TURN-039" in delivered
    assert "[L1 Active Working Context]" in delivered
