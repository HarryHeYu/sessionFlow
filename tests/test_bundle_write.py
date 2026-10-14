"""The bundle write path: a reader must never see a half-written bundle.

A bundle is read back immediately after it is written (`--json` returns the
compiled context) and can be read by another process entirely — a background
`voyager watch`, an editor, a test suite.  The default bundle name is the member
list plus `int(time.time())`, so two merges of the same sessions in the same
second target the *same* file.  Ten parallel plugin test processes doing exactly
that produced an empty read once in eighty runs, which is what these tests pin
down.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from voyager import continuity as cont


def test_the_destination_is_replaced_never_truncated(tmp_path, monkeypatch):
    """At the moment of the swap the old file must still be complete.

    This is the property that makes a concurrent read safe: if the destination
    were opened for writing, a reader arriving mid-write would get a short file.
    """
    target = tmp_path / "bundle.md"
    target.write_text("old bundle", encoding="utf-8")

    seen = {}
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        seen["destination"] = Path(dst).read_text(encoding="utf-8")
        seen["staged"] = Path(src).read_text(encoding="utf-8")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    cont._write_atomic(target, "new bundle")

    assert seen["destination"] == "old bundle", (
        "the destination was truncated before the swap; a reader could have "
        "seen a partial bundle")
    assert seen["staged"] == "new bundle"
    assert target.read_text(encoding="utf-8") == "new bundle"


def test_a_failed_write_leaves_the_previous_bundle_intact(tmp_path, monkeypatch):
    """A disk-full part-way through must not destroy the bundle that was there."""
    target = tmp_path / "bundle.md"
    target.write_text("old bundle", encoding="utf-8")

    real_write_text = Path.write_text

    def failing(self, data, *args, **kwargs):
        if self.name.startswith("bundle.md.tmp-"):
            real_write_text(self, str(data)[:4], *args, **kwargs)  # partial
            raise OSError("no space left on device")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing)
    with pytest.raises(OSError):
        cont._write_atomic(target, "new bundle")

    assert target.read_text(encoding="utf-8") == "old bundle"
    assert list(tmp_path.glob("*.tmp-*")) == [], "a temp file was left behind"


def test_no_temp_file_survives_a_successful_write(tmp_path):
    target = tmp_path / "bundle.md"
    cont._write_atomic(target, "a bundle")
    assert target.read_text(encoding="utf-8") == "a bundle"
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_a_second_write_replaces_the_first(tmp_path):
    """Two merges of the same members in the same second share a path."""
    target = tmp_path / "bundle.md"
    cont._write_atomic(target, "first")
    cont._write_atomic(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_the_engine_actually_writes_through_the_atomic_path(
        synthetic_trio, tmp_path, monkeypatch):
    """The helper being correct is not enough — the engine has to use it.

    Without this, reverting the call site to ``out.write_text`` leaves every
    test above passing while the race comes straight back.
    """
    store, rows = synthetic_trio
    out = tmp_path / "bundle.md"
    out.write_text("previous bundle", encoding="utf-8")

    seen = {}
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        seen["destination"] = Path(dst).read_text(encoding="utf-8")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    res = cont.handoff_thread(store, sessions=rows, goal="g",
                              style="continuation", output=out)

    assert res["context_path"] == str(out.resolve())
    assert seen.get("destination") == "previous bundle", (
        "handoff_thread wrote the bundle in place: a concurrent reader could "
        "have seen a partial file")
    assert out.read_text(encoding="utf-8").startswith("# Continuation Bundle")
    assert list(tmp_path.glob("*.tmp-*")) == []
