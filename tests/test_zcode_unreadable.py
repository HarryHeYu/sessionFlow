"""ZCode — a database that is present but unreadable is not a vanished source.

`discover_zcode_db` returns only the databases it can read, which is correct:
the scanner wants usable sources.  The bug was what the *caller* concluded from
an empty list.  `run_scan` read "discovery found nothing" as "nothing is on
disk any more", called `prune_missing_sessions(provider, set())`, and marked
every session that provider owned `SOURCE_MISSING` — retiring a history that was
still sitting on disk, merely corrupt, locked or permission-denied for a round.

The fix separates the two questions:

* `probe_zcode_db(path)` classifies one candidate — absent / ok / unreadable /
  not-zcode;
* `unusable_zcode_dbs()` names the candidates that exist but cannot be used;
* the scan subtracts those from the set of paths it treats as gone.

Nothing here changes the persisted vocabulary: the store still speaks
`ACTIVE_SOURCE` / `SOURCE_MISSING` / `ARCHIVED_CANONICAL`.  A probe verdict only
decides whether *this round* may release anything.

Every fixture is a real SQLite file written to a temp tree.  The one case that
cannot be produced portably — a POSIX permission denial — verifies itself before
asserting, and skips with a reason if the file turns out to be readable anyway.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from voyager.adapters import zcode
from voyager.cli import run_scan
from voyager.store import Store

_ZCODE_DDL = (
    "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, parent_id TEXT,"
    " project_id INTEGER, time_created INTEGER, time_updated INTEGER,"
    " summary_additions BLOB, summary_deletions BLOB, summary_files BLOB)",
    "CREATE TABLE message (id INTEGER, session_id INTEGER, sequence INTEGER,"
    " time_created INTEGER, data TEXT)",
    "CREATE TABLE part (message_id INTEGER, sequence INTEGER,"
    " time_created INTEGER, data BLOB)",
    "CREATE TABLE tool_usage (tool_call_id TEXT, exit_code INTEGER,"
    " error_message TEXT, stdout_bytes BLOB, stderr_bytes BLOB, session_id TEXT)",
    "CREATE TABLE model_usage (model_id TEXT, provider_id TEXT,"
    " input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,"
    " cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,"
    " session_id TEXT)",
)


def _make_db(path: Path, sid: str, title: str) -> Path:
    """A real, valid ZCode database.

    Truncates first, so it can also repair a path that currently holds a
    corrupt file — which is what the recovery test does.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    con = sqlite3.connect(str(path))
    for ddl in _ZCODE_DDL:
        con.execute(ddl)
    con.execute(
        "INSERT INTO session VALUES (?, '/proj/demo', ?, NULL, NULL, 1000, 2000,"
        " NULL, NULL, NULL)", (sid, title))
    con.execute("INSERT INTO message VALUES (1, ?, 1, 1500, ?)",
                (sid, json.dumps({"role": "user"})))
    con.execute("INSERT INTO part VALUES (1, 1, 1500, ?)",
                (json.dumps({"type": "text", "text": "hello from zcode"}),))
    con.commit()
    con.close()
    return path


def _corrupt(path: Path) -> None:
    """Break the SQLite header so the file cannot be read as a database.

    The file stays on disk and stays the same size — exactly the situation the
    old code mistook for "the source is gone".
    """
    raw = bytearray(path.read_bytes())
    raw[0:16] = b"NOT A DATABASE!!"
    path.write_bytes(bytes(raw))


def _wrong_schema(path: Path) -> Path:
    """Readable SQLite, but not a ZCode store."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE session (id TEXT, wrong_col TEXT)")
    con.commit()
    con.close()
    return path


@pytest.fixture
def zcode_home(monkeypatch, tmp_path):
    """Point the adapter's HOME at a temp tree and disable the env override."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("VOYAGER_ZCODE_DB", raising=False)
    return tmp_path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "zcode-unreadable.db")
    yield s
    s.close()


def _db_path(home: Path) -> Path:
    return home / ".zcode" / "cli" / "db" / "db.sqlite"


def _state(store: Store, sid: str) -> str | None:
    rows = store.q("SELECT source_state FROM sessions WHERE id=?", (sid,))
    return rows[0][0] if rows else None


# --- 1. the probe itself ----------------------------------------------------

def test_probe_classifies_every_case(zcode_home, tmp_path):
    """absent / ok / unreadable / not-zcode are four distinct answers."""
    missing = tmp_path / "never-existed.sqlite"
    assert zcode.probe_zcode_db(missing) == zcode.PROBE_ABSENT

    good = _make_db(tmp_path / "good.sqlite", "s1", "Good")
    assert zcode.probe_zcode_db(good) == zcode.PROBE_OK

    corrupt = _make_db(tmp_path / "corrupt.sqlite", "s2", "Corrupt")
    _corrupt(corrupt)
    assert zcode.probe_zcode_db(corrupt) == zcode.PROBE_UNREADABLE

    wrong = _wrong_schema(tmp_path / "wrong.sqlite")
    assert zcode.probe_zcode_db(wrong) == zcode.PROBE_NOT_ZCODE


def test_probe_reports_a_locked_database_as_unreadable(zcode_home, tmp_path):
    """A live EXCLUSIVE lock must read as unreadable, not as absent.

    A real lock, held by a real second connection — the adapter's own
    read-only open is what has to fail.
    """
    db = _make_db(tmp_path / "locked.sqlite", "s3", "Locked")
    holder = sqlite3.connect(str(db), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        verdict = zcode.probe_zcode_db(db)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert verdict == zcode.PROBE_UNREADABLE, \
        "a database held under an EXCLUSIVE lock must read as unreadable"
    # The lock is gone now, so the same path must be usable again.
    assert zcode.probe_zcode_db(db) == zcode.PROBE_OK


@pytest.mark.skipif(os.name == "nt", reason="chmod does not deny reads on Windows")
def test_probe_reports_a_permission_denied_file_as_unreadable(zcode_home, tmp_path):
    db = _make_db(tmp_path / "denied.sqlite", "s4", "Denied")
    os.chmod(db, 0o000)
    try:
        # Verify the premise rather than assuming it: a suite running as root
        # can still read the file, and then this test would prove nothing.
        try:
            with open(db, "rb"):
                pass
        except OSError:
            pass
        else:
            pytest.skip("the file is still readable (running as root?)")
        assert zcode.probe_zcode_db(db) == zcode.PROBE_UNREADABLE
    finally:
        os.chmod(db, 0o600)


def test_probe_never_raises_on_a_directory_or_a_junk_file(zcode_home, tmp_path):
    """A probe is called from the scan loop; it must not be able to break it."""
    a_dir = tmp_path / "not-a-file.sqlite"
    a_dir.mkdir()
    assert zcode.probe_zcode_db(a_dir) == zcode.PROBE_ABSENT

    junk = tmp_path / "junk.sqlite"
    junk.write_text("this is not a database at all", encoding="utf-8")
    assert zcode.probe_zcode_db(junk) == zcode.PROBE_UNREADABLE


# --- 2. discovery keeps its contract ---------------------------------------

def test_discovery_still_excludes_unusable_databases(zcode_home):
    """`discover_zcode_db` returns usable sources only — unchanged contract."""
    db = _db_path(zcode_home)
    _make_db(db, "s5", "Will be corrupted")
    assert zcode.discover_zcode_db() == [db]

    _corrupt(db)
    assert zcode.discover_zcode_db() == [], "an unreadable db is not a source"
    # ...but it is not forgotten either: this is the set the scan subtracts.
    assert zcode.unusable_zcode_dbs() == [db]


def test_unusable_is_empty_when_the_database_is_fine(zcode_home):
    db = _db_path(zcode_home)
    _make_db(db, "s6", "Fine")
    assert zcode.unusable_zcode_dbs() == []


def test_absent_is_not_reported_as_unusable(zcode_home):
    """The distinction the whole fix rests on: nothing on disk is not
    "present but unreadable"."""
    assert zcode.discover_zcode_db() == []
    assert zcode.unusable_zcode_dbs() == []


# --- 3. the regression: a scan must not retire history it cannot read -------

def test_a_corrupt_database_does_not_retire_its_sessions(zcode_home, store):
    """The bug, end to end through the real scan pipeline."""
    db = _db_path(zcode_home)
    _make_db(db, "sess_corrupt", "ZCode work")

    run_scan(store, providers=["zcode"], force=True, quiet=True)
    assert _state(store, "zcode:sess_corrupt") in (None, "ACTIVE_SOURCE"), \
        "precondition: the session is indexed and live"
    assert store.search("hello from zcode"), "precondition: searchable"

    _corrupt(db)
    run_scan(store, providers=["zcode"], force=True, quiet=True)

    assert _state(store, "zcode:sess_corrupt") != "SOURCE_MISSING", \
        "a database that is present but unreadable must not release its sessions"
    assert store.q("SELECT COUNT(*) n FROM events")[0]["n"] >= 1
    assert store.search("hello from zcode"), \
        "the retained history must stay searchable"


def test_recovery_reindexes_once_the_database_is_readable_again(zcode_home, store):
    """The failure is transient: fix the file and the round works normally."""
    db = _db_path(zcode_home)
    _make_db(db, "sess_recover", "ZCode work")
    run_scan(store, providers=["zcode"], force=True, quiet=True)

    _corrupt(db)
    run_scan(store, providers=["zcode"], force=True, quiet=True)
    assert _state(store, "zcode:sess_recover") != "SOURCE_MISSING"

    _make_db(db, "sess_recover", "ZCode work, repaired")
    run_scan(store, providers=["zcode"], force=True, quiet=True)

    assert _state(store, "zcode:sess_recover") in (None, "ACTIVE_SOURCE")
    assert store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 1, \
        "recovery must reconcile to one session, not duplicate it"


def test_a_deleted_database_still_retires_its_sessions(zcode_home, store):
    """The negative control: the fix must not have disabled pruning.

    A database that is genuinely gone keeps the O2 behaviour — history is
    retained and marked, never deleted.
    """
    db = _db_path(zcode_home)
    _make_db(db, "sess_gone", "ZCode work")
    run_scan(store, providers=["zcode"], force=True, quiet=True)

    db.unlink()
    run_scan(store, providers=["zcode"], force=True, quiet=True)

    assert _state(store, "zcode:sess_gone") == "SOURCE_MISSING"
    assert store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 1, \
        "retention still means retained, not deleted"
