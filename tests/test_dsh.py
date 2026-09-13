"""DSH adapter regression test.

Fixture: tests/fixtures/dsh/sessions/.../session.jsonl, zstd-compressed at
test time (the adapter reads the compressed artifact DSH writes). Also covers
the corrupt-archive path: a broken .zstd must surface as an error result, not
as a crash that aborts the scan.
"""

from __future__ import annotations


def test_dsh_parse(adapter_of, patch_paths, dsh_fixture):
    ad = adapter_of("dsh")
    patch_paths(ad, SESSIONS_DIR=dsh_fixture)
    files = ad.discover()
    assert len(files) == 1
    r = ad.parse(files[0])
    s, evs = r["session"], r["events"]
    assert s["native_session_id"] == "session-dsh-1"
    assert s["title"] == "你好，跑个测试"
    assert s["model"] == "deepseek-v4-pro"
    assert s["resume_cmd"] == "dsh --resume session-dsh-1"
    kinds = [e["kind"] for e in evs]
    assert kinds.count("user") == 1 and kinds.count("assistant") == 1
    assert kinds.count("tool_call") == 1 and kinds.count("tool_result") == 1
    assert kinds.count("reasoning") == 1


def test_dsh_corrupt_zstd_is_reported(adapter_of, patch_paths, dsh_fixture):
    ad = adapter_of("dsh")
    patch_paths(ad, SESSIONS_DIR=dsh_fixture)
    f = list(ad.discover())[0]
    f.write_bytes(b"NOT-ZSTD")
    r = ad.parse(f)
    assert r and "__error__" in r     # surfaced, not a crash
