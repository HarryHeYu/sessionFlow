"""Characterization tests for the scan pipeline, written BEFORE it moves.

The scan subsystem is about to be extracted from ``voyager/cli.py`` into
``voyager/scanner.py``.  The existing D5/D6/D7 tests pin its *edge* behaviour
(interruption, partial writes, source changes); these pin its *ordinary*
contract, which is what a move is most likely to disturb by accident:

* the shape of the stats dict every caller reads
* what a fresh / unchanged / forced scan reports
* provider scoping
* the human-readable output, because the workflow documentation quotes it
* ``voyager.cli.run_scan`` staying importable, because ``auto.py`` and eight
  test modules import it from there

None of these assert anything new; they all pass on the pre-move code.  If one
fails after the move, the move changed behaviour.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from voyager.cli import run_scan
from voyager.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def _zcode_db(tmp_path: Path, n: int) -> Path:
    path = tmp_path / "zcode" / "db.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            (FIXTURES / "zcode" / "seed.sql").read_text(encoding="utf-8"))
        for i in range(2, n + 1):
            sid = "sess_z%d" % i
            ts = 1757000000000 + i * 1000
            con.execute(
                "INSERT INTO session(id, project_id, slug, directory, title,"
                " version, time_created, time_updated, task_type, title_source)"
                " VALUES (?,?,?,?,?,?,?,?,'interactive','first_input')",
                (sid, "p", sid, "E:/proj/d", "t%d" % i, "1", ts, ts + 10))
            con.execute(
                "INSERT INTO message(id, session_id, time_created, time_updated,"
                " data, sequence) VALUES (?,?,?,?,?,1)",
                ("m_%d" % i, sid, ts, ts, '{"role": "user"}'))
            con.execute(
                "INSERT INTO part(id, message_id, session_id, time_created,"
                " time_updated, data, sequence) VALUES (?,?,?,?,?,?,0)",
                ("p_%d" % i, "m_%d" % i, sid, ts, ts,
                 '{"type": "text", "text": "s%d"}' % i))
        con.commit()
    finally:
        con.close()
    return path


@pytest.fixture
def zc(tmp_path, monkeypatch):
    db = _zcode_db(tmp_path, 3)
    monkeypatch.setenv("VOYAGER_ZCODE_DB", str(db))
    return db


def test_run_scan_returns_the_documented_keys(tmp_path, zc):
    store = Store(tmp_path / "index.db")
    res = run_scan(store, providers=["zcode"], quiet=True)
    store.close()
    for key in ("new", "skip", "events_added", "changed_sources", "elapsed",
                "sessions", "events", "write_failures", "auto_attached",
                "pending_stale"):
        assert key in res, "run_scan must keep reporting %r" % key
    assert isinstance(res["elapsed"], float)


def test_first_scan_indexes_everything_then_skips(tmp_path, zc):
    store = Store(tmp_path / "index.db")
    first = run_scan(store, providers=["zcode"], quiet=True)
    assert first["new"] == 3 and first["skip"] == 0
    assert first["sessions"] == 3

    second = run_scan(store, providers=["zcode"], quiet=True)
    store.close()
    assert second["new"] == 0 and second["skip"] == 1, (
        "an unchanged artifact is one source, skipped")


def test_force_reindexes_and_keeps_its_meaning(tmp_path, zc):
    """``--force`` means "re-read everything"; it is not a repair mode."""
    store = Store(tmp_path / "index.db")
    run_scan(store, providers=["zcode"], quiet=True)
    forced = run_scan(store, providers=["zcode"], force=True, quiet=True)
    n = len(store.sessions("zcode"))
    store.close()
    assert forced["new"] == 3, "force must re-ingest, not skip"
    assert n == 3, "force must not duplicate or lose sessions"


def test_provider_scope_is_respected(tmp_path, zc, monkeypatch):
    """Scoping to one provider must not touch another's sessions."""
    import voyager.adapters.dsh as dsh_mod

    # keep the second provider hermetic: an empty tree, never the real ~/.dsh
    empty = tmp_path / "empty-dsh"
    empty.mkdir()
    monkeypatch.setattr(dsh_mod, "SESSIONS_DIR", empty)

    store = Store(tmp_path / "index.db")
    run_scan(store, providers=["zcode"], quiet=True)
    other = run_scan(store, providers=["dsh"], quiet=True)
    n_zcode = len(store.sessions("zcode"))
    store.close()
    assert n_zcode == 3, "scanning dsh must not disturb zcode"
    assert other["new"] == 0


def test_output_shape_is_stable(tmp_path, zc, capsys):
    store = Store(tmp_path / "index.db")
    run_scan(store, providers=["zcode"], quiet=False)
    out = capsys.readouterr().out
    store.close()
    assert "scan complete: 3 sessions" in out
    assert "index:" in out
    assert "zcode: 3 sessions indexed" in out


def test_quiet_scan_prints_nothing(tmp_path, zc, capsys):
    store = Store(tmp_path / "index.db")
    run_scan(store, providers=["zcode"], quiet=True)
    out = capsys.readouterr().out
    store.close()
    assert out == "", "a quiet scan must not write to stdout: %r" % out


def test_run_scan_stays_importable_from_cli():
    """The compatibility entry point auto.py and eight test modules rely on."""
    import voyager.cli as cli_mod
    import voyager.auto as auto_mod

    assert callable(cli_mod.run_scan)
    assert cli_mod.run_scan.__module__ in ("voyager.cli", "voyager.scanner"), (
        "run_scan moved somewhere unexpected: %s" % cli_mod.run_scan.__module__)
    assert auto_mod is not None
