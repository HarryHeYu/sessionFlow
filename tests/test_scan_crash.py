"""Scan crash tests that use a real, separately-killed child process.

``test_scan_consistency.py`` simulates an interruption with an exception, which
proves the *logic*.  This file proves the *storage* behaviour: a child process
is terminated with ``os._exit`` so nothing unwinds, no ``finally`` runs, no
Python buffer is flushed and SQLite gets no chance to clean up.  That is the
closest thing to a kill inside a test, short of SIGKILL racing a sleep.

Never pointed at a real index: every child works on ``tmp_path`` only.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from voyager.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"

#: Runs a scan and dies abruptly at an injected point.  ``os._exit`` is a real
#: process termination: no exception is raised, no rollback is issued.
CHILD = r'''
import os, sqlite3, sys
from pathlib import Path

import voyager.store as store_mod
from voyager.cli import run_scan

mode, db, index = sys.argv[1], sys.argv[2], sys.argv[3]

if mode == "after_first_session":
    # Die just after the first session is written, exactly as a Ctrl-C or a
    # power event would leave it: the first session is committed, the rest of
    # the artifact never happens.
    real = store_mod.Store.replace_session
    state = {"n": 0}

    def killer(self, *a, **kw):
        state["n"] += 1
        if state["n"] > 1:
            os._exit(137)
        return real(self, *a, **kw)

    store_mod.Store.replace_session = killer
elif mode == "inside_transaction":
    # Die inside the per-session transaction, after BEGIN and after the
    # session row is inserted but before COMMIT.
    def killer(*a, **kw):
        os._exit(137)

    store_mod._truncate = killer
else:
    raise SystemExit("unknown mode %r" % mode)

store = store_mod.Store(index)
run_scan(store, providers=["zcode"], quiet=True)
print("CHILD_COMPLETED")          # must never be reached
'''


def _two_session_zcode_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            (FIXTURES / "zcode" / "seed.sql").read_text(encoding="utf-8"))
        con.execute(
            "INSERT INTO session(id, project_id, slug, directory, title, version,"
            " time_created, time_updated, task_type, title_source)"
            " VALUES ('sess_z2','proj_x','sess_z2','E:/proj/demo2',"
            " 'zcode title 2','1.2.3',1757001000000,1757001100000,"
            " 'interactive','first_input')")
        con.execute(
            "INSERT INTO message(id, session_id, time_created, time_updated, data,"
            " sequence) VALUES ('m3','sess_z2',1757001000000,1757001000000,"
            " '{\"role\": \"user\", \"time\": {\"created\": 1757001000000},"
            " \"path\": {\"cwd\": \"E:/proj/demo2\", \"root\": \"E:/proj/demo2\"}}', 1)")
        con.execute(
            "INSERT INTO part(id, message_id, session_id, time_created,"
            " time_updated, data, sequence) VALUES ('p9','m3','sess_z2',"
            " 1757001000000,1757001000000,"
            " '{\"type\": \"text\", \"text\": \"second session\"}', 0)")
        con.commit()
    finally:
        con.close()
    return path


def _run_child(tmp_path: Path, mode: str) -> tuple:
    """Run the crashing child; return (completed_process, db, index).

    The child gets its own environment; the caller must point the *parent*
    process at the same artifact before re-scanning, or discovery finds
    nothing and the test proves nothing.
    """
    db = _two_session_zcode_db(tmp_path / "zcode" / "db.sqlite")
    index = tmp_path / "index.db"
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)          # override the sandbox shim
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["VOYAGER_NO_SYNC"] = "1"
    env["VOYAGER_ZCODE_DB"] = str(db)
    env["HOME"] = str(tmp_path / "home")
    env["USERPROFILE"] = str(tmp_path / "home")
    env["APPDATA"] = str(tmp_path / "home" / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(tmp_path / "home" / "AppData" / "Local")

    proc = subprocess.run([sys.executable, str(script), mode, str(db),
                           str(index)],
                          env=env, cwd=str(REPO_ROOT), timeout=180,
                          capture_output=True, text=True)
    return proc, db, index


def test_a_kill_mid_artifact_write_is_recovered_by_a_plain_rescan(
        tmp_path, monkeypatch):
    """D5: the sessions that were never written must not be lost forever."""
    proc, db, index = _run_child(tmp_path, "after_first_session")
    assert proc.returncode == 137, (
        "the child must die abruptly; got rc=%s\nstdout=%s\nstderr=%s"
        % (proc.returncode, proc.stdout, proc.stderr))
    assert "CHILD_COMPLETED" not in proc.stdout

    store = Store(index)
    before = len(store.sessions("zcode"))
    store.close()
    assert before == 1, (
        "the first session should have committed; got %d" % before)

    # what a user actually does after an interrupted scan
    monkeypatch.setenv("VOYAGER_ZCODE_DB", str(db))
    from voyager.cli import run_scan
    store = Store(index)
    run_scan(store, providers=["zcode"], quiet=True)
    after = len(store.sessions("zcode"))
    store.close()
    assert after == 2, (
        "a plain re-scan must converge after a killed scan; got %d of 2" % after)


def test_a_kill_inside_a_session_transaction_leaves_no_partial_session(
        tmp_path, monkeypatch):
    """I1: a session, its events and its source row are one commit or none."""
    proc, db, index = _run_child(tmp_path, "inside_transaction")
    assert proc.returncode == 137, (
        "the child must die abruptly; got rc=%s\nstderr=%s"
        % (proc.returncode, proc.stderr))
    assert "CHILD_COMPLETED" not in proc.stdout

    store = Store(index)
    try:
        n_sessions = store.con.execute(
            "SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_events = store.con.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0]
        n_sources = store.con.execute(
            "SELECT COUNT(*) FROM sources").fetchone()[0]
    finally:
        store.close()
    assert (n_sessions, n_events, n_sources) == (0, 0, 0), (
        "the killed transaction must be rolled back whole, not half applied: "
        "sessions=%d events=%d sources=%d" % (n_sessions, n_events, n_sources))

    monkeypatch.setenv("VOYAGER_ZCODE_DB", str(db))
    from voyager.cli import run_scan
    store = Store(index)
    run_scan(store, providers=["zcode"], quiet=True)
    after = len(store.sessions("zcode"))
    store.close()
    assert after == 2, "a plain re-scan must still converge; got %d of 2" % after
