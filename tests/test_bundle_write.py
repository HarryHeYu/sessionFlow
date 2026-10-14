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


# --- the default name is unique, so no invocation overwrites another ---------
#
# Atomic replacement makes a torn read impossible; it does nothing about two
# invocations choosing the same path.  These two properties are independent and
# both are needed, so both are pinned separately.

_ROWS = [{"provider": "claude", "native_id": "m2"},
         {"provider": "codex", "native_id": "m1"}]


def test_two_merges_in_the_same_second_get_different_names():
    """The old name was members + `int(time.time())`, so these collided."""
    first = cont.default_bundle_name(_ROWS)
    second = cont.default_bundle_name(_ROWS)
    assert first != second, (
        "two merges produced the same path; the second would silently "
        "overwrite the first")
    assert first.endswith(".md") and second.endswith(".md")


def test_the_default_name_keeps_its_documented_prefix():
    """`bundle-merged-*.md` is what users glob for; it must keep working."""
    name = cont.default_bundle_name(_ROWS)
    assert name.startswith("bundle-merged-2sessions-claude-codex-")


def test_a_single_session_name_stays_deterministic():
    """One session re-exported is meant to land on the same file."""
    rows = [{"provider": "codex", "native_id": "1111-2222-3333"}]
    assert (cont.default_bundle_name(rows)
            == cont.default_bundle_name(rows)
            == "bundle-codex-1111-2222-3333.md")


def test_the_name_is_filesystem_safe():
    rows = [{"provider": "codex", "native_id": 'a/b\\c:d*e?f"g<h>i|j'},
            {"provider": "claude", "native_id": "m2"}]
    name = cont.default_bundle_name(rows)
    assert not (set(name) & set('/\\:*?"<>|')), name


def test_concurrent_writes_of_one_target_do_not_share_a_staging_file(tmp_path):
    """Two threads of one process must not fight over the temp file."""
    import threading

    target = tmp_path / "bundle.md"
    payloads = ["A" * 20000, "B" * 20000]
    errors = []

    def writer(text):
        try:
            for _ in range(20):
                cont._write_atomic(target, text)
        except Exception as exc:          # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    # Whatever won, the file is one complete payload - never a mix, never empty.
    assert target.read_text(encoding="utf-8") in payloads
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
