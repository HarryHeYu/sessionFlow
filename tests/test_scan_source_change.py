"""D7 — the fingerprint must describe the version that was actually read.

The recorded fingerprint used to be taken *after* the source was read (and, for
a multi-session artifact, after the whole artifact had been written).  Any write
landing in between was therefore recorded as though it had been indexed, so the
next scan saw "unchanged" and the new content was never picked up.

Reproduced before the fix (``E:\\sessionflow-scratch\\p14\\probe_d7.py``):

* a per-file source appended between parse and write -> the appended message was
  never indexed by a plain re-scan;
* a multi-session artifact written by another process **during** the read -> the
  late session was never indexed.  This window is the whole ``ad.scan()`` call,
  which for a large artifact is seconds to minutes -- and ``voyager watch``
  scans in the background while the provider agent is running.

The fix records the fingerprint observed *before* the read, so a change during
the read leaves current != recorded and the next scan re-reads it.  It does not
change any transaction semantics: the session, its events and its source row are
still one commit.

Not covered here: a change that preserves both size and mtime (a backup restore).
Nothing short of reading the file can see that, and reading every file on every
scan would defeat the fingerprint entirely -- measured at 122 s against 1.6 s for
the no-op scan.  Documented in ``docs/SCAN_CONSISTENCY.md``.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from voyager.cli import run_scan
from voyager.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_SRC = (FIXTURES / "claude" / "projects" / "E--proj--demo"
              / "cla-1111.jsonl")

APPENDED = (
    '{"type": "user", "uuid": "u9", "parentUuid": "a2",'
    ' "timestamp": "2026-08-02T10:01:00Z", "cwd": "E:/proj/demo",'
    ' "sessionId": "cla-1111", "message": {"role": "user",'
    ' "content": "APPENDED DURING THE SCAN"}}\n')


def _claude_tree(tmp_path: Path) -> Path:
    projects = tmp_path / "projects"
    d = projects / "E--proj--demo"
    d.mkdir(parents=True)
    shutil.copyfile(CLAUDE_SRC, d / "cla-1111.jsonl")
    return projects


def _zcode_db(tmp_path: Path, n: int) -> Path:
    path = tmp_path / "zcode" / "db.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            (FIXTURES / "zcode" / "seed.sql").read_text(encoding="utf-8"))
        for i in range(2, n + 1):
            sid = "sess_z%d" % i
            con.execute(
                "INSERT INTO session(id, project_id, slug, directory, title,"
                " version, time_created, time_updated, task_type, title_source)"
                " VALUES (?,?,?,?,?,?,?,?,'interactive','first_input')",
                (sid, "proj_x", sid, "E:/proj/demo%d" % i, "title %d" % i,
                 "1.2.3", 1757000000000 + i * 1000000,
                 1757000000000 + i * 1000000 + 100000))
            con.execute(
                "INSERT INTO message(id, session_id, time_created, time_updated,"
                " data, sequence) VALUES (?,?,?,?,?,1)",
                ("m_%d" % i, sid, 1757000000000 + i * 1000000,
                 1757000000000 + i * 1000000, '{"role": "user"}'))
            con.execute(
                "INSERT INTO part(id, message_id, session_id, time_created,"
                " time_updated, data, sequence) VALUES (?,?,?,?,?,?,0)",
                ("p_%d" % i, "m_%d" % i, sid, 1757000000000 + i * 1000000,
                 1757000000000 + i * 1000000, '{"type": "text", "text": "x"}'))
        con.commit()
    finally:
        con.close()
    return path


def _event_count(store: Store, sid: str) -> int:
    return store.con.execute(
        "SELECT COUNT(*) FROM events WHERE sid=?", (sid,)).fetchone()[0]


def test_a_source_appended_during_the_read_is_retried(tmp_path, monkeypatch):
    """Per-file: the fingerprint must describe what parse() actually saw."""
    import voyager.adapters.claude as claude_mod

    projects = _claude_tree(tmp_path)
    monkeypatch.setattr(claude_mod, "PROJECTS_DIR", projects)
    src = projects / "E--proj--demo" / "cla-1111.jsonl"

    real_parse = claude_mod.ClaudeAdapter.parse

    def parse_then_append(self, path):
        out = real_parse(self, path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(APPENDED)          # lands after the read, before the write
        return out

    monkeypatch.setattr(claude_mod.ClaudeAdapter, "parse", parse_then_append)
    index = tmp_path / "index.db"
    store = Store(index)
    run_scan(store, providers=["claude"], quiet=True)
    first = _event_count(store, "claude:cla-1111")
    still_changed = store.source_changed("claude", src)
    store.close()
    monkeypatch.setattr(claude_mod.ClaudeAdapter, "parse", real_parse)

    assert still_changed, (
        "the source changed while it was being read, so it must not be "
        "recorded as fresh")

    store = Store(index)
    run_scan(store, providers=["claude"], quiet=True)
    second = _event_count(store, "claude:cla-1111")
    store.close()
    assert second > first, (
        "the appended message must be picked up by a plain re-scan "
        "(before=%d after=%d)" % (first, second))


def test_a_multi_session_artifact_written_during_the_read_is_retried(
        tmp_path, adapter_of, monkeypatch):
    """Shared artifact: a session added while we read it must not be lost."""
    zc = _zcode_db(tmp_path, 3)
    monkeypatch.setenv("VOYAGER_ZCODE_DB", str(zc))
    ad = adapter_of("zcode")
    real_scan = ad.scan

    def scan_then_grow(source_changed):
        bundles = real_scan(source_changed)
        con = sqlite3.connect(zc)       # another process appends a 4th session
        con.execute(
            "INSERT INTO session(id, project_id, slug, directory, title,"
            " version, time_created, time_updated, task_type, title_source)"
            " VALUES ('sess_z9','proj_x','sess_z9','E:/proj/demo9','late','1',"
            " 1757999999000,1757999999000,'interactive','first_input')")
        con.execute(
            "INSERT INTO message(id, session_id, time_created, time_updated,"
            " data, sequence) VALUES ('m_9','sess_z9',1757999999000,"
            " 1757999999000,'{\"role\": \"user\"}',1)")
        con.execute(
            "INSERT INTO part(id, message_id, session_id, time_created,"
            " time_updated, data, sequence) VALUES ('p_9','m_9','sess_z9',"
            " 1757999999000,1757999999000,'{\"type\": \"text\", \"text\": \"late\"}',0)")
        con.commit()
        con.close()
        return bundles

    monkeypatch.setattr(ad, "scan", scan_then_grow)
    index = tmp_path / "index.db"
    store = Store(index)
    run_scan(store, providers=["zcode"], quiet=True)
    n_first = len(store.sessions("zcode"))
    still_changed = store.source_changed("zcode", zc)
    store.close()
    monkeypatch.setattr(ad, "scan", real_scan)

    assert n_first == 3, "the read only saw three sessions"
    assert still_changed, (
        "the artifact changed while it was being read, so it must not be "
        "recorded as complete")

    store = Store(index)
    run_scan(store, providers=["zcode"], quiet=True)
    n_second = len(store.sessions("zcode"))
    store.close()
    assert n_second == 4, (
        "the session added during the read must arrive on the next scan; got %d"
        % n_second)


def test_an_untouched_scan_still_stamps(tmp_path, adapter_of, monkeypatch):
    """Guard: the ordinary path must still record freshness."""
    import voyager.adapters.claude as claude_mod

    projects = _claude_tree(tmp_path)
    monkeypatch.setattr(claude_mod, "PROJECTS_DIR", projects)
    src = projects / "E--proj--demo" / "cla-1111.jsonl"

    index = tmp_path / "index.db"
    store = Store(index)
    run_scan(store, providers=["claude"], quiet=True)
    assert not store.source_changed("claude", src), (
        "a completed scan of an untouched source must record it as fresh")
    res = run_scan(store, providers=["claude"], quiet=True)
    store.close()
    assert res["new"] == 0 and res["skip"] == 1, (
        "the second scan must be a no-op; got new=%d skip=%d"
        % (res["new"], res["skip"]))
