"""OpenCode adapter — synthetic (UNIT_VERIFIED) contract tests.

Every database here is built by tests/opencode_fixtures.py from DDL that mirrors
upstream v2.2.6 / the v1 migration test. No real OpenCode install is read, no
provider credentials are used, and the isolation fixture redirects HOME and
XDG_DATA_HOME before any test body runs.

What these tests prove: the adapter can index a synthetic OpenCode database
correctly, incrementally, idempotently and read-only. What they do NOT prove:
that a real OpenCode build writes this exact JSON, or that any form of native
resume works. See docs/audits/2026-11-opencode-phase3-report.md.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from opencode_fixtures import (T0, append_message, make_corrupt, make_legacy,
                               make_unknown, make_v2, make_v20_inline,
                               open_writer)


@pytest.fixture
def oc_env(monkeypatch, tmp_path):
    """Point discovery at one scratch directory and nothing else."""
    home = tmp_path / "home"
    data = home / ".local" / "share" / "opencode"
    data.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.delenv("VOYAGER_OPENCODE_DB", raising=False)
    monkeypatch.delenv("OPENCODE_DISABLE_CHANNEL_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    return data


@pytest.fixture
def adapter(adapter_of):
    return adapter_of("opencode")


def _index(db, bundles):
    """Minimal in-memory view of what the scanner would write."""
    return {b["session"]["id"]: b for b in bundles}


# --- schema probe ----------------------------------------------------------

def test_v2_schema_is_recognised(oc_env):
    from voyager.adapters import opencode as oc
    db = make_v2(oc_env / "opencode.db")
    assert oc.probe(db)[0] == oc.PROBE_OK
    assert oc.probe(db)[1] == "v2"


def test_legacy_schema_is_recognised(oc_env):
    from voyager.adapters import opencode as oc
    db = make_legacy(oc_env / "opencode.db")
    assert oc.probe(db) == (oc.PROBE_OK, "legacy")


def test_unknown_schema_is_named_not_silently_ignored(oc_env, adapter):
    from voyager.adapters import opencode as oc
    db = make_unknown(oc_env / "opencode.db")
    assert oc.probe(db)[0] == oc.PROBE_UNKNOWN_SCHEMA
    assert db in adapter.unusable_sources()
    assert db in adapter.unrecognised_sources()
    assert adapter.discover() == []


def test_corrupt_database_is_unreadable_not_absent(oc_env, adapter):
    from voyager.adapters import opencode as oc
    db = make_corrupt(oc_env / "opencode.db")
    assert oc.probe(db)[0] == oc.PROBE_UNREADABLE
    assert db in adapter.unusable_sources()
    assert adapter.discover() == []


def test_absent_database_is_absent(oc_env, adapter):
    from voyager.adapters import opencode as oc
    assert oc.probe(oc_env / "opencode.db")[0] == oc.PROBE_ABSENT
    assert adapter.discover() == []
    assert adapter.unusable_sources() == []


def test_explicit_override_takes_priority(oc_env, monkeypatch, adapter):
    other = oc_env.parent / "custom.db"
    make_v2(other)
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(other))
    assert other in adapter.discover()


def test_channel_database_is_discovered(oc_env, adapter):
    db = make_v2(oc_env / "opencode-beta.db")
    assert db in adapter.discover()


# --- mapping ---------------------------------------------------------------

def test_v2_session_and_events(oc_env, adapter):
    make_v2(oc_env / "opencode.db")
    bundles = adapter.scan(lambda p, f: True)
    assert len(bundles) == 1
    b = bundles[0]
    s, evs = b["session"], b["events"]
    assert s["id"] == "opencode:ses_000"
    assert s["title"] == "修复补丁"
    assert s["cwd"] == "/repo/demo"
    assert s["repo_root"] == "/repo/demo"       # from project.worktree
    assert s["model"] == "anthropic:claude-sonnet-4"
    assert s["can_resume"] is False and s["resume_cmd"] is None
    assert abs(s["started_at"] - T0 / 1000.0) < 0.001
    # 1 user + 2 assistant text parts ("正在处理" and the Unicode part).
    assert s["message_count"] == 3
    assert s["tool_count"] == 1
    assert s["metadata"]["usage_totals"] == {"input": 120, "output": 40}

    kinds = [e["kind"] for e in evs]
    assert kinds.count("user") == 1
    assert kinds.count("assistant") >= 1
    assert kinds.count("reasoning") == 1
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1

    user = next(e for e in evs if e["kind"] == "user")
    assert user["content"] == "补丁已完成"
    assert user["origin"] == "human"

    call = next(e for e in evs if e["kind"] == "tool_call")
    assert call["tool_name"] == "bash"
    assert call["tool_call_id"] == "call_1"
    assert call["command"] == "pytest -q"
    assert json.loads(call["tool_input"]) == {"command": "pytest -q"}

    result = next(e for e in evs if e["kind"] == "tool_result")
    assert result["tool_output"] == "done"
    assert result["tool_call_id"] == "call_1"

    # Unicode survives the round trip into event bodies.
    assert any("日本語" in (e["content"] or "") for e in evs)
    assert any("🚀" in (e["content"] or "") for e in evs)


def test_tool_result_is_not_invented_for_a_running_tool(oc_env, adapter):
    db = make_v2(oc_env / "opencode.db")
    con = sqlite3.connect(str(db))
    part = {"type": "tool", "id": "call_running", "name": "bash",
            "state": {"status": "running", "input": {"command": "sleep 1"}},
            "time": {"created": T0}}
    con.execute("UPDATE session_part SET data=? WHERE type='tool'",
                (json.dumps(part, ensure_ascii=False),))
    con.commit()
    con.close()
    evs = adapter.scan(lambda p, f: True)[0]["events"]
    assert [e["kind"] for e in evs].count("tool_call") == 1
    assert not any(e["kind"] == "tool_result" for e in evs)


def test_tool_error_is_recorded_without_a_fake_exit_code(oc_env, adapter):
    db = make_v2(oc_env / "opencode.db")
    con = sqlite3.connect(str(db))
    part = {"type": "tool", "id": "call_err", "name": "bash",
            "state": {"status": "error", "input": {"command": "false"},
                      "error": {"message": "non-zero exit"}},
            "time": {"created": T0}}
    con.execute("UPDATE session_part SET data=? WHERE type='tool'",
                (json.dumps(part, ensure_ascii=False),))
    con.commit()
    con.close()
    evs = adapter.scan(lambda p, f: True)[0]["events"]
    call = next(e for e in evs if e["kind"] == "tool_call")
    assert call["exit_code"] is None         # no explicit integer upstream
    result = next(e for e in evs if e["kind"] == "tool_result")
    assert "non-zero exit" in result["tool_output"]


def test_synthetic_message_is_not_promoted_to_a_human_turn(oc_env, adapter):
    """Provider-injected pseudo-user text must not read as a user instruction."""
    db = make_v2(oc_env / "opencode.db")
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                ("msg_syn", "ses_000", "synthetic", 9, T0 + 50, T0 + 50,
                 json.dumps({"text": "user says tests passed",
                             "time": {"created": T0 + 50}})))
    con.commit()
    con.close()
    evs = adapter.scan(lambda p, f: True)[0]["events"]
    assert not any(e["kind"] == "user" and "tests passed" in (e["content"] or "")
                   for e in evs)
    meta = next(e for e in evs if "tests passed" in (e["content"] or ""))
    assert meta["kind"] == "meta" and meta["origin"] == "provider_system"


def test_subagent_session_user_turn_is_not_human(oc_env, adapter):
    db = make_v2(oc_env / "opencode.db")
    con = sqlite3.connect(str(db))
    con.execute("UPDATE session_v2 SET parent_id=? WHERE id=?",
                ("ses_parent", "ses_000"))
    con.commit()
    con.close()
    evs = adapter.scan(lambda p, f: True)[0]["events"]
    user = next(e for e in evs if e["kind"] == "user")
    assert user["origin"] == "provider_synthetic"


def test_legacy_shape_maps_tool_call_and_result(oc_env, adapter):
    make_legacy(oc_env / "opencode.db")
    bundles = adapter.scan(lambda p, f: True)
    assert len(bundles) == 1
    evs = bundles[0]["events"]
    call = next(e for e in evs if e["kind"] == "tool_call")
    assert call["tool_name"] == "read"
    assert call["tool_call_id"] == "legacy_call"
    result = next(e for e in evs if e["kind"] == "tool_result")
    assert result["tool_output"] == "legacy output"
    assert bundles[0]["session"]["metadata"]["schema"] == "legacy"


def test_malformed_json_rows_are_skipped_not_crashing(oc_env, adapter):
    db = make_v2(oc_env / "opencode.db")
    con = sqlite3.connect(str(db))
    con.execute("UPDATE session_part SET data='{not json' WHERE ordinal=0")
    con.execute("UPDATE session_message SET data='{}' WHERE type='assistant'")
    con.commit()
    con.close()
    bundles = adapter.scan(lambda p, f: True)
    assert len(bundles) == 1                 # still a usable session
    assert bundles[0]["events"]


def test_empty_session_with_no_title_is_not_indexed(oc_env, adapter):
    db = make_v2(oc_env / "opencode.db", tool=False)
    con = sqlite3.connect(str(db))
    con.execute("UPDATE session_v2 SET title=NULL")
    con.execute("DELETE FROM session_part")
    con.execute("DELETE FROM session_message")
    con.commit()
    con.close()
    assert adapter.scan(lambda p, f: True) == []


# --- read-only -------------------------------------------------------------

def test_database_content_never_changes(oc_env, adapter, monkeypatch):
    """A read-only adapter must not alter any provider data.

    SQLite itself may CREATE ``-shm`` (shared-memory index) when a WAL database
    is opened, even with ``mode=ro``; that is a SQLite side effect, not a write
    to the session history. ``immutable=1`` would avoid it but hides committed
    WAL pages, which would silently drop live turns — see the design note.
    So we assert on the main database bytes, the WAL bytes and the logical
    content, and document the -shm behaviour instead of claiming "no files".
    """
    db = make_v2(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")

    main_before = db.read_bytes()
    wal_before = wal.read_bytes() if wal.is_file() else None

    def logical_content():
        con = sqlite3.connect(str(db))
        try:
            return con.execute(
                "SELECT count(*) FROM session_v2").fetchone()[0], con.execute(
                "SELECT count(*) FROM session_message").fetchone()[0], con.execute(
                "SELECT count(*) FROM session_part").fetchone()[0]
        finally:
            con.close()

    content_before = logical_content()
    adapter.scan(lambda p, f: True)
    assert db.read_bytes() == main_before, "main database changed"
    if wal_before is not None:
        assert wal.read_bytes() == wal_before, "WAL changed"
    assert logical_content() == content_before, "session rows changed"
    # -shm may now exist; it is a shared-memory index, never session content.
    if shm.is_file():
        assert b"ses_000" not in shm.read_bytes()


def test_writes_are_refused_by_the_connection(oc_env):
    from voyager.adapters import opencode as oc
    db = make_v2(oc_env / "opencode.db")
    con = oc._open_ro(db)
    assert con is not None
    with pytest.raises(sqlite3.Error):
        con.execute("INSERT INTO project VALUES ('x','x',NULL,NULL,1,1,'[]',1,1,1,0,1,'[]',NULL)")
    con.close()


# --- incremental / WAL / SOURCE_MISSING ------------------------------------

def test_wal_side_files_take_part_in_discovery(oc_env, adapter, monkeypatch):
    """A live writer's WAL must be visible to source_changed(), or new turns
    are skipped until a checkpoint rewrites the main database file."""
    db = make_v2(oc_env / "opencode.db", wal=True)
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    writer = open_writer(db)
    try:
        append_message(writer, "ses_000", "WAL 里的新消息")
        wal = Path(str(db) + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        found = adapter.discover()
        assert db in found
        assert wal in found, "WAL must be a discovery source for incremental scans"
    finally:
        writer.close()


def test_rescan_after_wal_write_sees_the_new_turn(oc_env, adapter, monkeypatch):
    db = make_v2(oc_env / "opencode.db", wal=True)
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    first = adapter.scan(lambda p, f: True)
    assert not any("WAL" in (e["content"] or "") for e in first[0]["events"])
    writer = open_writer(db)
    try:
        append_message(writer, "ses_000", "WAL 里的新消息")
        second = adapter.scan(lambda p, f: True)
        assert any("WAL" in (e["content"] or "") for e in second[0]["events"])
    finally:
        writer.close()


def test_scan_is_idempotent(oc_env, adapter, monkeypatch):
    db = make_v2(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    a = adapter.scan(lambda p, f: True)
    b = adapter.scan(lambda p, f: True)
    assert len(a) == len(b) == 1
    assert a[0]["session"] == b[0]["session"]
    assert [(e["seq"], e["kind"], e["content"]) for e in a[0]["events"]] == [
        (e["seq"], e["kind"], e["content"]) for e in b[0]["events"]]


def test_streaming_and_list_styles_index_identically(oc_env, adapter, monkeypatch):
    """emit= must not change what is produced (the #18 invariant)."""
    db = make_v2(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    listed = adapter.scan(lambda p, f: True)
    streamed = []
    adapter.scan(lambda p, f: True, emit=streamed.append)
    assert len(listed) == len(streamed) == 1
    assert listed[0]["session"] == streamed[0]["session"]
    assert [e["seq"] for e in listed[0]["events"]] == [
        e["seq"] for e in streamed[0]["events"]]
    assert adapter.scan_streams() is True


def test_unreadable_database_is_kept_not_treated_as_vanished(oc_env, adapter,
                                                            monkeypatch):
    db = make_corrupt(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    assert adapter.scan(lambda p, f: True) == []
    assert db in adapter.unusable_sources()


def test_unknown_schema_prints_a_warning_instead_of_zero_sessions(oc_env,
                                                                 adapter,
                                                                 monkeypatch,
                                                                 capsys):
    db = make_unknown(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    adapter.scan(lambda p, f: True)
    assert "unknown-schema" in capsys.readouterr().err


# --- scanner integration (real Store, synthetic provider db) ---------------

def test_scanner_indexes_and_search_finds_two_character_cjk(oc_env, monkeypatch,
                                                            tmp_path):
    """End to end through run_scan: adapter -> Store -> search."""
    from voyager.scanner import run_scan
    from voyager.store import Store
    db = make_v2(oc_env / "opencode.db", text="账本对不上")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    store = Store(tmp_path / "index.db")
    try:
        run_scan(store, providers=["opencode"], quiet=True)
        rows = store.q("SELECT id, provider, source_state FROM sessions")
        assert len(rows) == 1
        assert rows[0]["provider"] == "opencode"
        assert rows[0]["source_state"] == "ACTIVE_SOURCE"
        hits = store.search("账本")
        assert hits, "the 2-character CJK fallback must find the event"
        # A second scan must not duplicate anything.
        run_scan(store, providers=["opencode"], quiet=True)
        assert store.con.execute(
            "SELECT count(*) FROM sessions WHERE provider='opencode'").fetchone()[0] == 1
        assert store.con.execute("SELECT count(*) FROM events").fetchone()[0] == len(
            store.q("SELECT id FROM events"))
    finally:
        store.close()


def test_wal_write_triggers_an_incremental_rescan(oc_env, monkeypatch,
                                                  tmp_path):
    """The whole point of putting -wal in discover(): source_changed() sees it.

    Without this, a running OpenCode agent's new turns stay invisible until a
    checkpoint happens to rewrite the main .db file — which may be never while
    the process is alive. This test goes through the real Store/scanner path.
    """
    from voyager.scanner import run_scan
    from voyager.store import Store
    db = make_v2(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    store = Store(tmp_path / "index.db")
    try:
        first = run_scan(store, providers=["opencode"], quiet=True)
        n1 = store.con.execute("SELECT count(*) FROM events").fetchone()[0]
        assert first["new"] == 1

        writer = open_writer(db)
        try:
            append_message(writer, "ses_000", "增量新消息")
            assert Path(str(db) + "-wal").is_file()
            second = run_scan(store, providers=["opencode"], quiet=True)
            n2 = store.con.execute("SELECT count(*) FROM events").fetchone()[0]
            assert second["new"] == 1, "WAL change must trigger a rescan"
            assert n2 == n1 + 1, (n1, n2)
            assert store.search("增量新消息"), "new turn must be searchable"
        finally:
            writer.close()
    finally:
        store.close()


def test_vanished_database_retains_history_as_source_missing(oc_env, monkeypatch,
                                                            tmp_path):
    """O2: a source that disappears is retained, never deleted."""
    from voyager.scanner import run_scan
    from voyager.store import Store
    db = make_v2(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    store = Store(tmp_path / "index.db")
    try:
        run_scan(store, providers=["opencode"], quiet=True)
        n_events = store.con.execute("SELECT count(*) FROM events").fetchone()[0]
        assert n_events > 0
        db.unlink()
        run_scan(store, providers=["opencode"], quiet=True)
        row = store.q("SELECT source_state FROM sessions WHERE provider='opencode'")
        assert row and row[0]["source_state"] == "SOURCE_MISSING"
        assert store.con.execute("SELECT count(*) FROM events").fetchone()[0] == n_events
    finally:
        store.close()


def test_large_session_fixture_completes(oc_env, adapter, monkeypatch):
    db = make_v2(oc_env / "opencode.db", n_messages=5000)
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    bundles = adapter.scan(lambda p, f: True)
    assert len(bundles) == 1
    assert len(bundles[0]["events"]) >= 5000


# --- v2.0.x inline shape (no session_part; content in data.content) --------

def test_v20_inline_schema_is_recognised(oc_env):
    from voyager.adapters import opencode as oc
    db = make_v20_inline(oc_env / "opencode.db")
    assert oc.probe(db) == (oc.PROBE_OK, "v2-inline")


def test_parts_schema_wins_over_inline_when_session_part_exists(oc_env):
    """A v2.2.x database also has session_message; it must stay 'v2'."""
    from voyager.adapters import opencode as oc
    db = make_v2(oc_env / "opencode.db")
    assert oc.probe(db) == (oc.PROBE_OK, "v2")


def test_v20_inline_session_and_events(oc_env, adapter, monkeypatch):
    db = make_v20_inline(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    bundles = adapter.scan(lambda p, f: True)
    assert len(bundles) == 1
    b = bundles[0]
    s, evs = b["session"], b["events"]
    assert s["id"] == "opencode:ses_inline_000"
    assert s["title"] == "内联形状"
    assert s["metadata"]["schema"] == "v2-inline"
    assert s["can_resume"] is False and s["can_fork"] is False
    kinds = [e["kind"] for e in evs]
    assert kinds.count("user") == 1
    assert kinds.count("assistant") >= 2          # text parts, incl. CJK
    assert kinds.count("reasoning") == 1
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    # CJK text survives
    assert any("日本語" in (e.get("content") or "") for e in evs)
    # user turn is the human prompt; system/compaction stay meta
    assert [e for e in evs if e["kind"] == "user"][0]["content"] == "补丁已完成"
    metas = [e for e in evs if e["kind"] == "meta"]
    assert any("system note" in (e.get("content") or "") for e in metas)
    assert any("compacted summary" in (e.get("content") or "") for e in metas)
    assert all(e["origin"] == "provider_system" for e in metas)


def test_v20_inline_tool_exit_comes_from_state_metadata(oc_env, adapter,
                                                        monkeypatch):
    db = make_v20_inline(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    evs = adapter.scan(lambda p, f: True)[0]["events"]
    call = [e for e in evs if e["kind"] == "tool_call"][0]
    assert call["tool_name"] == "bash"
    assert call["tool_call_id"] == "call_1"
    assert call["command"] == "pytest -q"
    # exit 0 lives in state.metadata.exit and must survive as 0, not None
    assert call["exit_code"] == 0
    result = [e for e in evs if e["kind"] == "tool_result"][0]
    assert "done" in (result.get("tool_output") or "")


def test_v20_inline_running_tool_gets_no_invented_result(oc_env, adapter,
                                                         monkeypatch):
    db = make_v20_inline(oc_env / "opencode.db", running_tool=True)
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    evs = adapter.scan(lambda p, f: True)[0]["events"]
    assert [e for e in evs if e["kind"] == "tool_call"]
    assert not [e for e in evs if e["kind"] == "tool_result"]


def test_v20_inline_empty_user_text_is_skipped(oc_env, adapter, monkeypatch):
    db = make_v20_inline(oc_env / "opencode.db", empty_user=True)
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    evs = adapter.scan(lambda p, f: True)[0]["events"]
    assert not [e for e in evs if e["kind"] == "user"]


def test_v20_inline_wal_rescan_sees_a_new_turn(oc_env, adapter, monkeypatch):
    db = make_v20_inline(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    writer = open_writer(db)
    try:
        first = adapter.scan(lambda p, f: True)
        n_before = len([e for e in first[0]["events"] if e["kind"] == "user"])
        append_message(writer, "ses_inline_000", "第二个问题")
        second = adapter.scan(lambda p, f: True)
        n_after = len([e for e in second[0]["events"] if e["kind"] == "user"])
        assert n_after == n_before + 1
        assert any("第二个问题" in (e.get("content") or "")
                   for e in second[0]["events"])
    finally:
        writer.close()


def test_v20_inline_scan_is_idempotent(oc_env, adapter, monkeypatch):
    db = make_v20_inline(oc_env / "opencode.db")
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(db))
    a = adapter.scan(lambda p, f: True)
    b = adapter.scan(lambda p, f: True)
    assert [(x["session"]["id"], len(x["events"])) for x in a] == \
           [(x["session"]["id"], len(x["events"])) for x in b]
    assert [e["seq"] for e in a[0]["events"]] == \
           [e["seq"] for e in b[0]["events"]]


def test_explicit_override_does_not_probe_the_default_database(oc_env,
                                                               monkeypatch):
    """An operator-named DB must not cause an unrequested default-HOME read."""
    from voyager.adapters import opencode as oc
    make_v2(oc_env / "opencode.db")              # the default the user did NOT name
    other = oc_env.parent / "named.db"
    make_v20_inline(other)
    monkeypatch.setenv("VOYAGER_OPENCODE_DB", str(other))
    candidates = oc._candidate_dbs()
    assert candidates and all("named.db" == p.name for p in candidates)
    assert oc_env / "opencode.db" not in candidates
    assert oc_env / "opencode.db" not in oc.OpencodeAdapter().discover()
