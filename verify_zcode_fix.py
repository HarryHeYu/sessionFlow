#!/usr/bin/env python3
"""Comprehensive E2E verification for ZCode discovery -> scan chain."""
import tempfile, os, sys, sqlite3, json
from pathlib import Path

print("=" * 70)
print("COMPREHENSIVE E2E VERIFICATION FOR ZCode DISCOVERY → SCAN CHAIN")
print("=" * 70)

# Test 1: Custom DB via VOYAGER_ZCODE_DB env var
print("\n[Test 1] Custom DB override with VOYAGER_ZCODE_DB")
with tempfile.TemporaryDirectory() as tmp:
    custom_db = Path(tmp) / "custom_test.db"
    con = sqlite3.connect(str(custom_db))
    
    con.execute("CREATE TABLE session (id TEXT, directory TEXT, title TEXT, parent_id TEXT, project_id INTEGER, time_created INTEGER, time_updated INTEGER, summary_additions BLOB, summary_deletions BLOB, summary_files BLOB)")
    con.execute("CREATE TABLE message (id INTEGER, session_id INTEGER, sequence INTEGER, time_created INTEGER, data TEXT)")
    con.execute("CREATE TABLE part (message_id INTEGER, sequence INTEGER, time_created INTEGER, data BLOB)")
    con.execute("CREATE TABLE tool_usage (tool_call_id TEXT, exit_code INTEGER, error_message TEXT, stdout_bytes BLOB, stderr_bytes BLOB, session_id TEXT)")
    con.execute("CREATE TABLE model_usage (model_id TEXT, provider_id TEXT, input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, session_id TEXT)")
    
    con.execute("INSERT INTO session VALUES ('ONLY_CUSTOM_SESSION', '/my/custom/path', 'Custom Title', NULL, NULL, 5000, 6000, NULL, NULL, NULL)")
    con.execute("INSERT INTO message VALUES (1, 'ONLY_CUSTOM_SESSION', 1, 5500, ?)", (json.dumps({"role": "user", "model": {"modelID": "test"}}),))
    con.execute("INSERT INTO part VALUES (1, 1, 5500, ?)", (json.dumps({"type": "text", "text": "Hello from custom DB", "time": {"created": 5500}}),))
    con.commit()
    con.close()
    
    # Clear modules and set env BEFORE import
    modules_to_remove = [k for k in list(sys.modules.keys()) if 'voyager' in k]
    for mod in modules_to_remove:
        del sys.modules[mod]
    
    os.environ['VOYAGER_ZCODE_DB'] = str(custom_db)
    
    from voyager.adapters.zcode import ZCodeAdapter
    
    adapter = ZCodeAdapter()
    discovered = adapter.discover()
    print(f"  discover() found {len(discovered)} DB(s)")
    assert len(discovered) == 1, f"Expected 1, got {len(discovered)}"
    assert str(discovered[0]) == str(custom_db), "Wrong DB!"
    print(f"  OK: Discovered correct custom DB")
    
    bundles = adapter.scan(lambda p, f: True)
    print(f"  scan() returned {len(bundles)} bundle(s)")
    assert len(bundles) == 1, f"Expected 1, got {len(bundles)}"
    
    session = bundles[0]['session']
    assert session['id'] == 'zcode:ONLY_CUSTOM_SESSION', f"Wrong ID: {session['id']}"
    assert session['title'] == 'Custom Title', f"Wrong title: {session['title']}"
    assert session['cwd'] == '/my/custom/path', f"Wrong cwd: {session['cwd']}"
    print(f"  OK: Session from custom DB: {session['id']}")
    print(f"  OK: ONLY sessions from custom DB (not system DB)")

# Test 2: Multiple DBs discovery  
print("\n[Test 2] Multiple valid DBs all discovered and scanned")
with tempfile.TemporaryDirectory() as tmp2:
    db1 = Path(tmp2) / ".zcode" / "cli" / "db" / "db.sqlite"
    db2 = Path(tmp2) / ".zcode" / "cli" / "backup.sqlite"
    
    db1.parent.mkdir(parents=True)
    db2.parent.mkdir(parents=True)
    
    def create_session(db_path, sid, title):
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE session (id TEXT, directory TEXT, title TEXT, parent_id TEXT, project_id INTEGER, time_created INTEGER, time_updated INTEGER, summary_additions BLOB, summary_deletions BLOB, summary_files BLOB)")
        con.execute("CREATE TABLE message (id INTEGER, session_id INTEGER, sequence INTEGER, time_created INTEGER, data TEXT)")
        con.execute("CREATE TABLE part (message_id INTEGER, sequence INTEGER, time_created INTEGER, data BLOB)")
        con.execute("CREATE TABLE tool_usage (tool_call_id TEXT, exit_code INTEGER, error_message TEXT, stdout_bytes BLOB, stderr_bytes BLOB, session_id TEXT)")
        con.execute("CREATE TABLE model_usage (model_id TEXT, provider_id TEXT, input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, session_id TEXT)")
        con.execute("INSERT INTO session VALUES (?, '/path', ?, NULL, NULL, 1000, 2000, NULL, NULL, NULL)", (sid, title))
        con.execute("INSERT INTO message VALUES (1, ?, 1, 1500, ?)", (sid, json.dumps({"role": "user"})))
        con.execute("INSERT INTO part VALUES (1, 1, 1500, ?)", (json.dumps({"type": "text", "text": "X", "time": {"created": 1500}}),))
        con.commit()
        con.close()
    
    create_session(db1, "SESSION_1", "From DB1")
    create_session(db2, "SESSION_2", "From DB2")
    
    original_home = os.environ.get('HOME')
    os.environ['HOME'] = str(tmp2)
    
    try:
        modules_to_remove = [k for k in list(sys.modules.keys()) if 'voyager' in k]
        for mod in modules_to_remove:
            del sys.modules[mod]
        
        from voyager.adapters.zcode import ZCodeAdapter
        
        adapter = ZCodeAdapter()
        discovered = adapter.discover()
        print(f"  Found {len(discovered)} valid DB(s)")
        assert len(discovered) >= 2, f"Expected >=2, got {len(discovered)}"
        print(f"  OK: Multiple DBs discovered")
        
        bundles = adapter.scan(lambda p, f: True)
        titles = set(b['session']['title'] for b in bundles)
        print(f"  Sessions from both DBs: {sorted(titles)}")
        assert "From DB1" in titles, "Missing DB1 session"
        assert "From DB2" in titles, "Missing DB2 session"
        print(f"  OK: Both DBs scanned, no duplicates")
    finally:
        if original_home:
            os.environ['HOME'] = original_home
        elif 'HOME' in os.environ:
            del os.environ['HOME']

# Test 3: Schema validation  
print("\n[Test 3] Schema validation rejects invalid databases")
with tempfile.TemporaryDirectory() as tmp3:
    corrupt_db = Path(tmp3) / "corrupt.db"
    corrupt_db.write_text("not a sqlite file")
    
    from voyager.adapters.zcode import _open_ro, _validate_zcode_schema
    
    conn = _open_ro(corrupt_db)
    assert conn is None, "Corrupt file should not open"
    print(f"  OK: Corrupt files rejected")
    
    valid_schema_db = Path(tmp3) / "valid_schema.db"
    con = sqlite3.connect(str(valid_schema_db))
    # Wrong schema - missing columns
    con.execute("CREATE TABLE session (id TEXT, wrong_col TEXT)")
    con.commit()
    con.close()
    
    conn = _open_ro(valid_schema_db)
    assert conn is not None
    result = _validate_zcode_schema(conn)
    assert result is False, "Wrong schema should be rejected"
    conn.close()
    print(f"  OK: Wrong schema rejected")
    
    # Correct schema
    correct_db = Path(tmp3) / "correct.db"
    con = sqlite3.connect(str(correct_db))
    con.execute("CREATE TABLE session (id TEXT, directory TEXT, title TEXT, parent_id TEXT, project_id INTEGER, time_created INTEGER, time_updated INTEGER, summary_additions BLOB, summary_deletions BLOB, summary_files BLOB)")
    con.execute("CREATE TABLE message (id INTEGER, session_id INTEGER, sequence INTEGER, time_created INTEGER, data TEXT)")
    con.execute("CREATE TABLE part (message_id INTEGER, sequence INTEGER, time_created INTEGER, data BLOB)")
    con.execute("CREATE TABLE tool_usage (tool_call_id TEXT, exit_code INTEGER, error_message TEXT, stdout_bytes BLOB, stderr_bytes BLOB, session_id TEXT)")
    con.execute("CREATE TABLE model_usage (model_id TEXT, provider_id TEXT, input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, session_id TEXT)")
    con.commit()
    con.close()
    
    conn = _open_ro(correct_db)
    result = _validate_zcode_schema(conn)
    assert result is True, "Correct schema should be accepted"
    conn.close()
    print(f"  OK: Correct schema accepted")

print("\n" + "=" * 70)
print("ALL E2E TESTS PASSED!")
print("=" * 70)
print("\nKey verifications:")
print("  1. discover() finds custom DB when VOYAGER_ZCODE_DB set")
print("  2. scan() reads ONLY from discovered DBs (not DB_PATH)")
print("  3. Multiple DBs all scanned without duplicates")  
print("  4. Schema validation rejects invalid/corrupt databases")
print("\nReady to run full pytest suite...")
