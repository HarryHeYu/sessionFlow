"""Cursor adapter regression test.

Fixture: tests/fixtures/cursor/seed.sql builds a synthetic state.vscdb with
the cursorDiskKV key-value shape Cursor writes (composerData + bubbleId rows).
"""

from __future__ import annotations


def test_cursor_scan(adapter_of, patch_paths, cursor_fixture):
    ad = adapter_of("cursor")
    patch_paths(ad, VSCDB=cursor_fixture)
    bundles = ad.scan(lambda p, f: True)
    assert len(bundles) == 1
    s, evs = bundles[0]["session"], bundles[0]["events"]
    assert s["title"] == "cursor demo"
    assert s["repo_root"] == "E:/proj/demo"
    assert s["model"] == "gpt-x"
    kinds = [e["kind"] for e in evs]
    assert kinds.count("user") == 1 and kinds.count("assistant") == 2
    assert kinds.count("tool_call") == 1
    tc = next(e for e in evs if e["kind"] == "tool_call")
    assert tc["tool_call_id"] == "cb1"
