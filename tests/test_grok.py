"""Grok CLI adapter regression test.

Fixture: tests/fixtures/grok/sessions/<urlencoded-cwd>/<uuid>/ with
chat_history.jsonl + summary.json (git metadata, summary title, model).
"""

from __future__ import annotations


def test_grok_parse(adapter_of, patch_paths, grok_fixture):
    ad = adapter_of("grok")
    patch_paths(ad, SESSIONS_DIR=grok_fixture)
    files = ad.discover()
    assert len(files) == 1
    r = ad.parse(files[0])
    s, evs = r["session"], r["events"]
    assert s["native_session_id"] == "01990000-1111-7777-8888-000000000000"
    assert s["title"] == "check server"
    assert s["git_remote"] == "git@github.com:u/demo.git"
    assert s["resume_cmd"] == "grok -r 01990000-1111-7777-8888-000000000000"
    kinds = [e["kind"] for e in evs]
    assert kinds.count("tool_call") == 1 and kinds.count("tool_result") == 1
    assert s["message_count"] == 3 and s["tool_count"] == 1
