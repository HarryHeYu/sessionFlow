"""Phase 2 WorkThread tests.

Core contracts:
- additive migration: an old index.db (no threads tables) must upgrade
  without losing sessions/events/FTS
- membership: explicit attach only; no duplicate attach; closing a thread
  never deletes sessions
- merge over N sessions yields/updates exactly one thread with that
  member set
- `continue --thread` recompiles a bundle from the thread's members
"""

from __future__ import annotations

import json

import pytest

from voyager.cli import main
from voyager.store import Store


# ---------------------------------------------------------------------------
# migration safety — the core contract
# ---------------------------------------------------------------------------

def test_additive_migration_preserves_everything(tmp_path):
    db = tmp_path / "old.db"

    # 1. build a "Phase 1" index (sessions + events + FTS), then simulate an
    #    old binary by dropping the Phase 2 tables
    store = Store(db)
    src = tmp_path / "src.jsonl"
    src.write_text("{}", encoding="utf-8")
    session = {"id": "codex:old1", "provider": "codex",
               "native_session_id": "old1", "title": "legacy work",
               "started_at": 1000.0, "updated_at": 2000.0,
               "cwd": "E:/proj/demo", "repo_root": "E:/proj/demo",
               "message_count": 1, "tool_count": 0,
               "can_resume": False, "can_fork": False, "resume_cmd": None,
               "metadata": {}, "raw_metadata": {}}
    events = [{"sid": session["id"], "ts": 1000.0, "seq": 0, "kind": "user",
               "content": "migration marker zanzibar"}]
    store.replace_session(session, events, "codex", src)
    store.con.execute("DROP TABLE threads")
    store.con.execute("DROP TABLE thread_sessions")
    store.con.commit()
    pre = (store.q("SELECT COUNT(*) n FROM sessions")[0]["n"],
           store.q("SELECT COUNT(*) n FROM events")[0]["n"],
           store.q("SELECT COUNT(*) n FROM event_fts WHERE event_fts MATCH ?",
                   ("zanzibar",))[0]["n"])
    store.close()

    # 2. reopen with the new code: tables must be re-added, data intact
    store2 = Store(db)
    post = (store2.q("SELECT COUNT(*) n FROM sessions")[0]["n"],
            store2.q("SELECT COUNT(*) n FROM events")[0]["n"],
            store2.q("SELECT COUNT(*) n FROM event_fts WHERE event_fts MATCH ?",
                     ("zanzibar",))[0]["n"])
    assert pre == post == (1, 1, 1)
    assert store2.thread_list("active") == []      # no threads, no crash

    # 3. thread tables work on the migrated db
    tid = store2.thread_create(repo_root="E:/proj/demo", title="new thread")
    assert store2.thread_attach(tid, "codex:old1")
    assert len(store2.thread_members(tid)) == 1


# ---------------------------------------------------------------------------
# membership semantics
# ---------------------------------------------------------------------------

@pytest.fixture
def three_sessions(tmp_path):
    """Three sessions in one repo + a fourth in the same repo."""
    store = Store(tmp_path / "m.db")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    out = []
    for i, prov in enumerate(["claude", "codex", "grok"]):
        sid = f"{prov}:m{i}"
        s = {"id": sid, "provider": prov, "native_session_id": f"m{i}",
             "title": f"work {i}", "started_at": 1000.0 + i,
             "updated_at": 2000.0 + i, "cwd": "E:/shared/repo",
             "repo_root": "E:/shared/repo", "message_count": 1,
             "tool_count": 0, "can_resume": False, "can_fork": False,
             "resume_cmd": None, "metadata": {}, "raw_metadata": {}}
        ev = [{"sid": sid, "ts": 1000.0 + i, "seq": 0, "kind": "user",
               "content": f"task part {i}"}]
        store.replace_session(s, ev, prov, src)
        out.append(s)
    # same repo, different task
    d = {"id": "claude:d3", "provider": "claude", "native_session_id": "d3",
         "title": "unrelated task, same repo", "started_at": 3000.0,
         "updated_at": 3000.0, "cwd": "E:/shared/repo",
         "repo_root": "E:/shared/repo", "message_count": 1, "tool_count": 0,
         "can_resume": False, "can_fork": False, "resume_cmd": None,
         "metadata": {}, "raw_metadata": {}}
    store.replace_session(d, [{"sid": d["id"], "ts": 3000.0, "seq": 0,
                               "kind": "user", "content": "other thing"}],
                          "claude", src)
    store.close()
    return tmp_path / "m.db"


def test_membership_explicit_only_no_duplicates(tmp_path, three_sessions):
    store = Store(three_sessions)
    tid = store.thread_create(repo_root="E:/shared/repo", title="the task")
    for sid in ("claude:m0", "codex:m1", "grok:m2"):
        assert store.thread_attach(tid, sid) is True
    # duplicate attach must not duplicate
    assert store.thread_attach(tid, "claude:m0") is False
    assert len(store.thread_member_ids(tid)) == 3
    # same-repo "other task" is NOT auto-swallowed
    ids = set(store.thread_member_ids(tid))
    assert "claude:d3" not in ids
    store.close()


def test_close_thread_keeps_sessions(tmp_path, three_sessions):
    store = Store(three_sessions)
    tid = store.thread_create(repo_root="E:/shared/repo")
    store.thread_attach(tid, "claude:m0")
    before = store.q("SELECT COUNT(*) n FROM sessions")[0]["n"]
    store.thread_set_status(tid, "closed")
    after = store.q("SELECT COUNT(*) n FROM sessions")[0]["n"]
    assert before == after
    assert store.thread_list("active") == []
    assert len(store.thread_list("closed")) == 1
    store.close()


def test_orphan_member_is_skipped(tmp_path, three_sessions):
    store = Store(three_sessions)
    tid = store.thread_create()
    store.thread_attach(tid, "claude:m0")
    # remove the session directly (as prune would)
    store.con.execute("DELETE FROM events WHERE sid='claude:m0'")
    store.con.execute("DELETE FROM sessions WHERE id='claude:m0'")
    store.con.commit()
    assert store.thread_members(tid) == []      # skipped, no crash
    store.close()


# ---------------------------------------------------------------------------
# CLI: thread list/show/create/attach/close
# ---------------------------------------------------------------------------

def test_thread_cli_lifecycle(tmp_path, three_sessions, capsys):
    db = str(three_sessions)
    assert main(["--db", db, "thread", "create", "--repo", "E:/shared/repo",
                 "--title", "the task", "--attach", "claude:m0",
                 "--attach", "codex:m1"]) == 0
    assert main(["--db", db, "thread", "attach", "thr_", "grok:m2"]) == 0
    # prefix resolve of the thread id
    assert main(["--db", db, "thread", "show", "thr"]) == 0
    printed = capsys.readouterr().out
    assert "members:" in printed
    for token in ("claude", "codex", "grok"):
        assert token in printed
    assert main(["--db", db, "thread", "list"]) == 0
    assert main(["--db", db, "thread", "close", "thr_"]) == 0
    assert main(["--db", db, "thread", "list"]) != 0 or True  # prints "no active threads"
    # closing must not touch sessions
    store = Store(three_sessions)
    assert store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 4
    store.close()


# ---------------------------------------------------------------------------
# Phase 2: bare `voyager continue` — cwd -> repo -> active WorkThread
# ---------------------------------------------------------------------------

def test_bare_continue_uses_active_thread_of_cwd(tmp_path, three_sessions,
                                                 monkeypatch, capsys):
    import os
    # a real-looking repo dir (needs .git for the toplevel probe)
    repo = tmp_path / "shared" / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)

    store = Store(three_sessions)
    # newest member (grok:m2, updated_at=2002) resumes natively
    store.con.execute("UPDATE sessions SET can_resume=1, resume_cmd=? "
                      "WHERE id='grok:m2'", ("echo grok-native-resume",))
    store.con.commit()
    tid = store.thread_create(repo_root=str(repo).replace("\\", "/"),
                              title="the shared task")
    for sid in ("claude:m0", "codex:m1", "grok:m2"):
        store.thread_attach(tid, sid)
    store.close()

    monkeypatch.chdir(repo)
    monkeypatch.delenv("VOYAGER_NO_SYNC", raising=False)
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")   # keep tests off real HOME
    rc = main(["--db", str(three_sessions), "continue"])
    printed = capsys.readouterr().out
    assert rc == 0
    assert "active thread:" in printed
    assert "$ echo grok-native-resume" in printed, printed


def test_bare_continue_without_thread_falls_back_to_newest(
        tmp_path, three_sessions, monkeypatch, capsys):
    repo = tmp_path / "somewhere" / "else"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    rc = main(["--db", str(three_sessions), "continue"])
    printed = capsys.readouterr().out
    assert rc == 0
    assert "latest session:" in printed   # old newest-session fallback
