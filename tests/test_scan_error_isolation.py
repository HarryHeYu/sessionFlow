"""D6 — one failing write must not take the rest of the scan with it.

Measured behaviour before the fix (``E:\\sessionflow-scratch\\p14\\probe_d6.py``):

* per-file, write fails on source 2 of 3 -> the scan **aborts**; source 3 is
  never processed.  A *parse* failure on the same source is already isolated and
  the scan completes -- so the defect is the write path, not "errors are fatal".
* two providers, the first one's write fails -> the second provider is **never
  scanned at all**.
* a re-scan afterwards does converge, so nothing is corrupted; the cost is that
  the whole round is thrown away.

The invariant these tests pin is the one that makes isolation safe:

    **a multi-session artifact whose write did not fully land must not be
    stamped complete** -- otherwise the sessions that were never written are
    never picked up again (that is D5, one layer up).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from voyager.cli import run_scan
from voyager.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def _fail_on_nth(n: int):
    """Make the nth replace_session() call raise, as a locked database would."""
    real = Store.replace_session
    state = {"calls": 0}

    def flaky(self, *a, **kw):
        state["calls"] += 1
        if state["calls"] == n:
            raise sqlite3.OperationalError("database is locked")
        return real(self, *a, **kw)

    return flaky, real, state


def _dsh_tree(tmp_path: Path, n: int) -> Path:
    """n independent DSH session files: one file -> one session."""
    zstd = pytest.importorskip("zstandard", reason="dsh extra not installed")
    src = (FIXTURES / "dsh" / "sessions" / "--E--proj--demo--"
           / "session-dsh-1" / "session.jsonl")
    root = tmp_path / "dsh" / "sessions"
    base = src.read_text(encoding="utf-8")
    for i in range(1, n + 1):
        name = "session-dsh-%d" % i
        d = root / "--E--proj--demo--" / name
        d.mkdir(parents=True)
        text = base.replace('"id": "session-dsh-1"', '"id": "%s"' % name)
        (d / "session.jsonl.zstd").write_bytes(
            zstd.ZstdCompressor().compress(text.encode("utf-8")))
    return root


def _zcode_db(tmp_path: Path, n: int) -> Path:
    """n sessions inside ONE SQLite artifact: one artifact -> many sessions."""
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
                 1757000000000 + i * 1000000,
                 '{"role": "user", "time": {"created": %d},'
                 ' "path": {"cwd": "E:/proj/demo%d"}}'
                 % (1757000000000 + i * 1000000, i)))
            con.execute(
                "INSERT INTO part(id, message_id, session_id, time_created,"
                " time_updated, data, sequence) VALUES (?,?,?,?,?,?,0)",
                ("p_%d" % i, "m_%d" % i, sid, 1757000000000 + i * 1000000,
                 1757000000000 + i * 1000000,
                 '{"type": "text", "text": "session %d"}' % i))
        con.commit()
    finally:
        con.close()
    return path


def test_a_failing_write_does_not_stop_the_other_sources(
        tmp_path, adapter_of, monkeypatch):
    """Per-file: sources after the failure must still be processed."""
    import voyager.adapters.dsh as dsh_mod
    root = _dsh_tree(tmp_path, 3)
    monkeypatch.setattr(dsh_mod, "SESSIONS_DIR", root)

    index = tmp_path / "index.db"
    flaky, real, state = _fail_on_nth(2)
    monkeypatch.setattr(Store, "replace_session", flaky)
    store = Store(index)
    run_scan(store, providers=["dsh"], quiet=True)   # must not raise
    store.close()
    monkeypatch.setattr(Store, "replace_session", real)

    assert state["calls"] == 3, (
        "every source must be attempted; only %d were" % state["calls"])
    store = Store(index)
    n = len(store.sessions("dsh"))
    store.close()
    assert n == 2, "the two healthy sources must be indexed; got %d" % n


def test_a_failed_source_is_retried_and_converges(
        tmp_path, adapter_of, monkeypatch):
    import voyager.adapters.dsh as dsh_mod
    root = _dsh_tree(tmp_path, 3)
    monkeypatch.setattr(dsh_mod, "SESSIONS_DIR", root)

    index = tmp_path / "index.db"
    flaky, real, _ = _fail_on_nth(2)
    monkeypatch.setattr(Store, "replace_session", flaky)
    store = Store(index)
    run_scan(store, providers=["dsh"], quiet=True)
    store.close()
    monkeypatch.setattr(Store, "replace_session", real)

    store = Store(index)
    run_scan(store, providers=["dsh"], quiet=True)   # a plain re-scan
    n = len(store.sessions("dsh"))
    store.close()
    assert n == 3, "the failed source must come back on the next scan; got %d" % n


def test_a_failing_write_does_not_stop_the_next_provider(
        tmp_path, adapter_of, monkeypatch):
    """Providers are independent; one broken provider must not hide the rest.

    ``enabled_adapters()`` returns registry order, not the order passed in, so
    this asserts the property rather than which provider happens to run first:
    the failure costs exactly one session and *both* providers are still
    scanned.
    """
    import voyager.adapters.dsh as dsh_mod
    root = _dsh_tree(tmp_path, 2)
    zc = _zcode_db(tmp_path, 2)
    monkeypatch.setattr(dsh_mod, "SESSIONS_DIR", root)
    monkeypatch.setenv("VOYAGER_ZCODE_DB", str(zc))

    index = tmp_path / "index.db"
    flaky, real, _ = _fail_on_nth(1)
    monkeypatch.setattr(Store, "replace_session", flaky)
    store = Store(index)
    run_scan(store, providers=["dsh", "zcode"], quiet=True)
    store.close()
    monkeypatch.setattr(Store, "replace_session", real)

    store = Store(index)
    n_dsh = len(store.sessions("dsh"))
    n_zcode = len(store.sessions("zcode"))
    store.close()
    assert n_dsh >= 1 and n_zcode >= 1, (
        "both providers must still be scanned; got dsh=%d zcode=%d"
        % (n_dsh, n_zcode))
    assert n_dsh + n_zcode == 3, (
        "one write failed, so exactly three of the four sessions must be "
        "indexed; got dsh=%d zcode=%d" % (n_dsh, n_zcode))


def test_an_incompletely_written_artifact_is_not_stamped_complete(
        tmp_path, adapter_of, monkeypatch):
    """The D5 invariant, one layer up.

    A shared-path artifact that lost a session must keep reading as changed, or
    the missing session is never retried.
    """
    zc = _zcode_db(tmp_path, 3)
    monkeypatch.setenv("VOYAGER_ZCODE_DB", str(zc))
    ad = adapter_of("zcode")
    assert len(ad.scan(lambda p, f: True)) == 3

    index = tmp_path / "index.db"
    flaky, real, state = _fail_on_nth(2)
    monkeypatch.setattr(Store, "replace_session", flaky)
    store = Store(index)
    run_scan(store, providers=["zcode"], quiet=True)
    store.close()
    monkeypatch.setattr(Store, "replace_session", real)
    assert state["calls"] == 3

    store = Store(index)
    src_rows = store.con.execute(
        "SELECT COUNT(*) FROM sources WHERE provider='zcode'").fetchone()[0]
    store.close()
    assert src_rows == 0, (
        "the artifact lost a session, so nothing about it may be stamped "
        "fresh; %d source row(s) were written" % src_rows)

    store = Store(index)
    run_scan(store, providers=["zcode"], quiet=True)
    n = len(store.sessions("zcode"))
    store.close()
    assert n == 3, "a plain re-scan must converge; got %d of 3" % n


def test_a_parse_error_is_still_isolated(tmp_path, adapter_of, monkeypatch):
    """Guard: the parse path already behaved this way; keep it."""
    import voyager.adapters.dsh as dsh_mod
    root = _dsh_tree(tmp_path, 3)
    monkeypatch.setattr(dsh_mod, "SESSIONS_DIR", root)

    real_parse = dsh_mod.DshAdapter.parse
    state = {"n": 0}

    def flaky_parse(self, src):
        state["n"] += 1
        if state["n"] == 2:
            raise ValueError("simulated parse failure")
        return real_parse(self, src)

    monkeypatch.setattr(dsh_mod.DshAdapter, "parse", flaky_parse)
    index = tmp_path / "index.db"
    store = Store(index)
    run_scan(store, providers=["dsh"], quiet=True)
    n = len(store.sessions("dsh"))
    store.close()
    assert n == 2, "the two parseable sources must be indexed; got %d" % n


def test_a_failing_prune_does_not_abort_the_scan(tmp_path, adapter_of, monkeypatch):
    """prune is derived bookkeeping, and it writes on EVERY scan.

    Measured: a scan with nothing to index still takes the write lock for prune,
    so a concurrent writer made a no-op scan die with a traceback at
    ``store.py``'s per-source ``UPDATE sources SET last_seen``.  The indexing is
    already committed at that point; losing the round over bookkeeping the next
    scan recomputes is not a trade worth making.
    """
    import voyager.adapters.dsh as dsh_mod
    root = _dsh_tree(tmp_path, 3)
    monkeypatch.setattr(dsh_mod, "SESSIONS_DIR", root)

    def boom(self, provider, disk_paths):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "prune_missing_sessions", boom)
    index = tmp_path / "index.db"
    store = Store(index)
    run_scan(store, providers=["dsh"], quiet=True)      # must not raise
    n = len(store.sessions("dsh"))
    store.close()
    assert n == 3, "the indexing work must survive a failed prune; got %d" % n


def test_a_systemic_write_failure_still_stops_the_scan(
        tmp_path, adapter_of, monkeypatch):
    """Isolation must not turn a broken index into a silent no-op.

    One failure is a source problem; a failure on everything *and lasting* is an
    index problem, and the user has to hear about it rather than get a cheerful
    "scan complete" with nothing written.
    """
    import voyager.adapters.dsh as dsh_mod
    import voyager.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "MAX_WRITE_FAILURES", 2)
    monkeypatch.setattr(scanner_mod, "WRITE_FAILURE_GRACE_SECONDS", 0.0)
    root = _dsh_tree(tmp_path, 3)
    monkeypatch.setattr(dsh_mod, "SESSIONS_DIR", root)

    def always_fail(self, *a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Store, "replace_session", always_fail)
    index = tmp_path / "index.db"
    store = Store(index)
    with pytest.raises(sqlite3.OperationalError):
        run_scan(store, providers=["dsh"], quiet=True)
    n = len(store.sessions("dsh"))
    store.close()
    assert n == 0, "nothing may be half-written; got %d session(s)" % n


def test_a_transient_burst_of_write_failures_does_not_abort(
        tmp_path, adapter_of, monkeypatch):
    """A lock that clears must not cost the round.

    Blocked writes fail immediately rather than waiting out the busy timeout
    (measured: 25 of them in about a second), so a plain count would give up on
    a lock that clears moments later.  Only failures that also *last* stop the
    scan.
    """
    import voyager.adapters.dsh as dsh_mod
    import voyager.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "MAX_WRITE_FAILURES", 2)
    monkeypatch.setattr(scanner_mod, "WRITE_FAILURE_GRACE_SECONDS", 30.0)
    root = _dsh_tree(tmp_path, 5)
    monkeypatch.setattr(dsh_mod, "SESSIONS_DIR", root)

    real = Store.replace_session
    state = {"n": 0}

    def flaky(self, *a, **kw):
        state["n"] += 1
        if state["n"] <= 3:                 # a burst, then the lock clears
            raise sqlite3.OperationalError("database is locked")
        return real(self, *a, **kw)

    monkeypatch.setattr(Store, "replace_session", flaky)
    index = tmp_path / "index.db"
    store = Store(index)
    run_scan(store, providers=["dsh"], quiet=True)      # must not raise
    n = len(store.sessions("dsh"))
    store.close()
    assert state["n"] == 5, "every source must be attempted; got %d" % state["n"]
    assert n == 2, "the round must finish once the lock clears; got %d" % n
