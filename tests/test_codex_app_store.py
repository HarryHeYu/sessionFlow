"""The Codex app / VS Code extension store.

The gap this closes: the Codex app and its VS Code extension do **not** write
rollouts to `~/.codex/sessions`.  On this machine the newest rollout is from
2026-09-28 while the app has been used every day since, so conversations that
plainly exist were simply absent from the index.

Two rules matter and are tested here:

  * a thread already covered by a rollout is left to that pass -- re-indexing it
    from the app store would silently swap rollout-derived turns for app-derived
    ones without adding anything;
  * a thread with **no local items** (a cloud conversation) is never invented
    into turns.  It has no content to index, and saying otherwise would be a lie.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from voyager.adapters import codex as cx


def _make_dbs(tmp_path, threads, items, catalog=None, hist_name="thread_history_1.sqlite",
              state_name="state_5.sqlite"):
    """Build the two SQLite files the adapter reads, in miniature."""
    state = tmp_path / state_name
    con = sqlite3.connect(str(state))
    con.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, title TEXT, "
                "cwd TEXT, source TEXT, rollout_path TEXT, updated_at REAL)")
    for t in threads:
        con.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?)",
                    (t["id"], t.get("title"), t.get("cwd"), t.get("source"),
                     t.get("rollout_path"), t.get("updated_at", 0.0)))
    con.commit()
    con.close()

    hist = tmp_path / hist_name
    con = sqlite3.connect(str(hist))
    con.execute("CREATE TABLE IF NOT EXISTS thread_items (thread_id TEXT, turn_id TEXT, "
                "item_id TEXT, rollout_ordinal INTEGER, created_at_ms INTEGER, "
                "item_json TEXT, item_type TEXT)")
    for i, it in enumerate(items):
        con.execute("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?)",
                    (it["thread_id"], it.get("turn_id", "t"), it.get("item_id", "i%d" % i),
                     it.get("ordinal", i), it.get("ms", 1_700_000_000_000),
                     json.dumps(it["item"]), it["item"].get("type")))
    con.commit()
    con.close()

    dev = tmp_path / ("codex-dev-%s" % hist_name)
    con = sqlite3.connect(str(dev))
    con.execute("CREATE TABLE IF NOT EXISTS local_thread_catalog (thread_id TEXT, "
                "display_title TEXT, cwd TEXT, source_kind TEXT)")
    for c in (catalog or []):
        con.execute("INSERT INTO local_thread_catalog VALUES (?,?,?,?)",
                    (c["thread_id"], c.get("display_title"), c.get("cwd"),
                     c.get("source_kind")))
    con.commit()
    con.close()
    return hist, state, dev


@pytest.fixture
def appstore(tmp_path, monkeypatch):
    """One rollout-covered thread, one app-only thread, one cloud thread."""
    rollouts = tmp_path / "sessions"
    rollouts.mkdir()
    covered = rollouts / "rollout-2026-09-01T10-00-00-abc.jsonl"
    covered.write_text("{}", encoding="utf-8")

    hist, state, dev = _make_dbs(
        tmp_path,
        threads=[
            {"id": "covered-1", "title": "from a rollout", "cwd": "E:/repo",
             "source": "cli", "rollout_path": str(covered), "updated_at": 1.0},
            {"id": "app-only-1", "title": "app only", "cwd": r"\\?\E:\models\black_box",
             "source": "vscode", "rollout_path": str(tmp_path / "elsewhere.jsonl"),
             "updated_at": 2.0},
        ],
        items=[
            {"thread_id": "covered-1", "item": {"type": "userMessage",
                                                "content": [{"type": "text", "text": "hi"}]}},
            {"thread_id": "app-only-1", "ms": 1_700_000_001_000,
             "item": {"type": "userMessage",
                      "content": [{"type": "text", "text": "do the thing"}]}},
            {"thread_id": "app-only-1", "ms": 1_700_000_002_000,
             "item": {"type": "agentMessage", "text": "did the thing"}},
            {"thread_id": "app-only-1", "ms": 1_700_000_003_000,
             "item": {"type": "commandExecution", "command": "git status",
                      "cwd": r"\\?\E:\models\black_box", "exitCode": 0}},
        ],
        catalog=[{"thread_id": "cloud-1", "display_title": "a cloud chat",
                  "cwd": None, "source_kind": "chatgpt"}],
    )
    monkeypatch.setattr(cx, "SESSIONS_DIR", rollouts)
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB", hist)
    monkeypatch.setattr(cx, "STATE_DB", state)
    monkeypatch.setattr(cx, "CODEX_DEV_DB", dev)
    return tmp_path


def _scan(adapter):
    return adapter._scan_thread_history(lambda provider, path: True)


def test_an_app_only_thread_is_indexed(appstore):
    rows = _scan(cx.CodexAdapter())
    ids = {r["session"]["id"] for r in rows}
    assert "codex:app-only-1" in ids


def test_a_rollout_covered_thread_is_left_to_the_rollout_pass(appstore):
    rows = _scan(cx.CodexAdapter())
    ids = {r["session"]["id"] for r in rows}
    assert "codex:covered-1" not in ids, \
        "re-indexing it would swap rollout turns for app turns for no gain"


def test_a_cloud_thread_is_not_invented(appstore):
    rows = _scan(cx.CodexAdapter())
    assert not any(r["session"]["id"] == "codex:cloud-1" for r in rows)


def test_turns_are_built_from_the_items(appstore):
    rows = _scan(cx.CodexAdapter())
    row = next(r for r in rows if r["session"]["id"] == "codex:app-only-1")
    kinds = [e["kind"] for e in row["events"]]
    assert "user" in kinds and "assistant" in kinds and "tool_call" in kinds
    assert row["session"]["message_count"] == 2
    assert row["session"]["tool_count"] == 1


def test_the_cwd_comes_from_the_threads_own_commands(appstore):
    """An observation, not a guess: the commands record where they ran."""
    rows = _scan(cx.CodexAdapter())
    row = next(r for r in rows if r["session"]["id"] == "codex:app-only-1")
    assert row["session"]["cwd"] == "E:\\models\\black_box", \
        "the long-path prefix must be stripped"
    assert row["session"]["repo_root"] == "E:\\models\\black_box"


def test_the_cwd_prefers_the_registered_value(appstore, monkeypatch):
    sub = appstore / "case-registered"
    sub.mkdir()
    hist, state, dev = _make_dbs(
        sub, threads=[{"id": "t2", "title": "t", "cwd": "E:/registered",
                            "source": "vscode",
                            "rollout_path": str(appstore / "nope.jsonl")}],
        items=[{"thread_id": "t2", "item": {"type": "commandExecution",
                                            "command": "ls", "cwd": "E:/from-command"}}])
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB", hist)
    monkeypatch.setattr(cx, "STATE_DB", state)
    monkeypatch.setattr(cx, "CODEX_DEV_DB", dev)
    rows = _scan(cx.CodexAdapter())
    assert rows[0]["session"]["cwd"] == "E:/registered"


def test_a_registered_title_wins(appstore):
    rows = _scan(cx.CodexAdapter())
    row = next(r for r in rows if r["session"]["id"] == "codex:app-only-1")
    assert row["session"]["title"] == "app only"


def test_the_title_falls_back_to_the_first_human_turn(appstore, monkeypatch):
    """A thread the app never registered anywhere still needs a name."""
    sub = appstore / "case-title"
    sub.mkdir()
    hist, state, dev = _make_dbs(
        sub, threads=[{"id": "untitled", "title": None, "cwd": "E:/r",
                       "source": "vscode", "rollout_path": "x"}],
        items=[{"thread_id": "untitled", "item": {"type": "userMessage", "content": [
            {"type": "text", "text": "do the thing"}]}}])
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB", hist)
    monkeypatch.setattr(cx, "STATE_DB", state)
    monkeypatch.setattr(cx, "CODEX_DEV_DB", dev)
    rows = _scan(cx.CodexAdapter())
    assert rows[0]["session"]["title"] == "do the thing"


def test_injected_instruction_blocks_are_not_human_turns(appstore, monkeypatch):
    sub = appstore / "case-injected"
    sub.mkdir()
    hist, state, dev = _make_dbs(
        sub, threads=[{"id": "t3", "title": None, "cwd": "E:/r",
                            "source": "vscode", "rollout_path": "x"}],
        items=[{"thread_id": "t3", "item": {"type": "userMessage", "content": [
            {"type": "text", "text": "<environment_context>noise</environment_context>"}]}},
               {"thread_id": "t3", "item": {"type": "userMessage", "content": [
                   {"type": "text", "text": "a real question"}]}}])
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB", hist)
    monkeypatch.setattr(cx, "STATE_DB", state)
    monkeypatch.setattr(cx, "CODEX_DEV_DB", dev)
    rows = _scan(cx.CodexAdapter())
    origins = [e["origin"] for e in rows[0]["events"] if e["kind"] == "user"]
    assert origins == ["provider_bootstrap", "human"]


def test_a_thread_with_no_items_is_skipped(appstore, monkeypatch):
    sub = appstore / "case-empty"
    sub.mkdir()
    hist, state, dev = _make_dbs(
        sub, threads=[{"id": "empty", "title": "e", "cwd": "E:/r",
                            "source": "vscode", "rollout_path": "x"}], items=[])
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB", hist)
    monkeypatch.setattr(cx, "STATE_DB", state)
    monkeypatch.setattr(cx, "CODEX_DEV_DB", dev)
    assert _scan(cx.CodexAdapter()) == []


def test_missing_files_are_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB", tmp_path / "nope.sqlite")
    monkeypatch.setattr(cx, "STATE_DB", tmp_path / "nope2.sqlite")
    monkeypatch.setattr(cx, "CODEX_DEV_DB", tmp_path / "nope3.sqlite")
    assert _scan(cx.CodexAdapter()) == []


def test_the_store_is_opened_read_only(tmp_path):
    """We are a guest in someone else's database."""
    p = tmp_path / "x.sqlite"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.commit()
    con.close()
    con = cx._ro_connect(p)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO t VALUES (1)")
    finally:
        con.close()


def test_the_source_is_recorded_in_metadata(appstore):
    rows = _scan(cx.CodexAdapter())
    md = rows[0]["session"]["raw_metadata"]
    assert md["source"] == "codex_app"
    assert md["source_file"] == "thread_history_1.sqlite"


# --- the store must be visible to the scan pipeline -------------------------

def test_discover_lists_the_app_store(appstore):
    """The pipeline decides whether to call scan() at all from the change state of
    what discover() returns.  A store it never sees is a store whose updates never
    trigger a rescan -- which is exactly how this was first shipped broken: the
    adapter parsed the store correctly, and the scan reported "0 new"."""
    found = cx.CodexAdapter().discover()
    assert cx.THREAD_HISTORY_DB in found
    assert any(p.name.startswith("rollout-") for p in found)


def test_the_store_is_not_parsed_as_a_rollout(appstore):
    rows = cx.CodexAdapter().scan(lambda provider, path: True)
    ids = {r["session"]["id"] for r in rows}
    assert "codex:app-only-1" in ids
    assert not any("thread_history" in i for i in ids)


def test_scan_includes_the_app_threads_alongside_the_rollouts(appstore):
    """The app pass must not replace or suppress the rollout pass."""
    rows = cx.CodexAdapter().scan(lambda provider, path: True)
    assert any(r["session"]["id"] == "codex:app-only-1" for r in rows), "app store"
    # the fixture's rollout file is empty, so it legitimately yields no session;
    # what matters is that scan() still walked it (the group was built) and that
    # adding the app pass did not change that path
    assert all("thread_history" not in r["session"]["id"] for r in rows)


# --- the store is versioned, and can vanish --------------------------------

def test_the_store_is_found_by_glob_not_by_a_fixed_name(tmp_path, monkeypatch):
    """Codex rotated `thread_history_1.sqlite` away entirely once, so a hard-coded
    name silently stopped finding anything."""
    home = tmp_path / ".codex"
    home.mkdir()
    older = home / "thread_history_1.sqlite"
    newer = home / "thread_history_7.sqlite"
    for f in (older, newer):
        f.write_text("", encoding="utf-8")
    import os
    os.utime(older, (1_600_000_000, 1_600_000_000))
    os.utime(newer, (1_700_000_000, 1_700_000_000))

    # `_thread_history_db` appends ".codex" itself, so HOME is the parent
    monkeypatch.setattr(cx, "HOME", tmp_path)
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB", home / "thread_history_1.sqlite")
    assert cx._thread_history_db() == newer, "the newest match wins"


def test_a_missing_store_is_not_an_error(tmp_path, monkeypatch):
    """It is gone on this machine right now; discovery must simply find nothing."""
    home = tmp_path / ".codex"
    home.mkdir()
    monkeypatch.setattr(cx, "HOME", tmp_path)
    monkeypatch.setattr(cx, "SESSIONS_DIR", home / "sessions")
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB", home / "thread_history_1.sqlite")
    assert cx._thread_history_db() is None
    adapter = cx.CodexAdapter()
    assert adapter._store_paths() == []
    assert adapter.discover() == []
    assert adapter._scan_thread_history(lambda p, f: True) == []


# --- two stores: generations or shards?  Read all of them ------------------

def test_two_stores_coexist_and_both_are_read(tmp_path, monkeypatch):
    """We cannot prove whether these files are generations or shards, and reading
    only the newest would silently lose history in the shard case.  So every
    match is read; the semantics are explicit here rather than left to glob
    order."""
    home = tmp_path / ".codex"
    home.mkdir()
    a = home / "thread_history_1.sqlite"
    b = home / "thread_history_2.sqlite"
    _make_dbs(home, threads=[{"id": "in-first", "title": "first", "cwd": "E:/r",
                              "source": "vscode", "rollout_path": "x"}],
              items=[{"thread_id": "in-first", "item": {"type": "userMessage",
                                                        "content": [{"type": "text",
                                                                     "text": "one"}]}}])
    _make_dbs(home, hist_name="thread_history_2.sqlite",
              threads=[{"id": "in-second", "title": "second", "cwd": "E:/r",
                        "source": "vscode", "rollout_path": "x"}],
              items=[{"thread_id": "in-second", "item": {"type": "userMessage",
                                                         "content": [{"type": "text",
                                                                      "text": "two"}]}}])

    monkeypatch.setattr(cx, "HOME", tmp_path)
    monkeypatch.setattr(cx, "SESSIONS_DIR", home / "sessions")
    # the isolation guard patches THREAD_HISTORY_DB to a path that does not
    # exist; point it back at the *default-shaped* name so the glob branch (the
    # one production takes) is what runs here
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB",
                        home / "thread_history_1.sqlite")
    assert set(cx._thread_history_dbs()) == {a, b}

    rows = cx.CodexAdapter()._scan_thread_history(lambda p, f: True)
    ids = {r["session"]["id"] for r in rows}
    assert ids == {"codex:in-first", "codex:in-second"}, ids


def test_the_same_session_in_two_stores_is_counted_once(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    home.mkdir()
    a = home / "thread_history_1.sqlite"
    b = home / "thread_history_2.sqlite"
    for name in ("thread_history_1.sqlite", "thread_history_2.sqlite"):
        _make_dbs(home, hist_name=name,
                  threads=[{"id": "dup", "title": "dup", "cwd": "E:/r",
                            "source": "vscode", "rollout_path": "x"}],
                  items=[{"thread_id": "dup", "item": {"type": "userMessage",
                                                       "content": [{"type": "text",
                                                                    "text": "same"}]}}])

    monkeypatch.setattr(cx, "HOME", tmp_path)
    monkeypatch.setattr(cx, "SESSIONS_DIR", home / "sessions")
    # the glob branch needs the default-shaped name, not the guard's missing path
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB",
                        home / "thread_history_1.sqlite")
    rows = cx.CodexAdapter()._scan_thread_history(lambda p, f: True)
    assert [r["session"]["id"] for r in rows] == ["codex:dup"]


# --- the pipeline: --force must ignore *every* fingerprint layer ------------

def test_force_restores_a_session_whose_fingerprint_is_current(tmp_path, monkeypatch):
    """The regression that matters, driven through run_scan rather than the
    adapter: a source fingerprint can be current while the session it claims to
    cover is missing (an interrupted scan, a prune after the source rotated
    away).  A normal scan is allowed to skip it; --force must not."""
    from voyager.cli import run_scan
    from voyager.store import Store

    home = tmp_path / ".codex"
    home.mkdir()
    _make_dbs(home, threads=[{"id": "lost", "title": "lost", "cwd": "E:/repo",
                              "source": "vscode", "rollout_path": "x"}],
              items=[{"thread_id": "lost", "item": {"type": "userMessage",
                                                    "content": [{"type": "text",
                                                                 "text": "remember me"}]}}])
    monkeypatch.setattr(cx, "HOME", tmp_path)
    monkeypatch.setattr(cx, "SESSIONS_DIR", home / "sessions")
    monkeypatch.setattr(cx, "THREAD_HISTORY_DB", home / "thread_history_1.sqlite")

    store = Store(tmp_path / "index.db")
    try:
        run_scan(store, providers=["codex"], quiet=True)
        assert store.q("SELECT 1 FROM sessions WHERE id='codex:lost'"), "indexed once"

        # the session vanishes while the source fingerprint stays current.
        # Delete through the store's own path (which also clears the FTS index),
        # because a half-delete would fail on the next write for a reason that has
        # nothing to do with what this test is about.
        store.con.execute(
            "DELETE FROM event_fts WHERE rowid IN "
            "(SELECT id FROM events WHERE sid='codex:lost')")
        store.con.execute("DELETE FROM events WHERE sid='codex:lost'")
        store.con.execute("DELETE FROM sessions WHERE id='codex:lost'")
        store.con.commit()
        assert not store.q("SELECT 1 FROM sessions WHERE id='codex:lost'")

        # a normal scan is allowed to skip it: the fingerprint says "unchanged"
        run_scan(store, providers=["codex"], quiet=True)
        assert not store.q("SELECT 1 FROM sessions WHERE id='codex:lost'"), \
            "a normal incremental scan may skip an unchanged source"

        # --force must ignore every fingerprint layer and re-read
        run_scan(store, providers=["codex"], force=True, quiet=True)
        assert store.q("SELECT 1 FROM sessions WHERE id='codex:lost'"), \
            "--force means ignore every fingerprint layer"
        text = store.q("SELECT content FROM events WHERE sid='codex:lost'")
        assert any("remember me" in (r["content"] or "") for r in text)
    finally:
        store.close()
