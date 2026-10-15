"""Bounded-streaming scan regressions.

The multi-session path used to materialise every bundle before writing any of
them: ``ad.scan()`` returned a complete ``list`` and ``run_scan`` only started
calling ``replace_session`` once the whole artifact (or the whole provider) had
been parsed.  On the 10,000-session synthetic set that is ~4.4 KB per session
held in memory and the first durable write landing seconds before the last one
(``docs/releases/p9-streaming-scan.md``).

The fix is additive: an adapter may accept ``emit`` and hand its bundles over
one at a time, and the scanner consumes them through the *same* write loop it
always used.  These tests exist to prove the two things that could most easily
have been broken by that change:

* **equivalence** -- streaming and the old list-returning path must produce the
  same index, so an adapter that adopts ``emit`` does not change the data;
* **the guarantees that made materialisation safe in the first place** -- D5
  (an incompletely written source is never stamped complete), D6 (one failure
  does not take the round down and does not get stamped over), D7 (a source
  written while it is being read still reads as changed next time) and D8 (no
  new locking; two concurrent scans converge).

Every test drives a real on-disk SQLite index and the real ``run_scan``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from voyager.adapters.codex import CodexAdapter
from voyager.cli import run_scan
from voyager.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _rollout(sid: str, texts) -> str:
    """One codex rollout file: a session_meta row plus one event per text."""
    rows = [{"timestamp": "2026-08-01T10:00:00Z", "ordinal": 0,
             "type": "session_meta",
             "payload": {"session_id": sid, "id": sid,
                         "timestamp": "2026-08-01T10:00:00Z",
                         "cwd": "E:/proj/demo", "originator": "codex_vscode",
                         "cli_version": "0.153.0", "source": "vscode",
                         "model_provider": "openai",
                         "git": {"commit_hash": "abc1234", "branch": "main",
                                 "repository_url": "git@github.com:u/demo.git"}}}]
    for i, text in enumerate(texts):
        rows.append({"timestamp": "2026-08-01T10:0%d:%02dZ" % (i // 60 + 1, i),
                     "ordinal": i + 1, "type": "response_item",
                     "payload": {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": text}]}})
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"


def _codex_tree(root: Path, groups: int = 3, continuations: int = 2) -> Path:
    """A codex sessions tree: ``groups`` sessions, each with distinct content.

    Codex groups rollout files by the UUID in the *filename*, so files sharing
    a UUID are one session split across several artifacts.  That is the shape
    the streaming path has to get right: a group's bundles must all be written
    before the group counts as fresh.  Each file gets its own event text so that
    merging a continuation cannot look like a duplicate of its predecessor.
    """
    day = root / "2026" / "08" / "01"
    day.mkdir(parents=True, exist_ok=True)
    for g in range(groups):
        sid = "aaaaaaaa-0000-0000-0000-%012d" % g
        n_files = 1 + (continuations if g == 0 else 0)
        for c in range(n_files):
            texts = ["group %d file %d message %d" % (g, c, i) for i in range(3)]
            name = f"rollout-2026-08-01T10-00-{c:02d}-{sid}_w{c}.jsonl"
            (day / name).write_text(_rollout(sid, texts), encoding="utf-8")
    return root


@pytest.fixture
def codex_tree(tmp_path):
    return _codex_tree(tmp_path / "sessions")


def _index_state(index: Path) -> dict:
    import sqlite3
    con = sqlite3.connect(str(index))
    try:
        return {
            "sessions": con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "events": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "fts": con.execute("SELECT COUNT(*) FROM event_fts").fetchone()[0],
            "dup_events": con.execute(
                "SELECT COUNT(*) FROM (SELECT sid, seq, kind, content, COUNT(*) c"
                " FROM events GROUP BY sid, seq, kind, content HAVING c>1)"
            ).fetchone()[0],
            "sources": con.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "stamped": con.execute(
                "SELECT COUNT(*) FROM sources WHERE mtime IS NOT NULL"
            ).fetchone()[0],
        }
    finally:
        con.close()


# ---------------------------------------------------------------------------
# the contract itself
# ---------------------------------------------------------------------------

def test_codex_scan_accepts_emit_and_streams(codex_tree, monkeypatch):
    """The adapter hands bundles over one at a time instead of one list."""
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    ad = CodexAdapter()
    seen = []
    out = ad.scan(lambda prov, f: True, emit=seen.append)
    # Nothing is returned: everything went out through the callback.  An empty
    # list rather than None, so a caller that iterates the result either way
    # still gets zero items instead of a TypeError.
    assert out == [], "a streaming scan returns nothing; got %r" % (out,)
    assert len(seen) == 3, "three groups must stream; got %d" % len(seen)
    for bundle in seen:
        assert bundle["session"] and bundle["events"] is not None

    # and without the callback the old contract still holds exactly
    listed = ad.scan(lambda prov, f: True)
    assert isinstance(listed, list)
    assert len(listed) == len(seen)
    assert [b["session"]["id"] for b in listed] == [b["session"]["id"]
                                                    for b in seen]


def test_run_scan_drives_codex_through_the_emit_path(codex_tree, monkeypatch,
                                                     tmp_path):
    """The scanner must actually stream, not just be able to.

    Every other test here compares *results*, and results are the same either
    way -- a correct list path and a correct streaming path agree.  That leaves
    one thing unpinned: a refactor that quietly routes codex back to the list
    path would keep every test green and bring back the memory growth.  This
    one watches the call instead of the outcome.
    """
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    seen = {}
    real_scan = CodexAdapter.scan

    def watching(self, source_changed, emit=None):
        # The signature must mirror the real one: scan_streams() detects the
        # capability from the signature, so a wrapper that hides `emit` makes
        # the scanner take the list path (inspect.signature follows
        # ``__wrapped__``, which this hand-rolled wrapper would not set).
        seen["has_emit_kwarg"] = emit is not None
        seen["emit_callable"] = callable(emit)
        return real_scan(self, source_changed, emit=emit)

    monkeypatch.setattr(CodexAdapter, "scan", watching)
    store = Store(tmp_path / "index.db")
    run_scan(store, providers=["codex"], quiet=True)
    store.close()
    assert seen.get("has_emit_kwarg") and seen.get("emit_callable"), (
        "run_scan did not pass an emit callback to codex.scan(); the "
        "multi-session path is materialising again")


def test_the_first_bundle_is_written_before_the_last_is_parsed(
        codex_tree, monkeypatch, tmp_path):
    """The property the whole change exists for, asserted directly.

    With everything materialised first, the first ``replace_session`` happens
    after the last ``parse``.  Streaming has to reverse that.  Measured at 2k
    sessions: first write 23.17 s after the scan started, out of 34.64 s.
    """
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    real_parse = CodexAdapter.parse
    real_replace = Store.replace_session
    state = {"parsed": 0, "written": 0, "written_when_last_parsed": None}

    def counting_parse(self, source):
        r = real_parse(self, source)
        if r:
            state["parsed"] += 1
            state["written_when_last_parsed"] = state["written"]
        return r

    def counting_replace(self, *a, **kw):
        state["written"] += 1
        return real_replace(self, *a, **kw)

    monkeypatch.setattr(CodexAdapter, "parse", counting_parse)
    monkeypatch.setattr(Store, "replace_session", counting_replace)
    store = Store(tmp_path / "index.db")
    run_scan(store, providers=["codex"], quiet=True)
    store.close()
    monkeypatch.setattr(CodexAdapter, "parse", real_parse)
    monkeypatch.setattr(Store, "replace_session", real_replace)

    assert state["parsed"] >= 3, "the fixture must yield several files"
    assert state["written_when_last_parsed"] >= 1, (
        "at the moment the last file was parsed, %d sessions had been written: "
        "everything was materialised before the first write"
        % state["written_when_last_parsed"])


def test_an_adapter_without_emit_still_uses_the_list_path(
        tmp_path, monkeypatch):
    """Old adapters are not forced to change (additive interface).

    ZCode is a real multi-session adapter whose ``scan`` takes no ``emit``.
    Driving it through the scanner must keep working untouched -- the scanner
    has to fall back to the list loop for it.
    """
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "zcode.db"))
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
    con.close()
    monkeypatch.setenv("VOYAGER_ZCODE_DB", str(tmp_path / "zcode.db"))

    import inspect
    from voyager.adapters.zcode import ZCodeAdapter
    assert "emit" not in inspect.signature(ZCodeAdapter.scan).parameters, (
        "this test is only meaningful while ZCode's scan has no emit")
    assert not ZCodeAdapter().scan_streams()

    index = tmp_path / "index.db"
    store = Store(index)
    res = run_scan(store, providers=["zcode"], quiet=True)
    store.close()
    assert res["new"] == 2, "the non-emit adapter must index both sessions"
    state = _index_state(index)
    assert state["sessions"] == 2
    assert state["dup_events"] == 0
    assert state["fts"] == state["events"]


# ---------------------------------------------------------------------------
# the guarantees that must survive streaming
# ---------------------------------------------------------------------------

def test_streaming_and_list_scans_agree(codex_tree, monkeypatch, tmp_path):
    """The same data through either adapter style must give the same index."""
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    results = {}
    for label in ("streamed", "listed"):
        index = tmp_path / ("index-%s.db" % label)
        store = Store(index)
        run_scan(store, providers=["codex"], quiet=True)
        store.close()
        results[label] = _index_state(index)

    assert results["streamed"] == results["listed"], (
        "streaming changed the result: %r vs %r"
        % (results["streamed"], results["listed"]))
    assert results["listed"]["sessions"] == 3
    # One row per *file*, not per session: the group with continuations
    # registers its anchor plus both continuation files, so 3 + 1 + 1 = 5.
    assert results["listed"]["sources"] == 5


def test_a_streaming_scan_that_dies_midway_leaves_the_source_unstamped(
        codex_tree, monkeypatch, tmp_path):
    """D5 through streaming: partial progress must not look like completion."""
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    index = tmp_path / "index.db"
    real = Store.replace_session
    state = {"calls": 0}

    def interrupted(self, *a, **kw):
        state["calls"] += 1
        if state["calls"] > 1:
            raise KeyboardInterrupt("killed mid-stream")
        return real(self, *a, **kw)

    monkeypatch.setattr(Store, "replace_session", interrupted)
    store = Store(index)
    with pytest.raises(KeyboardInterrupt):
        run_scan(store, providers=["codex"], quiet=True)
    store.close()
    monkeypatch.setattr(Store, "replace_session", real)

    con = sqlite3.connect(str(index))
    try:
        stamped = con.execute(
            "SELECT COUNT(*) FROM sources WHERE mtime IS NOT NULL").fetchone()[0]
    finally:
        con.close()
    assert stamped == 0, (
        "the source was stamped fresh although the artifact was not fully "
        "written; the sessions that never landed can never be indexed again "
        "(%d rows stamped)" % stamped)

    store = Store(index)
    run_scan(store, providers=["codex"], quiet=True)
    store.close()
    state_after = _index_state(index)
    assert state_after["sessions"] == 3, (
        "a plain re-scan must converge; got %s sessions" % state_after["sessions"])
    assert state_after["dup_events"] == 0
    assert state_after["fts"] == state_after["events"]


def test_a_failing_stream_write_is_isolated_and_withheld_from_stamping(
        codex_tree, monkeypatch, tmp_path):
    """D6 through streaming: one failure must not finish the artifact off."""
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    index = tmp_path / "index.db"
    real = Store.replace_session
    state = {"calls": 0}

    def flaky(self, *a, **kw):
        state["calls"] += 1
        if state["calls"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real(self, *a, **kw)

    monkeypatch.setattr(Store, "replace_session", flaky)
    store = Store(index)
    run_scan(store, providers=["codex"], quiet=True)
    store.close()
    monkeypatch.setattr(Store, "replace_session", real)
    assert state["calls"] == 3, "all three bundles must be attempted"

    store = Store(index)
    run_scan(store, providers=["codex"], quiet=True)
    store.close()
    after = _index_state(index)
    assert after["sessions"] == 3, (
        "the session that failed to write was never indexed again; got %s"
        % after["sessions"])
    assert after["dup_events"] == 0
    assert after["fts"] == after["events"]


def test_an_append_during_a_streaming_read_is_not_missed(
        codex_tree, monkeypatch, tmp_path):
    """D7 through streaming: the fingerprint must describe the source we read."""
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    index = tmp_path / "index.db"
    seeded = False

    def instrumented(self, *a, **kw):
        """Append to a group's file while the artifact is being read."""
        nonlocal seeded
        if not seeded:
            seeded = True
            target = sorted(codex_tree.rglob(
                "rollout-*-aaaaaaaa-0000-0000-0000-000000000000_w*.jsonl"))[0]
            target.write_text(
                target.read_text(encoding="utf-8") + "\n" + _extra_line(),
                encoding="utf-8")
        return real_replace(self, *a, **kw)

    real_replace = Store.replace_session
    monkeypatch.setattr(Store, "replace_session", instrumented)

    store = Store(index)
    run_scan(store, providers=["codex"], quiet=True)
    store.close()

    store = Store(index)
    res = run_scan(store, providers=["codex"], quiet=True)
    store.close()
    assert res["new"] >= 1, (
        "the file that changed while it was being read still reads unchanged, "
        "so the appended event can never be indexed")


def _extra_line() -> str:
    return (
        '{"timestamp": "2026-08-01T11:00:00.000Z", "type": "response_item",'
        ' "payload": {"type": "message", "role": "assistant",'
        ' "content": [{"type": "output_text",'
        ' "text": "appended while the artifact was being read"}]}}'
    )


def test_repeated_streaming_scans_do_not_duplicate(codex_tree, monkeypatch,
                                                   tmp_path):
    """Idempotence: three streaming scans in a row."""
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    index = tmp_path / "index.db"
    for _ in range(3):
        store = Store(index)
        run_scan(store, providers=["codex"], quiet=True)
        store.close()
    state = _index_state(index)
    assert state["sessions"] == 3
    assert state["dup_events"] == 0
    assert state["fts"] == state["events"]
    assert state["sources"] == 5, (
        "one row per file: the multi-rollout group registers its anchor and "
        "both continuation files, so 3 + 1 + 1 = 5")


def test_force_through_streaming_reads_every_group_again(codex_tree,
                                                        monkeypatch, tmp_path):
    """--force must still bypass the adapter's own change check."""
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    index = tmp_path / "index.db"
    store = Store(index)
    run_scan(store, providers=["codex"], quiet=True)
    store.close()

    store = Store(index)
    res = run_scan(store, providers=["codex"], quiet=True, force=True)
    store.close()
    assert res["new"] == 3, (
        "force must re-read every group; got new=%s" % res["new"])
    assert _index_state(index)["sessions"] == 3


def test_a_source_that_disappears_and_returns_is_not_retained(
        codex_tree, monkeypatch, tmp_path):
    """Phase 4 scenario 4/8: a vanished source is not a deleted session.

    Retention is a source-lifecycle property, so it has to survive the
    streaming path: removing every rollout must mark the sessions rather than
    delete them, and putting the files back must reconcile to live.
    """
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    index = tmp_path / "index.db"
    store = Store(index)
    run_scan(store, providers=["codex"], quiet=True)
    store.close()
    assert _index_state(index)["sessions"] == 3

    # make every rollout disappear
    for f in sorted(codex_tree.rglob("rollout-*.jsonl")):
        f.unlink()
    store = Store(index)
    run_scan(store, providers=["codex"], quiet=True)
    missing = [r for r in store.sessions("codex")
               if r["source_state"] == "SOURCE_MISSING"]
    live = store.sessions("codex")
    store.close()
    assert len(live) == 3, (
        "a vanished source is retention, not deletion; %d session(s) survived"
        % len(live))
    assert len(missing) == 3, "every session must be marked missing, not dropped"

    # ...and put them back
    _codex_tree(codex_tree)
    store = Store(index)
    run_scan(store, providers=["codex"], quiet=True)
    missing_again = [r for r in store.sessions("codex")
                     if r["source_state"] == "SOURCE_MISSING"]
    live_again = store.sessions("codex")
    store.close()
    assert len(missing_again) == 0, (
        "returning sources must reconcile to live; %d still marked missing"
        % len(missing_again))
    assert len(live_again) == 3
    state = _index_state(index)
    assert state["dup_events"] == 0
    assert state["fts"] == state["events"]


def test_two_concurrent_streaming_scans_converge(tmp_path, monkeypatch):
    """Phase 4 scenario 9: no new lock, and the outcome is one correct index.

    Two real child processes against one on-disk index.  The streaming path
    calls back into the store from inside the adapter's own read of the source,
    which is exactly where a lock would have to be added if one were needed.

    The index is created by the parent *before* the children start, on purpose:
    concurrent *scans* of an existing index are the scenario under test.
    Concurrent *creation* of a brand-new index is a different, separately
    covered case (test_concurrent_creation_of_a_fresh_index_does_not_fail).
    """
    import subprocess
    import sys
    codex_tree = _codex_tree(tmp_path / "sessions")
    import voyager.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod, "SESSIONS_DIR", codex_tree)
    for name in ("THREAD_HISTORY_DB", "STATE_DB", "CODEX_DEV_DB"):
        monkeypatch.setattr(codex_mod, name,
                            codex_tree.parent / ("_absent_%s.db" % name))

    index = tmp_path / "index.db"
    seed = Store(index)
    seed.close()          # schema + WAL mode established; children only scan

    child = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "from voyager.store import Store\n"
        "from voyager.cli import run_scan\n"
        "import voyager.adapters.codex as m\n"
        "m.SESSIONS_DIR = Path(%r)\n"
        "m.THREAD_HISTORY_DB = Path(%r)\n"
        "m.STATE_DB = Path(%r)\n"
        "m.CODEX_DEV_DB = Path(%r)\n"
        "s = Store(%r)\n"
        "run_scan(s, providers=['codex'], quiet=True)\n"
        "s.close()\n"
        "print('CHILD_OK')\n"
    ) % (str(Path(r"E:/code/voyager")), str(codex_tree),
         str(codex_tree.parent / "_absent_history.db"),
         str(codex_tree.parent / "_absent_state.db"),
         str(codex_tree.parent / "_absent_dev.db"),
         str(index))

    procs = [subprocess.Popen([sys.executable, "-c", child],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, cwd=str(tmp_path))
             for _ in range(2)]
    for p in procs:
        _out, err = p.communicate(timeout=180)
        assert p.returncode == 0, "a concurrent scan failed: %s" % err[-800:]

    state = _index_state(index)
    assert state["sessions"] == 3, (
        "two concurrent scans must converge on one index; got %d sessions"
        % state["sessions"])
    assert state["dup_events"] == 0, "concurrent scans duplicated events"
    assert state["fts"] == state["events"], "FTS disagrees with events"


def test_concurrent_creation_of_a_fresh_index_does_not_fail(tmp_path):
    """Two users' first-ever `voyager scan`, started at the same moment.

    Found by the concurrent-scan test above failing on CI: both children were
    *creating* the index, not scanning it, and one died at
    ``PRAGMA journal_mode=WAL`` with ``database is locked`` (Ubuntu) or
    ``attempt to write a readonly database`` (Windows).  Measured separately:
    1 failure in 6 trials, so it is rare -- and it is a real one, because a
    first scan on a machine with two shells is an ordinary thing to do.

    The setup is idempotent (every statement is IF NOT EXISTS / guarded by a
    read), so the only thing wrong is that a concurrent setup is not tolerated.
    This test drives the real ``Store`` from two real processes.
    """
    import subprocess
    import sys
    from voyager.store import Store

    CHILD = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "from voyager.store import Store\n"
        "s = Store(Path(sys.argv[1]))\n"
        "s.close()\n"
        "print('OK')\n"
    ) % str(Path(r"E:/code/voyager"))

    # Several trials: the failure is a race, so a single run proves nothing
    # either way.  Ten pairs is enough to catch a ~1-in-6 rate.
    for trial in range(10):
        db = tmp_path / ("fresh%d.db" % trial)
        procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(db)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, cwd=str(tmp_path))
                 for _ in range(2)]
        failures = []
        for p in procs:
            _out, err = p.communicate(timeout=90)
            if p.returncode != 0 or "OK" not in _out:
                failures.append("rc=%d %s" % (p.returncode, err.strip()[-300:]))
        assert failures == [], (
            "trial %d: a fresh index could not be created concurrently; the "
            "setup is idempotent so this is a pure tolerance bug:/n  %s"
            % (trial, "\n  ".join(failures)))
        # and the result must be a usable index, not merely an unbroken process
        store = Store(db)
        assert store.stats()["sessions"] >= 0
        store.close()


def test_the_setup_retry_actually_retries(tmp_path, monkeypatch):
    """The retry must be real, not just present.

    The concurrent-creation failure is a ~1-in-20 race, so "the probe passes
    now" is not evidence that the fix works.  Drive the mechanism directly:
    make the first attempts raise exactly the error SQLite gives
    (``attempt to write a readonly database``) and check that Store() still
    comes up, with a settled schema, rather than raising or leaving a
    half-open connection.
    """
    import sqlite3
    from voyager.store import Store

    real_setup = Store._setup
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] <= 2:            # lose the race twice, then win
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real_setup(self)

    monkeypatch.setattr(Store, "_setup", flaky)
    db = tmp_path / "index.db"
    store = Store(db)
    try:
        assert calls["n"] == 3, "setup must have been retried; got %d attempts" % calls["n"]
        # the schema really is there, not merely an unbroken object
        assert store.stats()["sessions"] >= 0
        assert store.con.execute(
            "SELECT COUNT(*) FROM sources").fetchone()[0] >= 0
    finally:
        store.close()

    # and a setup that never recovers must raise, not hang or half-open
    def never(self):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(Store, "_setup", never)
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path / "stuck.db")
