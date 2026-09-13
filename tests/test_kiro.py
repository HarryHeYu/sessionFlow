"""Kiro IDE adapter regression test.

Fixture: tests/fixtures/kiro/workspace-sessions/RW==/{k1.json,sessions.json},
copied under a temp %APPDATA%/Kiro/User/globalStorage/kiro.kiroagent so the
adapter's real discovery path is exercised.
"""

from __future__ import annotations


def test_kiro_parse(adapter_of, kiro_fixture):
    ad = adapter_of("kiro")
    files = ad.discover()
    assert files, "kiro fixtures not found"
    r = ad.parse(files[0])
    s, evs = r["session"], r["events"]
    assert s["native_session_id"] == "k1"
    assert s["title"] == "kiro demo"
    assert s["cwd"] == "E:/proj/demo"
    assert s["started_at"] == 1757000000.0
    kinds = [e["kind"] for e in evs]
    assert kinds == ["user", "assistant"]
    ev0 = evs[0]
    assert ev0["files"] == ["a.md"]     # mention parts recorded as files
