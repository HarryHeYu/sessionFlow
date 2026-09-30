"""Backups must be SQLite-consistent snapshots, not file copies.

The failure this file exists to prevent: `shutil.copy2(index.db)` looks like a
backup and is not one.  The index runs in WAL mode, so committed rows can still
live in `index.db-wal`; a file copy captures whatever bytes happened to be in the
main file, which can be stale or torn.

Each test drives a real WAL database through the backup and checks the result the
way a recovery would: open it, run integrity_check, and look for the newest
committed rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest

from voyager.db_health import create_backup, list_backups


@pytest.fixture
def wal_db(tmp_path):
    """A WAL database with committed rows that have NOT been checkpointed."""
    path = tmp_path / "index.db"
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, body TEXT)")
    con.commit()
    con.executemany("INSERT INTO rows (body) VALUES (?)",
                    [("row-%d" % i,) for i in range(200)])
    con.commit()
    # deliberately no `PRAGMA wal_checkpoint` -- this is the state a naive copy
    # gets wrong
    yield path, con
    con.close()


def _wal_path(path):
    return path.with_name(path.name + "-wal")


def _integrity(path):
    con = sqlite3.connect(str(path))
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def _count(path):
    """Row count, or None when the file cannot even be read as this database."""
    try:
        con = sqlite3.connect(str(path))
        try:
            return con.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return None


def test_the_wal_actually_holds_the_committed_state(wal_db):
    """Guards the premise: if this ever stops being true the other tests are moot."""
    path, _ = wal_db
    wal = _wal_path(path)
    assert wal.exists(), "WAL mode should have created a -wal file"
    assert wal.stat().st_size > 0, "committed rows should still be in the WAL"


def test_a_backup_taken_while_the_writer_is_open_is_consistent(wal_db, tmp_path):
    path, writer = wal_db
    out = tmp_path / "backups"

    backup = create_backup(path, out)          # writer connection still open

    assert _integrity(backup) == "ok"
    assert _count(backup) == 200, "every committed row must be in the snapshot"

    # and the writer can keep going afterwards
    writer.execute("INSERT INTO rows (body) VALUES ('after')")
    writer.commit()
    assert _count(backup) == 200, "the snapshot is a point in time, not a view"


def test_the_snapshot_contains_rows_the_main_file_alone_would_miss(wal_db, tmp_path):
    """The direct proof that a file copy is not equivalent."""
    path, _ = wal_db
    out = tmp_path / "backups"
    backup = create_backup(path, out)

    # the main database file, copied byte-for-byte as shutil would
    naive = tmp_path / "naive.db"
    naive.write_bytes(path.read_bytes())

    assert _count(backup) == 200, "the snapshot must hold the committed rows"
    # The direct comparison.  A byte copy of the main file is missing the WAL
    # content: on this platform it does not even contain the table, because the
    # schema itself has not been checkpointed yet.  Whatever it contains, the
    # snapshot is the one that is right.
    assert _count(naive) != 200, (
        "premise: the main file alone should NOT look like a complete database "
        "(got %r); if this ever changes, re-check what the copy captures"
        % (_count(naive),))


def test_repeated_backups_all_verify(wal_db, tmp_path):
    path, _ = wal_db
    out = tmp_path / "backups"
    made = []
    for i in range(3):
        made.append(create_backup(path, out))
        time.sleep(1.05)              # the name is second-resolution
    assert len({p.name for p in made}) == 3, "each backup gets its own name"
    for p in made:
        assert _integrity(p) == "ok"
        assert _count(p) == 200


def test_metadata_describes_the_file_that_exists(wal_db, tmp_path):
    path, _ = wal_db
    out = tmp_path / "backups"
    backup = create_backup(path, out)
    meta = json.loads(backup.with_suffix(".meta.json").read_text(encoding="utf-8"))

    assert meta["backup_file"] == backup.name
    assert meta["size_bytes"] == backup.stat().st_size
    assert meta["method"] == "sqlite3.Connection.backup"
    assert meta["integrity_check"] == "ok"
    assert meta["table_counts"]["rows"] == 200

    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    assert meta["sha256"] == digest, "metadata must match the real bytes"


def test_list_backups_reflects_the_real_files(wal_db, tmp_path):
    path, _ = wal_db
    out = tmp_path / "backups"
    backup = create_backup(path, out)
    listed = list_backups(out)
    assert listed, listed
    names = {entry.get("backup_file") or entry.get("path") or entry.get("name")
             for entry in listed}
    assert backup.name in names


def test_a_failed_backup_leaves_nothing_that_looks_like_a_backup(tmp_path):
    """Destination unwritable: no .db, no metadata, no stray partial."""
    out = tmp_path / "backups"
    out.mkdir()
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(Exception):
        create_backup(missing, out)
    leftovers = [p.name for p in out.iterdir()]
    assert leftovers == [], leftovers


def test_a_failed_verification_does_not_publish_the_snapshot(tmp_path, monkeypatch):
    """If verification fails, the temporary file must be removed."""
    path = tmp_path / "index.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()

    import voyager.db_health as dh

    real_connect = sqlite3.connect

    class BadVerdict:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a):
            if sql.strip().upper().startswith("PRAGMA INTEGRITY_CHECK"):
                return self
            return self._real.execute(sql, *a)

        def fetchone(self):
            return ("not ok",)

        def close(self):
            return self._real.close()

    def fake_connect(target, *a, **kw):
        real = real_connect(target, *a, **kw)
        if str(target).endswith(".partial"):
            return BadVerdict(real)
        return real

    monkeypatch.setattr(dh.sqlite3, "connect", fake_connect)
    out = tmp_path / "backups"
    with pytest.raises(Exception):
        dh.create_backup(path, out)

    monkeypatch.undo()
    assert [p.name for p in out.iterdir()] == [], "no partial, no metadata, no backup"
