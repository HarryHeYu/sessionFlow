"""Phase 6 `voyager switch <agent>` tests (roadmap #7).

Contracts (D7/D11/D12/D13):
- lease consumption: a live foreign lease blocks the switch and names the
  holder; --steal takes it over (logged); a stale lease never blocks
- same-provider native resume has priority (D7); cross-provider gets a
  Continuation Bundle + a pending attach (never a fabricated session id)
- launch failure releases the lease (no dangling live lease)
- git dirty tree only warns
- goal/budget flow into the bundle
- provider files are never written
"""

from __future__ import annotations

import os
import subprocess

import pytest

from pathlib import Path

from voyager.cli import main
from voyager.store import LEASE_HEARTBEAT_TIMEOUT, Store


@pytest.fixture
def thread_db(tmp_path):
    """Yields (db_path, thread_id, store)."""
    db = tmp_path / "s.db"
    store = Store(db)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    for i, prov in enumerate(["codex", "claude"]):
        s = {"id": f"{prov}:m{i}", "provider": prov,
             "native_session_id": f"m{i}", "title": f"work {i}",
             "started_at": 1000.0 + i, "updated_at": 2000.0 + i,
             "cwd": "E:/proj/demo", "repo_root": "E:/proj/demo",
             "message_count": 2, "tool_count": 1,
             "can_resume": prov == "codex",
             "can_fork": False,
             "resume_cmd": f"codex resume m{i}" if prov == "codex" else None,
             "metadata": {}, "raw_metadata": {}}
        store.replace_session(
            s, [{"sid": s["id"], "ts": 2000.0 + i, "seq": 0,
                 "kind": "user", "content": f"switch test work {i}"}],
            prov, src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="the task")
    for sid in ("codex:m0", "claude:m1"):
        store.thread_attach(tid, sid)
    yield str(db), tid, store
    store.close()


def _hold_lease(store, tid, holder):
    ok, _ = store.thread_lease_acquire(tid, holder, pid=os.getpid())
    assert ok


def test_switch_refused_while_foreign_lease_live(tmp_path, thread_db, capsys):
    db, tid, store = thread_db
    _hold_lease(store, tid, "grok")
    rc = main(["--db", db, "switch", "claude", "--thread", tid, "--no-launch"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "leased to grok" in out and "--steal" in out
    assert store.thread_lease_get(tid)["holder"] == "grok"


def test_switch_with_steal_takes_over_and_logs(tmp_path, thread_db, capsys):
    db, tid, store = thread_db
    _hold_lease(store, tid, "grok")
    rc = main(["--db", db, "switch", "claude", "--thread", tid, "--no-launch",
               "--steal"])
    assert rc == 0
    assert store.thread_lease_get(tid)["holder"] == "claude"
    assert "steal" in (store.db_path.parent / "leases.log").read_text(
        encoding="utf-8")


def test_switch_takes_over_stale_lease_without_steal(tmp_path, thread_db):
    db, tid, store = thread_db
    _hold_lease(store, tid, "grok")
    import time
    store.con.execute(
        "UPDATE thread_leases SET heartbeat_at=? WHERE thread_id=?",
        (time.time() - (LEASE_HEARTBEAT_TIMEOUT + 5), tid))
    store.con.commit()
    rc = main(["--db", db, "switch", "claude", "--thread", tid, "--no-launch"])
    assert rc == 0
    assert store.thread_lease_get(tid)["holder"] == "claude"


def test_switch_same_provider_native_resume(tmp_path, thread_db, capsys):
    db, tid, store = thread_db
    rc = main(["--db", db, "switch", "codex", "--thread", tid, "--no-launch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "$ codex resume m0" in out
    assert "continuation bundle" not in out
    assert store.thread_lease_get(tid)["holder"] == "codex"


def test_switch_cross_provider_compiles_bundle_and_pending(
        tmp_path, thread_db, capsys):
    db, tid, store = thread_db
    rc = main(["--db", db, "switch", "claude", "--thread", tid, "--no-launch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "continuation bundle:" in out
    assert "resolves the pending attach" in out
    lease = store.thread_lease_get(tid)
    assert lease["holder"] == "claude"
    pend = store.thread_pending_list(tid)
    assert len(pend) == 1 and pend[0]["provider"] == "claude"


def test_switch_launch_failure_releases_lease(tmp_path, thread_db, capsys):
    db, tid, store = thread_db
    # a native resume command that cannot exist
    store.con.execute("UPDATE sessions SET resume_cmd=? WHERE id='codex:m0'",
                      ("definitely-not-a-real-binary-xyz --resume",))
    store.con.commit()
    rc = main(["--db", db, "switch", "codex", "--thread", tid])
    cap = capsys.readouterr()
    assert rc == 1, (cap.out, cap.err)
    out = cap.out + cap.err
    assert "lease released" in out
    assert store.thread_lease_get(tid) is None


def test_switch_goal_and_budget_flow_into_bundle(tmp_path, thread_db):
    db, tid, store = thread_db
    out = tmp_path / "b.md"
    assert main(["--db", db, "switch", "claude", "--thread", tid, "--no-launch",
                 "--goal", "switch test work", "--budget", "compact",
                 "-o", str(out)]) == 0
    content = out.read_text(encoding="utf-8")
    assert 'goal: "switch test work"' in content
    assert "## Goal-ranked evidence" in content
    from voyager.budget import estimate_tokens
    assert estimate_tokens(content) <= 4000


def test_switch_warns_on_dirty_repo(tmp_path, thread_db, monkeypatch, capsys):
    db, tid, store = thread_db
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    # make the thread resolve via cwd by matching repo_root
    store.con.execute("UPDATE threads SET repo_root=? WHERE id=?",
                      (str(repo).replace("\\", "/"), tid))
    store.con.commit()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    rc = main(["--db", db, "switch", "claude", "--thread", tid, "--no-launch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "uncommitted change(s)" in out


def test_switch_no_thread_is_actionable(tmp_path, capsys):
    db = tmp_path / "empty.db"
    Store(db).close()
    rc = main(["--db", str(db), "switch", "claude", "--no-launch"])
    assert rc == 1
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "no active WorkThread" in out
    assert "voyager thread create" in out


def test_switch_unknown_agent(tmp_path, thread_db):
    db, tid, store = thread_db
    assert main(["--db", db, "switch", "not-an-agent"]) == 2


def test_switch_never_writes_provider_files(tmp_path, thread_db):
    db, tid, store = thread_db
    src = Path(db).parent / "s.jsonl"
    before = src.read_bytes()
    assert main(["--db", db, "switch", "claude", "--thread", tid, "--no-launch"]) == 0
    assert src.read_bytes() == before


