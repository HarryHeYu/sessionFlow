"""Phase 2b single-writer lease tests (D13 / issue #11).

Core contracts:
- additive migration: an index without thread_leases upgrades cleanly
- one live writer per thread: a second acquire is refused and names the
  holder; an EXPIRED lease (stale heartbeat OR dead pid) never blocks
- heartbeat/pid are bound to the lease token: wrong token can't renew or
  release; steal is explicit and lands in the audit log
- `thread unlock` refuses live leases without --steal, clears expired ones
- watch is the heartbeat: live pids get renewed, dead ones left to expire
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from voyager.cli import _watch_sleep, main
from voyager.store import LEASE_HEARTBEAT_TIMEOUT, Store, lease_state


@pytest.fixture
def leased_store(tmp_path):
    """A store with one thread to lease."""
    store = Store(tmp_path / "l.db")
    tid = store.thread_create(repo_root="E:/proj/demo", title="the task")
    yield store, tid
    store.close()


def _backdate_heartbeat(store: Store, tid: str, seconds_ago: float) -> None:
    row = store.thread_lease_get(tid)
    store.con.execute(
        "UPDATE thread_leases SET heartbeat_at=? WHERE thread_id=?",
        (time.time() - seconds_ago, tid))
    store.con.commit()


# ---------------------------------------------------------------------------
# migration safety — same contract as threads
# ---------------------------------------------------------------------------

def test_additive_migration_upgrades_old_index(tmp_path):
    db = tmp_path / "old.db"
    store = Store(db)
    store.thread_create(repo_root="E:/proj/demo")
    store.con.execute("DROP TABLE thread_leases")
    store.con.commit()
    store.close()

    store2 = Store(db)
    tid = store2.thread_list("active")[0]["id"]
    ok, row = store2.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert ok and row["holder"] == "codex"
    store2.close()


# ---------------------------------------------------------------------------
# acquire: one live writer, expiry never blocks
# ---------------------------------------------------------------------------

def test_acquire_blocks_second_holder(tmp_path, leased_store):
    store, tid = leased_store
    ok, mine = store.thread_lease_acquire(tid, "codex",
                                          native_session_id="c1",
                                          pid=os.getpid())
    assert ok and mine["lease_token"]

    # a second store (separate connection, same db — the real switch case)
    store2 = Store(store.db_path)
    ok2, blocker = store2.thread_lease_acquire(tid, "grok")
    assert not ok2
    assert blocker["holder"] == "codex"
    assert blocker["native_session_id"] == "c1"
    store2.close()


def test_acquire_on_missing_thread(leased_store):
    store, _ = leased_store
    assert store.thread_lease_acquire("thr_nope", "codex") == (False, None)


def test_stale_heartbeat_takeover(tmp_path, leased_store):
    store, tid = leased_store
    ok, _ = store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert ok
    _backdate_heartbeat(store, tid, LEASE_HEARTBEAT_TIMEOUT + 5)

    ok2, fresh = store.thread_lease_acquire(tid, "grok", pid=os.getpid())
    assert ok2 and fresh["holder"] == "grok"
    assert fresh["heartbeat_at"] > fresh["acquired_at"] - 1


def test_dead_pid_takeover_but_live_pid_holds(tmp_path, leased_store):
    store, tid = leased_store
    # a process that has already exited
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    ok, _ = store.thread_lease_acquire(tid, "codex", pid=dead.pid)
    assert ok
    assert lease_state(store.thread_lease_get(tid))["expired"]
    assert lease_state(store.thread_lease_get(tid))["why"] == "pid gone"

    ok2, mine = store.thread_lease_acquire(tid, "grok", pid=os.getpid())
    assert ok2
    st = lease_state(store.thread_lease_get(tid))
    assert (st["held"], st["expired"]) == (True, False)


# ---------------------------------------------------------------------------
# token binding: renew / release / steal
# ---------------------------------------------------------------------------

def test_renew_and_release_need_the_token(tmp_path, leased_store):
    store, tid = leased_store
    _, mine = store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    token = mine["lease_token"]

    assert store.thread_lease_renew(tid, "forged-token") is False
    assert store.thread_lease_release(tid, "forged-token") is False
    assert store.thread_lease_get(tid)["holder"] == "codex"

    _backdate_heartbeat(store, tid, 60)
    assert store.thread_lease_renew(tid, token) is True
    assert time.time() - store.thread_lease_get(tid)["heartbeat_at"] < 5

    assert store.thread_lease_release(tid, token) is True
    assert store.thread_lease_get(tid) is None


def test_steal_is_explicit_and_logged(tmp_path, leased_store):
    store, tid = leased_store
    log = store.db_path.parent / "leases.log"
    ok, _ = store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert ok

    ok2, mine = store.thread_lease_acquire(tid, "grok", steal=True)
    assert ok2 and mine["holder"] == "grok"
    assert "steal" in log.read_text(encoding="utf-8")

    # plain grant is logged too — full audit trail
    assert "acquire" in log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# watch as the heartbeat (D13)
# ---------------------------------------------------------------------------

def test_renew_alive_touches_live_pids_only(tmp_path, leased_store):
    store, tid = leased_store
    tid2 = store.thread_create(repo_root="E:/proj/other", title="second")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    ok, _ = store.thread_lease_acquire(tid, "codex", pid=dead.pid)
    ok2, _ = store.thread_lease_acquire(tid2, "grok", pid=os.getpid())
    assert ok and ok2
    _backdate_heartbeat(store, tid, 90)
    _backdate_heartbeat(store, tid2, 90)

    assert store.thread_lease_renew_alive() == 1
    assert time.time() - store.thread_lease_get(tid2)["heartbeat_at"] < 5
    assert time.time() - store.thread_lease_get(tid)["heartbeat_at"] >= 90


def test_watch_sleep_shortens_under_live_leases(leased_store):
    store, tid = leased_store

    class A:
        interval = 300
    assert _watch_sleep(A(), store) == 300

    store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert _watch_sleep(A(), store) == 30   # well under the 120s expiry


# ---------------------------------------------------------------------------
# CLI: thread show lease line + thread unlock
# ---------------------------------------------------------------------------

def test_thread_show_prints_lease_state(tmp_path, leased_store, capsys):
    store, tid = leased_store
    store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    assert main(["--db", str(store.db_path), "thread", "show", "thr"]) == 0
    out = capsys.readouterr().out
    assert "lease: held by codex" in out

    store.thread_lease_release(tid, store.thread_lease_get(tid)["lease_token"])
    assert main(["--db", str(store.db_path), "thread", "show", "thr"]) == 0
    assert "lease: free" in capsys.readouterr().out


def test_unlock_refuses_live_lease_without_steal(tmp_path, leased_store,
                                                 capsys):
    store, tid = leased_store
    store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    rc = main(["--db", str(store.db_path), "thread", "unlock", "thr"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "leased to codex" in out and "--steal" in out
    assert store.thread_lease_get(tid)["holder"] == "codex"

    assert main(["--db", str(store.db_path), "thread", "unlock", "thr",
                 "--steal"]) == 0
    assert store.thread_lease_get(tid) is None
    assert "stole lease" in capsys.readouterr().out
    assert "steal" in (store.db_path.parent / "leases.log").read_text(
        encoding="utf-8")


def test_unlock_clears_expired_lease_and_reports_free(tmp_path,
                                                      leased_store, capsys):
    store, tid = leased_store
    store.thread_lease_acquire(tid, "codex", pid=os.getpid())
    _backdate_heartbeat(store, tid, LEASE_HEARTBEAT_TIMEOUT + 1)

    assert main(["--db", str(store.db_path), "thread", "unlock", "thr"]) == 0
    out = capsys.readouterr().out
    assert "EXPIRED" in out and "heartbeat stale" in out
    assert store.thread_lease_get(tid) is None

    assert main(["--db", str(store.db_path), "thread", "unlock", "thr"]) == 0
    assert "holds no lease" in capsys.readouterr().out
