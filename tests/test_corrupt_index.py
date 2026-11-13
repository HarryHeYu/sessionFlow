"""A corrupt/unusable index is reported, not thrown at the user (#34).

Measured on v1.4.0 (both Windows and Ubuntu/WSL, cold-start acceptance of
issue #29, step 11b): every command that merely opens the store -- list,
status, search, scan, thread list -- died with a raw
``sqlite3.DatabaseError: file is not a database`` traceback from
``Store._setup``'s ``PRAGMA journal_mode=WAL``, while ``doctor`` and
``db check`` already rendered the same situation as a clean error.

The fix converts the unusable-file case into a typed exception the CLI
renders as one line of guidance. The corrupt file itself is evidence for
``db check`` / a backup restore and must be left untouched.
"""

from __future__ import annotations

import sqlite3

import pytest

from voyager.cli import main
from voyager.store import IndexUnreadableError, Store, open_read

CORRUPT = b"NOT A SQLITE DATABASE " * 8


def test_open_read_on_a_corrupt_index_raises_the_typed_error(tmp_path):
    db = tmp_path / "index.db"
    db.write_bytes(CORRUPT)
    with pytest.raises(IndexUnreadableError):
        open_read(db)


def test_store_on_a_corrupt_index_names_the_path(tmp_path):
    db = tmp_path / "nested" / "index.db"
    db.parent.mkdir()
    db.write_bytes(CORRUPT)
    with pytest.raises(IndexUnreadableError) as ei:
        Store(db)
    assert str(db) in str(ei.value)
    assert "file is not a database" in str(ei.value)


def test_a_corrupt_index_is_left_untouched(tmp_path):
    """The file is evidence for db check / restore: never rewritten or removed."""
    db = tmp_path / "index.db"
    db.write_bytes(CORRUPT)
    with pytest.raises(IndexUnreadableError):
        open_read(db)
    assert db.read_bytes() == CORRUPT


def test_cli_list_on_a_corrupt_index_is_a_clean_error(tmp_path, capsys):
    db = tmp_path / "index.db"
    db.write_bytes(CORRUPT)
    rc = main(["--db", str(db), "list"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "db check" in captured.err
    assert "Traceback" not in captured.err + captured.out
    assert db.read_bytes() == CORRUPT


def test_cli_scan_on_a_corrupt_index_is_a_clean_error(tmp_path, capsys):
    db = tmp_path / "index.db"
    db.write_bytes(CORRUPT)
    rc = main(["--db", str(db), "scan", "--platform", "codex"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err + captured.out


def test_a_caller_bug_is_not_masked_as_an_unreadable_index(tmp_path, monkeypatch):
    """A ProgrammingError is OUR defect, not a property of the file.

    The conversion must stay scoped to the unusable-file signatures; masking
    a code bug as "index unreadable" would send the user to db check and a
    backup restore over nothing.
    """
    db = tmp_path / "index.db"
    db.write_bytes(b"")                      # a valid empty sqlite file

    def boom(self):
        raise sqlite3.ProgrammingError("unknown placeholder :?9 -- our bug")

    monkeypatch.setattr(Store, "_setup", boom)
    with pytest.raises(sqlite3.ProgrammingError):
        Store(db)
