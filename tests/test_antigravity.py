"""Antigravity adapter regression test (heuristic protobuf decode).

Fixture: tests/fixtures/antigravity/seed.sql builds a conversations/*.db with
the step_type values the adapter knows (14 init, 15 message, 132 tool call,
101 task notification with exit code, 17 error). Covers both the printable-run
extraction and the git remote detection from the init payload.
"""

from __future__ import annotations


def test_antigravity_parse(adapter_of, patch_paths, antigravity_fixture):
    ad = adapter_of("antigravity")
    patch_paths(ad, CONV_DIR=antigravity_fixture)
    files = ad.discover()
    assert len(files) == 1
    r = ad.parse(files[0])
    s, evs = r["session"], r["events"]
    assert s["native_session_id"] == "ag-1"
    assert s["title"].startswith("帮我把项目重构一遍")
    assert s["git_remote"] == "git@github.com:u/demo.git"
    kinds = [e["kind"] for e in evs]
    assert "user" in kinds and "assistant" in kinds
    assert kinds.count("tool_call") == 1
    tc = next(e for e in evs if e["kind"] == "tool_call")
    assert tc["tool_name"] == "run_command"
    assert kinds.count("error") == 1
    tr = next(e for e in evs if e["kind"] == "tool_result")
    assert tr["exit_code"] == 0
