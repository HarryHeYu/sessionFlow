"""WorkThread lifecycle: close, reopen, archive, and staleness.

The invariant these tests protect is the one the core refuses to bend: two active
WorkThreads for one repository is AMBIGUOUS, and *timestamps must never settle it*.
Reopening a thread can create exactly that state, so it has to say so rather than
letting the newest timestamp win quietly.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from voyager.cli import main
from voyager.store import Store


@pytest.fixture
def two_threads(tmp_path, monkeypatch, capsys):
    """One repo, two threads -- one active, one closed."""
    path = tmp_path / "index.db"
    store = Store(path)
    live = store.thread_create(repo_root="E:/repo", title="live", goal="g")
    done = store.thread_create(repo_root="E:/repo", title="done", goal="g")
    store.thread_set_status(done, "closed")
    store.close()
    capsys.readouterr()
    return path, live, done


def _run(capsys, path, *argv):
    capsys.readouterr()
    rc = main(["--db", str(path)] + list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


# --- reopen -----------------------------------------------------------------

def test_reopen_makes_a_closed_thread_active_again(two_threads, capsys):
    path, live, done = two_threads
    rc, out, _ = _run(capsys, path, "thread", "reopen", done)
    assert rc == 0
    assert "reopened" in out

    store = Store(path)
    try:
        assert store.thread_get(done)["status"] == "active"
    finally:
        store.close()


def test_reopen_warns_when_it_creates_ambiguity(two_threads, capsys):
    """The whole point: two active threads for one repo is a choice, not a race."""
    path, live, done = two_threads
    rc, out, _ = _run(capsys, path, "thread", "reopen", done)
    assert rc == 0
    assert "WARNING" in out
    assert live in out, "the other active thread must be named"
    assert "AMBIGUOUS" in out
    assert "refuse" in out


def test_reopen_does_not_warn_when_it_is_the_only_thread(tmp_path, capsys):
    path = tmp_path / "solo.db"
    store = Store(path)
    tid = store.thread_create(repo_root="E:/solo", title="solo", goal="g")
    store.thread_set_status(tid, "closed")
    store.close()
    rc, out, _ = _run(capsys, path, "thread", "reopen", tid)
    assert rc == 0
    assert "WARNING" not in out


def test_reopen_an_unknown_thread_fails_cleanly(tmp_path, capsys):
    path = tmp_path / "x.db"
    Store(path).close()
    rc, out, err = _run(capsys, path, "thread", "reopen", "thr_nope")
    assert rc == 1
    assert "not found" in err


# --- archive ----------------------------------------------------------------

def test_archive_is_a_distinct_terminal_state(two_threads, capsys):
    path, live, done = two_threads
    rc, out, _ = _run(capsys, path, "thread", "archive", live)
    assert rc == 0
    store = Store(path)
    try:
        assert store.thread_get(live)["status"] == "archived"
        assert store.thread_get(live)["status"] != "closed"
    finally:
        store.close()


def test_an_archived_thread_is_not_active(two_threads, capsys):
    path, live, done = two_threads
    _run(capsys, path, "thread", "archive", live)
    store = Store(path)
    try:
        assert live not in {r["id"] for r in store.thread_list("active")}
    finally:
        store.close()


# --- stale ------------------------------------------------------------------

def test_a_fresh_thread_is_not_stale(tmp_path, capsys):
    path = tmp_path / "fresh.db"
    store = Store(path)
    store.thread_create(repo_root="E:/fresh", title="fresh", goal="g")
    store.close()
    rc, out, _ = _run(capsys, path, "thread", "stale")
    assert rc == 0
    assert "no active thread is stale" in out


def test_an_idle_thread_is_reported(tmp_path, capsys):
    path = tmp_path / "idle.db"
    store = Store(path)
    tid = store.thread_create(repo_root="E:/idle", title="idle", goal="g")
    store.con.execute("UPDATE threads SET updated_at=?, created_at=? WHERE id=?",
                      (time.time() - 90 * 86400, time.time() - 90 * 86400, tid))
    store.con.commit()
    store.close()
    rc, out, _ = _run(capsys, path, "thread", "stale")
    assert rc == 0
    assert tid in out
    assert "idle 90d" in out
    assert "close or archive it explicitly" in out


def test_stale_never_declares_a_thread_abandoned(tmp_path, capsys):
    """Stale is an observation, not a decision: the thread stays active."""
    path = tmp_path / "idle2.db"
    store = Store(path)
    tid = store.thread_create(repo_root="E:/idle2", title="idle", goal="g")
    store.con.execute("UPDATE threads SET updated_at=?, created_at=? WHERE id=?",
                      (time.time() - 400 * 86400, time.time() - 400 * 86400, tid))
    store.con.commit()
    store.close()
    _run(capsys, path, "thread", "stale")
    store = Store(path)
    try:
        assert store.thread_get(tid)["status"] == "active", \
            "reporting a thread as stale must not change its state"
    finally:
        store.close()


def test_stale_json_contract(tmp_path, capsys):
    path = tmp_path / "j.db"
    store = Store(path)
    tid = store.thread_create(repo_root="E:/j", title="j", goal="g")
    store.con.execute("UPDATE threads SET updated_at=?, created_at=? WHERE id=?",
                      (time.time() - 60 * 86400, time.time() - 60 * 86400, tid))
    store.con.commit()
    store.close()
    rc, out, _ = _run(capsys, path, "thread", "stale", "--json")
    payload = json.loads(out)
    assert rc == 0
    assert set(payload) == {"days", "stale"}
    assert payload["stale"][0]["id"] == tid
    assert payload["stale"][0]["age_days"] >= 59
