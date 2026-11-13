"""Checkpoint read paths must not create or migrate anything (#42).

Measured on main @ 8f112b8 (and v1.4.0): ``cmd_checkpoint`` opened the
read-write ``Store()`` and ran ``init_checkpoint_schema`` (DDL) for *every*
subcommand, so in a fresh home ``voyager thread checkpoint list`` created
``index.db`` plus the checkpoint table, an index old enough to predate
checkpoints was silently migrated by a read, and a corrupt index produced a
raw traceback like every other pre-#38 surface.

Read paths (list / show / export) must: create nothing, never run
persistent DDL, degrade to the empty answer when the checkpoint table is
absent, and fail cleanly on an unusable index. Mutating subcommands
(create / update / import) keep writing exactly as before. All of these
are pure-pathlib tests: they run on Windows and Linux alike.
"""

from __future__ import annotations

import sqlite3

import pytest

from voyager.cli import main


def _corrupt(db):
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"NOT A SQLITE DATABASE checkpoint-probe")


def test_checkpoint_list_on_a_fresh_home_creates_nothing(tmp_path, capsys):
    db = tmp_path / "index.db"
    rc = main(["--db", str(db), "thread", "checkpoint", "list"])
    assert rc == 0
    assert "no checkpoints found" in capsys.readouterr().out
    assert not db.exists(), "a read must not create the index"
    assert not list(tmp_path.rglob("*-wal")) and not list(tmp_path.rglob("*-shm"))


def test_checkpoint_show_on_a_fresh_home_creates_nothing(tmp_path, capsys):
    db = tmp_path / "index.db"
    rc = main(["--db", str(db), "thread", "checkpoint", "show", "chk_x"])
    assert rc == 1
    assert "checkpoint not found" in capsys.readouterr().err
    assert not db.exists()


def test_failed_checkpoint_export_creates_nothing(tmp_path, capsys):
    """export fails before it touches the output path, and creates no index."""
    db = tmp_path / "index.db"
    out = tmp_path / "out" / "cp.json"
    rc = main(["--db", str(db), "thread", "checkpoint", "export", "chk_x",
               "-o", str(out)])
    assert rc == 1
    assert "failed to export checkpoint" in capsys.readouterr().err
    assert not db.exists()
    assert not out.exists()


def test_corrupt_index_is_a_clean_error_for_checkpoint_reads(tmp_path, capsys):
    """A corrupt index must not look like 'no checkpoints' (#42 criterion 2)."""
    db = tmp_path / "index.db"
    _corrupt(db)
    rc = main(["--db", str(db), "thread", "checkpoint", "list"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err and "unreadable" in captured.err
    assert "Traceback" not in captured.err + captured.out
    assert "no checkpoints found" not in captured.out
    assert db.read_bytes().startswith(b"NOT A SQLITE DATABASE")


def test_an_old_db_is_not_migrated_by_a_read(tmp_path, capsys):
    """A db predating checkpoints gets the empty answer, no DDL, no tables."""
    db = tmp_path / "index.db"
    main(["--db", str(db), "scan", "--platform", "codex"])
    capsys.readouterr()

    def _tables(con):
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    con = sqlite3.connect(str(db))
    try:
        assert "checkpoints" not in _tables(con)   # pre-condition
    finally:
        con.close()

    rc = main(["--db", str(db), "thread", "checkpoint", "list"])
    assert rc == 0
    assert "no checkpoints found" in capsys.readouterr().out

    con = sqlite3.connect(str(db))
    try:
        assert "checkpoints" not in _tables(con), (
            "a read must not create the checkpoint table in a real db")
    finally:
        con.close()


def test_a_read_on_existing_tables_runs_no_persistent_ddl(tmp_path, monkeypatch,
                                                          capsys):
    """With the tables already there, a read goes through no DDL at all."""
    db = tmp_path / "index.db"
    main(["--db", str(db), "scan", "--platform", "codex"])
    capsys.readouterr()
    main(["--db", str(db), "thread", "create", "--repo", str(tmp_path),
          "--title", "t"])
    tid = capsys.readouterr().out.split()[1]
    monkeypatch.chdir(tmp_path)          # create auto-detects the thread from cwd
    rc = main(["--db", str(db), "thread", "checkpoint", "create", "--goal", "g"])
    capsys.readouterr()
    assert rc == 0
    assert "checkpoints" in {r[0] for r in sqlite3.connect(str(db)).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    from voyager import checkpoint as cp

    def refuse(conn):
        raise AssertionError("a checkpoint read must not run DDL")
    monkeypatch.setattr(cp, "add_checkpoint_schema", refuse)

    rc = main(["--db", str(db), "thread", "checkpoint", "list", tid])
    assert rc == 0
    assert "no checkpoints found" not in capsys.readouterr().out


def test_checkpoint_create_still_writes(tmp_path, capsys, monkeypatch):
    db = tmp_path / "index.db"
    main(["--db", str(db), "scan", "--platform", "codex"])
    capsys.readouterr()
    main(["--db", str(db), "thread", "create", "--repo", str(tmp_path),
          "--title", "t"])
    tid = capsys.readouterr().out.split()[1]
    monkeypatch.chdir(tmp_path)          # create auto-detects the thread from cwd
    rc = main(["--db", str(db), "thread", "checkpoint", "create",
               "--goal", "acceptance goal"])
    assert rc == 0
    assert "checkpoint created:" in capsys.readouterr().out
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT goal FROM checkpoints").fetchall()
    finally:
        con.close()
    assert rows and rows[0][0] == "acceptance goal"
