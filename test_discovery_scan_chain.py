#!/usr/bin/env python3
"""
Final E2E test proving discover() -> scan() chain works correctly.

This test proves:
1. VOYAGER_ZCODE_DB env var takes priority over system DB
2. scan() reads ONLY from discovered DBs (not hardcoded DB_PATH)  
3. Only sessions from custom DB are returned
"""
import tempfile, os, sys, sqlite3, json
from pathlib import Path

print("Testing: discover() -> scan() chain")
print("=" * 60)

with tempfile.TemporaryDirectory() as tmp:
    # Create custom ZCode database
    custom_db = Path(tmp) / "custom_zcode.db"
    con = sqlite3.connect(str(custom_db))
    
    # Complete schema matching production expectations
    con.execute("CREATE TABLE session (id TEXT, directory TEXT, title TEXT, parent_id TEXT, project_id INTEGER, time_created INTEGER, time_updated INTEGER, summary_additions BLOB, summary_deletions BLOB, summary_files BLOB)")
    con.execute("CREATE TABLE message (id INTEGER, session_id INTEGER, sequence INTEGER, time_created INTEGER, data TEXT)")
    con.execute("CREATE TABLE part (message_id INTEGER, sequence INTEGER, time_created INTEGER, data BLOB)")
    con.execute("CREATE TABLE tool_usage (tool_call_id TEXT, exit_code INTEGER, error_message TEXT, stdout_bytes BLOB, stderr_bytes BLOB, session_id TEXT)")
    con.execute("CREATE TABLE model_usage (model_id TEXT, provider_id TEXT, input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, session_id TEXT)")
    
    # Insert unique session that won't conflict with any system data
    UNIQUE_ID = "ZCODE_FIX_VERIFICATION_SESSION_999"
    con.execute("INSERT INTO session VALUES (?, '/verification/path', 'Verification Title', NULL, NULL, 99999, 88888, NULL, NULL, NULL)",
                (UNIQUE_ID,))
    con.execute("INSERT INTO message VALUES (1, ?, 1, 5000, ?)", 
                (UNIQUE_ID, json.dumps({"role": "user", "model": {"modelID": "verification-model"}})))
    con.execute("INSERT INTO part VALUES (1, 1, 5000, ?)",
                (json.dumps({"type": "text", "text": "This session proves discovery->scan works", "time": {"created": 5000}}),))
    con.commit()
    con.close()
    
    print(f"\n1. Created custom DB at: {custom_db.name}")
    print(f"   Contains session: {UNIQUE_ID}")
    
    # Set environment variable BEFORE importing adapter modules
    os.environ['VOYAGER_ZCODE_DB'] = str(custom_db)
    print(f"\n2. Set VOYAGER_ZCODE_DB={custom_db.name}")
    
    # Clear any cached voyager modules to force fresh import
    modules_to_remove = [k for k in list(sys.modules.keys()) if 'voyager' in k]
    for mod in modules_to_remove:
        del sys.modules[mod]
    
    # Import AFTER setting env var
    from voyager.adapters.zcode import ZCodeAdapter
    
    print(f"\n3. Testing adapter.discover()...")
    adapter = ZCodeAdapter()
    discovered = adapter.discover()
    
    assert len(discovered) == 1, f"FAIL: Expected 1 DB, got {len(discovered)}"
    assert str(discovered[0]) == str(custom_db), f"FAIL: Wrong DB discovered: {discovered[0]}"
    print(f"   OK - discover() found exactly 1 DB")
    print(f"   OK - Correctly identified custom DB (not system DB)")
    
    print(f"\n4. Testing adapter.scan()...")
    bundles = adapter.scan(lambda p, f: True)
    
    assert len(bundles) == 1, f"FAIL: Expected 1 bundle, got {len(bundles)}"
    print(f"   OK - scan() returned exactly 1 session bundle")
    
    session = bundles[0]['session']
    events = bundles[0]['events']
    
    assert session['id'] == f'zcode:{UNIQUE_ID}', f"FAIL: Wrong session ID: {session['id']}"
    print(f"   OK - Session ID matches: {session['id']}")
    
    assert session['title'] == 'Verification Title', f"FAIL: Wrong title: {session['title']}"
    print(f"   OK - Session title correct: {session['title']}")
    
    assert session['cwd'] == '/verification/path', f"FAIL: Wrong cwd: {session['cwd']}"
    print(f"   OK - Session CWD correct: {session['cwd']}")
    
    assert len(events) == 1, f"FAIL: Expected 1 event, got {len(events)}"
    print(f"   OK - Has correct number of events: {len(events)}")
    
    print("\n" + "=" * 60)
    print("SUCCESS!")
    print("=" * 60)
    print("\nThe ZCode discovery -> scan chain works correctly:")
    print("  - discover() respects VOYAGER_ZCODE_DB override")
    print("  - scan() iterates over self.discover() results")
    print("  - NO hardcoded DB_PATH used in normal operation")
    print("  - Only sessions from discovered/custom DB returned")
    print("\nThis is the foundation required for Phase 8.")
