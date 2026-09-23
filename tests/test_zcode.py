"""ZCode adapter regression test.

Fixture: tests/fixtures/zcode/seed.sql builds a synthetic db.sqlite whose DDL
mirrors the real ZCode CLI schema (session / message / part / model_usage /
tool_usage, constraints included). Also asserts the tool_usage exit-code
enrichment and the model_usage aggregation the adapter depends on.
"""

from __future__ import annotations


def test_zcode_scan(adapter_of, patch_paths, zcode_fixture):
    # Use env var override for discovery-based scan (new architecture)
    import os
    import sys
    
    # Temporarily set env var before importing adapter
    original_env = os.environ.get('VOYAGER_ZCODE_DB')
    os.environ['VOYAGER_ZCODE_DB'] = str(zcode_fixture)
    
    try:
        # Clear cached modules to force fresh import
        modules_to_remove = [k for k in list(sys.modules.keys()) if 'voyager' in k]
        for mod in modules_to_remove:
            del sys.modules[mod]
        
        ad = adapter_of("zcode")
        bundles = ad.scan(lambda p, f: True)
        
        assert len(bundles) == 1
        s, evs = bundles[0]["session"], bundles[0]["events"]
        assert s["id"] == "zcode:sess_z1"
        assert s["title"] == "zcode title"
        assert s["cwd"] == "E:/proj/demo"
        assert s["model"] == "p:m"
        kinds = [e["kind"] for e in evs]
        assert kinds.count("user") == 1 and kinds.count("reasoning") == 1
        assert kinds.count("tool_call") == 1
        tc = next(e for e in evs if e["kind"] == "tool_call")
        assert tc["exit_code"] == 0     # enriched from tool_usage
        assert s["metadata"]["summary"] == {"additions": 12, "deletions": 3, "files": 4}
        assert s["metadata"]["usage_totals"]["by_model"]["anthropic:claude-sonnet"] == {
            "input": 200, "output": 60, "reasoning": 0,
            "cache_read": 20, "cache_write": 0,
        }
    finally:
        # Restore original env
        if original_env:
            os.environ['VOYAGER_ZCODE_DB'] = original_env
        elif 'VOYAGER_ZCODE_DB' in os.environ:
            del os.environ['VOYAGER_ZCODE_DB']


def test_zcode_db_env_override_expands_tilde(monkeypatch, tmp_path):
    """`VOYAGER_ZCODE_DB=~/x` must resolve against HOME, not the cwd.

    Left literal, `Path("~")` is *relative*, so the override pointed at a
    directory inside whatever the process working directory happened to be.
    """
    from voyager.adapters import zcode

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("VOYAGER_ZCODE_DB", "~/z/db.sqlite")

    class _Con:
        def close(self):
            pass

    monkeypatch.setattr(zcode, "_open_ro", lambda p: _Con())
    monkeypatch.setattr(zcode, "_validate_zcode_schema", lambda con: True)

    assert zcode.discover_zcode_db() == [tmp_path / "z/db.sqlite"]


# --- promoted from the retired repo-root script `verify_zcode_fix.py` --------
#
# That file was script-style: top-level code, prints, asserts, and not a single
# `def test_`, so pytest never collected it and CI never ran it.  Two of its
# three cases existed nowhere else; they live here now.  (Its third case -- the
# VOYAGER_ZCODE_DB override -- is already covered by `test_zcode_scan` above.)

_ZCODE_DDL = (
    "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, parent_id TEXT,"
    " project_id INTEGER, time_created INTEGER, time_updated INTEGER,"
    " summary_additions BLOB, summary_deletions BLOB, summary_files BLOB)",
    "CREATE TABLE message (id INTEGER, session_id INTEGER, sequence INTEGER,"
    " time_created INTEGER, data TEXT)",
    "CREATE TABLE part (message_id INTEGER, sequence INTEGER,"
    " time_created INTEGER, data BLOB)",
    "CREATE TABLE tool_usage (tool_call_id TEXT, exit_code INTEGER,"
    " error_message TEXT, stdout_bytes BLOB, stderr_bytes BLOB, session_id TEXT)",
    "CREATE TABLE model_usage (model_id TEXT, provider_id TEXT,"
    " input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,"
    " cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,"
    " session_id TEXT)",
)


def _make_zcode_db(path, sid, title):
    import json
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    for ddl in _ZCODE_DDL:
        con.execute(ddl)
    con.execute(
        "INSERT INTO session VALUES (?, '/path', ?, NULL, NULL, 1000, 2000,"
        " NULL, NULL, NULL)", (sid, title))
    con.execute("INSERT INTO message VALUES (1, ?, 1, 1500, ?)",
                (sid, json.dumps({"role": "user"})))
    con.execute("INSERT INTO part VALUES (1, 1, 1500, ?)",
                (json.dumps({"type": "text", "text": "X",
                             "time": {"created": 1500}}),))
    con.commit()
    con.close()


def test_zcode_discovers_and_scans_every_valid_db(monkeypatch, tmp_path):
    """Every database under HOME/.zcode is discovered and scanned, not just one.

    `discover_zcode_db()` resolves HOME at call time and then rglobs `*.sqlite`,
    so pointing HOME at a tmp tree is sufficient -- no module reload needed.
    """
    from voyager.adapters.zcode import ZCodeAdapter

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("VOYAGER_ZCODE_DB", raising=False)

    _make_zcode_db(tmp_path / ".zcode/cli/db/db.sqlite", "SESSION_1", "From DB1")
    _make_zcode_db(tmp_path / ".zcode/cli/backup.sqlite", "SESSION_2", "From DB2")

    adapter = ZCodeAdapter()
    assert len(adapter.discover()) >= 2

    titles = {b["session"]["title"] for b in adapter.scan(lambda p, f: True)}
    assert {"From DB1", "From DB2"} <= titles


def test_zcode_schema_validation_rejects_bad_databases(tmp_path):
    """Wrong schema rejected; correct schema accepted."""
    import sqlite3

    from voyager.adapters.zcode import _open_ro, _validate_zcode_schema

    wrong = tmp_path / "wrong.db"
    con = sqlite3.connect(str(wrong))
    con.execute("CREATE TABLE session (id TEXT, wrong_col TEXT)")
    con.commit()
    con.close()
    conn = _open_ro(wrong)
    assert conn is not None
    assert _validate_zcode_schema(conn) is False
    conn.close()

    good = tmp_path / "good.db"
    _make_zcode_db(good, "S", "T")
    conn = _open_ro(good)
    assert _validate_zcode_schema(conn) is True
    conn.close()


def test_zcode_ignores_a_file_that_is_not_a_database(monkeypatch, tmp_path):
    """A non-SQLite file under HOME/.zcode must not be discovered.

    `_open_ro` alone does not catch this: `sqlite3.connect()` is lazy and hands
    back a connection without reading the file, so the rejection actually
    happens in `_validate_zcode_schema`, whose first query raises
    `file is not a database`.

    Worth spelling out because the retired `verify_zcode_fix.py` asserted
    `_open_ro(corrupt) is None` -- which cannot hold.  That script could never
    have run to completion, which is a second reason to retire it rather than
    trust it as a record.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("VOYAGER_ZCODE_DB", raising=False)

    corrupt = tmp_path / ".zcode" / "corrupt.sqlite"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("not a sqlite file", encoding="utf-8")

    from voyager.adapters.zcode import ZCodeAdapter

    assert ZCodeAdapter().discover() == []
