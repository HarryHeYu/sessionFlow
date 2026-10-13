"""Opening a current index must not need the write lock.

``Store.__init__`` ran two unconditional writes on *every* open:

* ``INSERT OR IGNORE INTO meta(key, value) VALUES('schema', '1')``
* the O2 backfill ``UPDATE sessions SET source_state='ACTIVE_SOURCE' WHERE ...``

Both are no-ops once the database is current, but both still take the write
lock.  Measured with another connection holding ``BEGIN IMMEDIATE``
(``E:\\sessionflow-scratch\\p14\\probe_lock.py``): ``executescript(SCHEMA)`` and
the ``PRAGMA``s are fine, those two are not.

The consequence is user-visible: every entry point constructs ``Store()`` --
``scan``, ``continue``, ``status``, the MCP tools, the DSH bridge -- so with
``voyager watch`` scanning in the background a foreground command could die at
startup with an unhandled ``database is locked`` after the 5 s busy timeout.

The fix guards both writes with a read, so a current index opens without ever
asking for the write lock, while a legacy database still migrates.
"""

from __future__ import annotations

import sqlite3

import pytest

from voyager.store import Store


def _hold_write_lock(path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=1)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("BEGIN IMMEDIATE")
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('holder', '1')")
    return con


def test_opening_a_current_index_does_not_need_the_write_lock(tmp_path):
    path = tmp_path / "index.db"
    Store(path).close()                    # a current database

    holder = _hold_write_lock(path)
    try:
        # a second connection holds the write lock; opening must still work
        store = Store(path)
        store.close()
    finally:
        holder.execute("COMMIT")
        holder.close()


def test_a_fresh_database_still_gets_its_schema_marker(tmp_path):
    """Guard: the meta row is still written when it is actually missing."""
    path = tmp_path / "fresh.db"
    store = Store(path)
    try:
        row = store.con.execute(
            "SELECT value FROM meta WHERE key='schema'").fetchone()
        assert row is not None and row[0] == "1"
    finally:
        store.close()


def test_a_legacy_row_is_still_backfilled(tmp_path):
    """Guard: the read that replaces the blind UPDATE must still migrate."""
    path = tmp_path / "legacy.db"
    store = Store(path)
    store.con.execute(
        "INSERT OR REPLACE INTO sessions(id, provider, native_id, title,"
        " source_state) VALUES ('p:legacy','p','legacy','t','LIVE')")
    store.con.commit()
    store.close()

    store = Store(path)                    # reopen: the migration must run
    try:
        row = store.con.execute(
            "SELECT source_state FROM sessions WHERE id='p:legacy'").fetchone()
        assert row["source_state"] == "ACTIVE_SOURCE", (
            "the pre-O2 spelling must still be normalised on open; got %r"
            % row["source_state"])
    finally:
        store.close()
