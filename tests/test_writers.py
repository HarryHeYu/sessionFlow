"""Issue #10 transcript transplant tests (opt-in, lease-gated).

Contracts:
- codex/grok writers produce a NEW native session (new id, never touching
  existing files) that parses back through the platform adapter
  (round-trip gate against schema drift)
- lease gate: no lease / wrong holder / stale heartbeat -> RuntimeError
- unsupported providers (claude/dsh/zcode/cursor/antigravity/kiro) fail
  loudly with a reason, never a silent fallback
- only user/assistant text survives (tool calls / reasoning dropped)
- switch --mode transcript requires the gate and records the new id on
  the lease; provider files outside the new session are untouched
"""

from __future__ import annotations

import json
from pathlib import Path
import os
import time

import pytest

from voyager.adapters.base import get_adapter
from voyager.cli import main
from voyager.store import Store
from voyager.writers import (
    SUPPORTED_WRITERS,
    UNSUPPORTED_REASON,
    write_transcript,
    writer_supported,
)


@pytest.fixture
def leased_thread(tmp_path, monkeypatch):
    """An isolated HOME + a thread with codex/claude members and a codex lease."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    db = tmp_path / "l.db"
    store = Store(db)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    for i, prov in enumerate(["codex", "claude"]):
        s = {"id": f"{prov}:t{i}", "provider": prov,
             "native_session_id": f"t{i}", "title": f"work {i}",
             "started_at": 1000.0 + i, "updated_at": 2000.0 + i,
             "cwd": "E:/proj/demo", "repo_root": "E:/proj/demo",
             "message_count": 1, "tool_count": 0, "can_resume": False,
             "can_fork": False, "resume_cmd": None,
             "metadata": {}, "raw_metadata": {}}
        store.replace_session(
            s, [{"sid": s["id"], "ts": 2000.0 + i, "seq": 0,
                 "kind": "user", "content": f"transcript turn {i}: 中文也支持"}],
            prov, src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="transplant me")
    for sid in ("codex:t0", "claude:t1"):
        store.thread_attach(tid, sid)
    yield store, tid, home
    store.close()


def test_gate_table_matches_probe():
    assert SUPPORTED_WRITERS == {"codex", "grok"}
    assert not writer_supported("claude")
    assert "TIMEOUT" in UNSUPPORTED_REASON["claude"]
    assert not writer_supported("dsh") and "unverified" in \
        UNSUPPORTED_REASON["dsh"]


def test_lease_gate_no_lease(tmp_path):
    store = Store(tmp_path / "x.db")
    store.thread_create(repo_root="E:/x")
    with pytest.raises(RuntimeError, match="requires a lease"):
        write_transcript(store, store.thread_list("active")[0]["id"], "codex",
                         home=tmp_path)
    store.close()


def test_lease_gate_wrong_holder(tmp_path, leased_thread):
    store, tid, home = leased_thread
    ok, _ = store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert ok
    with pytest.raises(RuntimeError, match="held by codex, not grok"):
        write_transcript(store, tid, "grok", home=home)


def test_lease_gate_stale_heartbeat(tmp_path, leased_thread):
    store, tid, home = leased_thread
    ok, _ = store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert ok
    store.con.execute(
        "UPDATE thread_leases SET heartbeat_at=? WHERE thread_id=?",
        (time.time() - 120, tid))
    store.con.commit()
    with pytest.raises(RuntimeError, match="stale"):
        write_transcript(store, tid, "codex", home=home)


def test_codex_writer_round_trip(tmp_path, leased_thread):
    store, tid, home = leased_thread
    ok, _ = store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert ok
    r = write_transcript(store, tid, "codex", home=home)
    assert r["turns"] == 2                       # both user turns, tool dropped
    assert r["native_session_id"] != "t0"
    # lease records the new native id
    assert store.thread_lease_get(tid)["native_session_id"] == \
        r["native_session_id"]

    # round-trip: the codex adapter must discover + parse the new file
    from voyager.adapters.base import get_adapter
    f = r["path"]
    parsed = get_adapter("codex").parse(f)
    assert parsed and "__error__" not in parsed
    assert parsed["session"]["native_session_id"] == r["native_session_id"]
    # flattened: user/assistant only, Chinese intact, no tool events
    kinds = [e["kind"] for e in parsed["events"]]
    assert set(kinds) <= {"user", "assistant"}
    texts = " ".join(e["content"] or "" for e in parsed["events"])
    assert "transcript turn 0" in texts and "中文也支持" in texts


def test_codex_writer_never_touches_existing_sessions(tmp_path, leased_thread):
    store, tid, home = leased_thread
    ok, _ = store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert ok
    sessions_root = home / ".codex" / "sessions"
    assert not sessions_root.exists()          # nothing existed before
    write_transcript(store, tid, "codex", home=home)
    files = sorted(str(p) for p in sessions_root.rglob("*") if p.is_file())
    # exactly one new file, containing the NEW session id only
    assert len(files) == 1
    content = Path(files[0]).read_text(encoding="utf-8")
    new_id = store.thread_lease_get(tid)["native_session_id"]
    assert new_id in content
    assert "t0" not in new_id


def test_grok_writer_round_trip(tmp_path, leased_thread):
    store, tid, home = leased_thread
    ok, _ = store.thread_lease_acquire(tid, "grok", pid=os.getpid())
    assert ok
    r = write_transcript(store, tid, "grok", home=home)
    assert r["sid"].startswith("grok:")
    from voyager.adapters.base import get_adapter
    f = r["path"]
    parsed = get_adapter("grok").parse(f)
    assert parsed and "__error__" not in parsed
    assert parsed["session"]["native_session_id"] == r["native_session_id"]
    assert parsed["session"]["title"] == "voyager transcript transplant"


def test_unsupported_provider_fails_loudly(tmp_path, leased_thread):
    store, tid, home = leased_thread
    ok, _ = store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert ok
    for prov in ("claude", "dsh", "zcode", "cursor", "antigravity", "kiro"):
        with pytest.raises(RuntimeError, match="unsupported"):
            write_transcript(store, tid, prov, home=home)


def test_switch_mode_transcript_end_to_end(tmp_path, leased_thread, capsys):
    """switch --mode transcript: writes the synthetic session, records the
    id on the lease, prints the native resume command."""
    store, tid, home = leased_thread
    db = store.db_path
    rc = main(["--db", str(db), "switch", "codex", "--thread", tid,
               "--mode", "transcript", "--no-launch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "transcript written:" in out
    assert "codex resume" in out
    lease = store.thread_lease_get(tid)
    assert lease["native_session_id"] and lease["holder"] == "codex"


def test_switch_mode_transcript_unsupported(tmp_path, leased_thread, capsys):
    store, tid, home = leased_thread
    # lease held by claude (the "current" agent); switching to claude
    # transfers, but claude has no writer -> clear error
    rc = main(["--db", str(store.db_path), "switch", "claude",
               "--thread", tid, "--mode", "transcript", "--no-launch"])
    assert rc != 0
    cap = capsys.readouterr()
    combined = cap.out + cap.err
    assert "unsupported" in combined or "TIMEOUT" in combined
