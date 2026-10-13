"""Scan crash-consistency regressions.

Two properties, both about "a scan was interrupted; does the next scan converge
without --force?":

* a **multi-session artifact** (one SQLite file -> many sessions: ZCode, Cursor,
  Codex's app store) must not be left looking "unchanged" after a partial write,
  or the sessions that were never written are never indexed again;
* a **per-file adapter** (one file -> one session) must stay self-healing.

And one performance guard: prune's per-session lookup must use an index.  The
`sources` table's only key is ``(provider, path, sid)``, so a lookup by
``(provider, sid)`` used to scan every source row of that provider once per
session -- measured at 13.2 s for 10,000 sessions, against 0.55 s with the
index (``docs/SCAN_CONSISTENCY.md``).

See ``docs/SCAN_CONSISTENCY.md`` for the full contract.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from voyager.cli import run_scan
from voyager.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def _two_session_zcode_db(path: Path) -> Path:
    """The committed ZCode seed (1 session) plus a second, independent session.

    The seed is left untouched because other tests assert its exact contents;
    the second session is added here so this file owns its own fixture shape.
    """
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


def _interrupt_after(n: int):
    """Make the nth replace_session() call raise, emulating a kill mid-scan.

    Returns (patched, real, state) so the caller can put the real method back
    before the recovery scan without undoing unrelated monkeypatches.
    """
    real = Store.replace_session
    state = {"calls": 0}

    def flaky(self, *a, **kw):
        state["calls"] += 1
        if state["calls"] > n:
            raise KeyboardInterrupt("simulated interruption mid-scan")
        return real(self, *a, **kw)

    return flaky, real, state


def test_partial_write_of_a_multi_session_artifact_is_recovered_by_rescan(
        tmp_path, adapter_of, monkeypatch):
    """One file -> many sessions.  Writing k of n must not hide the rest."""
    db = _two_session_zcode_db(tmp_path / "zcode" / "db.sqlite")
    monkeypatch.setenv("VOYAGER_ZCODE_DB", str(db))
    ad = adapter_of("zcode")
    assert len(ad.discover()) == 1
    assert len(ad.scan(lambda p, f: True)) == 2, "fixture must yield two sessions"

    index = tmp_path / "index.db"
    flaky, real, state = _interrupt_after(1)
    monkeypatch.setattr(Store, "replace_session", flaky)
    store = Store(index)
    with pytest.raises(KeyboardInterrupt):
        run_scan(store, providers=["zcode"], quiet=True)
    store.close()
    monkeypatch.setattr(Store, "replace_session", real)
    assert state["calls"] == 2, "the interruption must land on the second write"

    # A plain re-scan -- exactly what a user runs after a Ctrl-C -- must
    # converge on the complete index.
    store = Store(index)
    run_scan(store, providers=["zcode"], quiet=True)
    n = len(store.sessions("zcode"))
    store.close()
    assert n == 2, (
        "the artifact still looked 'unchanged' after a partial write, so the "
        "session that was never written is permanently missing")


def test_partial_write_of_a_per_file_adapter_is_recovered_by_rescan(
        tmp_path, adapter_of, monkeypatch):
    """One file -> one session.  This path already heals; keep it that way."""
    zstd = pytest.importorskip("zstandard", reason="dsh extra not installed")
    src = (FIXTURES / "dsh" / "sessions" / "--E--proj--demo--"
           / "session-dsh-1" / "session.jsonl")
    root = tmp_path / "dsh" / "sessions"
    base = src.read_text(encoding="utf-8")
    for name in ("session-dsh-1", "session-dsh-2"):
        d = root / "--E--proj--demo--" / name
        d.mkdir(parents=True)
        text = base.replace('"id": "session-dsh-1"', '"id": "%s"' % name)
        (d / "session.jsonl.zstd").write_bytes(
            zstd.ZstdCompressor().compress(text.encode("utf-8")))

    import voyager.adapters.dsh as dsh_mod
    monkeypatch.setattr(dsh_mod, "SESSIONS_DIR", root)
    ad = adapter_of("dsh")
    assert len(ad.discover()) == 2

    index = tmp_path / "index.db"
    flaky, real, _ = _interrupt_after(1)
    monkeypatch.setattr(Store, "replace_session", flaky)
    store = Store(index)
    with pytest.raises(KeyboardInterrupt):
        run_scan(store, providers=["dsh"], quiet=True)
    store.close()
    monkeypatch.setattr(Store, "replace_session", real)

    store = Store(index)
    run_scan(store, providers=["dsh"], quiet=True)
    n = len(store.sessions("dsh"))
    store.close()
    assert n == 2, "the untouched file must read as changed on the next scan"


def test_prune_lookup_is_indexed(tmp_path):
    """prune runs on every scan; its per-session lookup must not table-scan."""
    store = Store(tmp_path / "index.db")
    try:
        plan = " | ".join(
            r[3] for r in store.con.execute(
                "EXPLAIN QUERY PLAN SELECT path FROM sources"
                " WHERE provider=? AND sid=?", ("zcode", "x")))
    finally:
        store.close()
    assert "sid=?" in plan, (
        "the lookup only uses the (provider, ...) prefix of the primary key, "
        "so it scans every source row of the provider once per session: %s"
        % plan)
    assert "SCAN sources" not in plan, plan


def test_prune_marks_nothing_when_every_source_is_present(tmp_path):
    """Guard the prune semantics the index change must not alter."""
    store = Store(tmp_path / "index.db")
    try:
        src = tmp_path / "a.jsonl"
        src.write_text("{}\n", encoding="utf-8")
        store.replace_session(
            {"id": "p:1", "native_session_id": "1", "title": "t"}, [],
            "probe", src)
        assert store.prune_missing_sessions("probe", {str(src)}) == 0
        row = store.con.execute(
            "SELECT source_state FROM sessions WHERE id='p:1'").fetchone()
        assert row["source_state"] == "ACTIVE_SOURCE"
    finally:
        store.close()
