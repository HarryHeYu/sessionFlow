"""Schema migration for the verification evidence table.

Four database states have to keep working, and none of them may be rewritten:

  1. a fresh database               -- the table appears, nothing else does
  2. a legacy database without it   -- reads report "no evidence", no writes
  3. an existing populated database -- every other table is untouched
  4. repeated open / migration      -- idempotent, same shape every time

The evidence schema is frozen while these tests are written: the point is to
prove the *migration* is safe, and changing the schema at the same time would let
one mask the other.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from voyager import verification_harness as vh
from voyager.store import Store
from voyager.verification_harness import HOOK_TRIGGERED


def _table_names(con) -> set:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _index_names(con) -> set:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")}


def _row_counts(con) -> dict:
    out = {}
    for t in sorted(_table_names(con)):
        if t == vh.TABLE:
            continue
        try:
            out[t] = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except Exception:
            out[t] = None
    return out


def _schema_digest(con) -> str:
    """Everything except the evidence table *and its own indexes*.

    Adding the evidence table legitimately adds those; what must never change is
    everything that was already there.
    """
    rows = list(con.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE '%verification_events%' "
        "ORDER BY name"))
    return hashlib.sha256(repr(rows).encode()).hexdigest()


# --- 1. fresh database ------------------------------------------------------

def test_fresh_database_gets_only_the_evidence_table(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    path.touch()                       # exists, but completely empty
    monkeypatch.setattr(vh, "_db_path", lambda explicit=None: path)

    con = sqlite3.connect(str(path))
    before = _table_names(con)
    con.close()
    assert before == set()

    assert vh.record_event("codex", HOOK_TRIGGERED, "c1", observed_at=1.0)
    con = sqlite3.connect(str(path))
    after = _table_names(con)
    con.close()
    assert after == {vh.TABLE}, "the harness must create its own table and no other"


# --- 2. legacy database without the table -----------------------------------

def test_legacy_database_without_the_table_reads_as_no_evidence(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    store = Store(path)                # the real schema, no evidence table
    store.close()
    monkeypatch.setattr(vh, "_db_path", lambda explicit=None: path)

    con = sqlite3.connect(str(path))
    assert vh.TABLE not in _table_names(con)
    before = _schema_digest(con)
    con.close()

    out = vh.query_status("codex")["providers"]["codex"]
    assert out["observed_state"] is None
    assert out["evidence_count"] == 0

    con = sqlite3.connect(str(path))
    assert vh.TABLE not in _table_names(con), "a read must not create the table"
    assert _schema_digest(con) == before, "a read must not touch the schema"
    con.close()


def test_a_legacy_mutable_table_is_ignored(tmp_path, monkeypatch):
    """The old `verification_records` shape is not evidence and is not migrated."""
    path = tmp_path / "legacy2.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE verification_records (provider TEXT PRIMARY KEY, "
                "promoted_to TEXT, evidence TEXT)")
    con.execute("INSERT INTO verification_records VALUES ('codex', "
                "'ZERO_TOUCH_LIVE_VERIFIED', '[{\"probe\": 1}]')")
    con.commit()
    con.close()
    monkeypatch.setattr(vh, "_db_path", lambda explicit=None: path)

    out = vh.query_status("codex")["providers"]["codex"]
    assert out["observed_state"] is None, "legacy rows must not become live evidence"

    con = sqlite3.connect(str(path))
    # left exactly as it was: deprecated, not deleted, not rewritten
    assert "verification_records" in _table_names(con)
    assert con.execute("SELECT COUNT(*) FROM verification_records").fetchone()[0] == 1
    con.close()


# --- 3. existing populated database -----------------------------------------

def _populate(path):
    store = Store(path)
    tid = store.thread_create(repo_root="E:/repo", title="t", goal="g")
    from voyager.model import new_event, new_session

    sess = new_session(id="codex:s1", provider="codex", native_session_id="s1",
                       title="seeded", started_at=1.0, updated_at=2.0,
                       repo_root="E:/repo", cwd="E:/repo")
    src = path.parent / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store.replace_session(
        sess,
        [new_event(sid="codex:s1", seq=1, kind="user", ts=1.0, content="hello")],
        "codex", src)
    store.thread_attach(tid, "codex:s1")
    store.close()
    return tid


def test_a_populated_database_is_left_alone(tmp_path, monkeypatch):
    path = tmp_path / "populated.db"
    _populate(path)
    monkeypatch.setattr(vh, "_db_path", lambda explicit=None: path)

    con = sqlite3.connect(str(path))
    counts_before = _row_counts(con)
    schema_before = _schema_digest(con)
    con.close()

    vh.record_event("codex", HOOK_TRIGGERED, "c1", observed_at=1.0)
    vh.query_status()

    con = sqlite3.connect(str(path))
    assert _row_counts(con) == counts_before, "existing rows must not change"
    assert _schema_digest(con) == schema_before, "existing schema must not change"
    assert vh.TABLE in _table_names(con)
    con.close()


def test_recording_does_not_disturb_the_indexes_of_other_tables(tmp_path, monkeypatch):
    path = tmp_path / "populated2.db"
    _populate(path)
    monkeypatch.setattr(vh, "_db_path", lambda explicit=None: path)
    con = sqlite3.connect(str(path))
    before = _index_names(con)
    con.close()

    vh.record_event("codex", HOOK_TRIGGERED, "c1", observed_at=1.0)

    con = sqlite3.connect(str(path))
    added = _index_names(con) - before
    con.close()
    assert added == {"idx_verification_events_chain",
                     "idx_verification_events_session",
                     "idx_verification_events_time"}, added


# --- 4. repeated open / migration -------------------------------------------

def test_repeated_open_and_migration_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "repeat.db"
    path.touch()
    monkeypatch.setattr(vh, "_db_path", lambda explicit=None: path)

    vh.record_event("codex", HOOK_TRIGGERED, "c1", observed_at=1.0)
    con = sqlite3.connect(str(path))
    shape = (_table_names(con), _index_names(con))
    rows = con.execute("SELECT COUNT(*) FROM %s" % vh.TABLE).fetchone()[0]
    con.close()

    for _ in range(5):
        con = sqlite3.connect(str(path))
        vh.ensure_schema(con)
        con.close()
        vh.query_status()

    con = sqlite3.connect(str(path))
    assert (_table_names(con), _index_names(con)) == shape
    assert con.execute("SELECT COUNT(*) FROM %s" % vh.TABLE).fetchone()[0] == rows
    con.close()


def test_the_primary_key_deduplicates_across_reopens(tmp_path, monkeypatch):
    path = tmp_path / "pk.db"
    path.touch()
    monkeypatch.setattr(vh, "_db_path", lambda explicit=None: path)

    first = vh.record_event("codex", HOOK_TRIGGERED, "c1", observed_at=1.0)
    second = vh.record_event("codex", HOOK_TRIGGERED, "c1", observed_at=1.0)
    assert first and second is None, "replay across connections must still dedupe"

    con = sqlite3.connect(str(path))
    assert con.execute("SELECT COUNT(*) FROM %s" % vh.TABLE).fetchone()[0] == 1
    pk = [r for r in con.execute("PRAGMA table_info(%s)" % vh.TABLE) if r[5]]
    con.close()
    assert [r[1] for r in pk] == ["event_id"], "event_id is the only key"


def test_the_schema_declares_the_columns_the_derivation_needs(tmp_path):
    """A migration that quietly drops a column would break derivation silently."""
    path = tmp_path / "cols.db"
    con = sqlite3.connect(str(path))
    vh.ensure_schema(con)
    cols = {r[1]: r[2] for r in con.execute("PRAGMA table_info(%s)" % vh.TABLE)}
    con.close()
    assert set(cols) == {"event_id", "provider", "event_type", "correlation_id",
                         "native_session_id", "thread_id", "source_session_id",
                         "observed_at", "payload_json"}
    assert cols["event_id"] == "TEXT"
    assert cols["observed_at"] == "REAL"
    for nullable in ("native_session_id", "thread_id", "source_session_id"):
        assert cols[nullable] == "TEXT", nullable
