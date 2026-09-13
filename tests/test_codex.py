"""Codex adapter regression test.

Fixture: tests/fixtures/codex/sessions/.../rollout-*.jsonl — session_meta,
user/assistant messages, reasoning, function_call + output, token_usage_record.
If Codex changes its rollout format this test is the tripwire.
"""

from __future__ import annotations


def test_codex_parse(adapter_of, patch_paths, codex_fixture):
    ad = adapter_of("codex")
    patch_paths(ad, SESSIONS_DIR=codex_fixture)
    files = ad.discover()
    assert len(files) == 1
    r = ad.parse(files[0])
    assert r and "__error__" not in r
    s, evs = r["session"], r["events"]
    assert s["native_session_id"] == "11111111-2222-3333-4444-555555555555"
    assert s["cwd"] == "E:/proj/demo"
    assert s["git_branch"] == "main" and s["git_commit"] == "abc1234"
    assert s["git_remote"] == "git@github.com:u/demo.git"
    assert s["resume_cmd"] == "codex resume 11111111-2222-3333-4444-555555555555"
    kinds = [e["kind"] for e in evs]
    assert kinds.count("user") == 1 and kinds.count("assistant") == 1
    assert kinds.count("tool_call") == 1 and kinds.count("tool_result") == 1
    assert kinds.count("reasoning") == 1
    tr = next(e for e in evs if e["kind"] == "tool_result")
    assert tr["exit_code"] == 0 and "all good" in tr["tool_output"]
    assert s["metadata"]["usage_totals"]["input"] == 100


def test_codex_malformed_lines_survive(adapter_of, patch_paths, codex_fixture):
    ad = adapter_of("codex")
    patch_paths(ad, SESSIONS_DIR=codex_fixture)
    f = list(ad.discover())[0]
    f.write_text("GARBAGE\n" + f.read_text(encoding="utf-8"), encoding="utf-8")
    r = ad.parse(f)
    assert r and len(r["events"]) == 5   # garbage line skipped, rest parsed
