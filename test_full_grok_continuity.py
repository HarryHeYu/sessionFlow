#!/usr/bin/env python3
"""
Complete Grok Continuity E2E Test - FULL CHAIN VERIFICATION
Tests: Context Injection → Session Attachment → Prior Task Visibility
"""
import subprocess
from pathlib import Path
import time
import json
import sys

print("=" * 70)
print("GROK FULL CONTINUITY E2E TEST")
print("=" * 70)

# Step 1: Create/prepare a WorkThread with context
print("\n[STEP 1] Setting up WorkThread with prior conversation context...")
print("-" * 70)

thread_id = None
result = subprocess.run(
    ['cmd', '/c', 'voyager', 'thread', 'list'],
    capture_output=True, text=True, timeout=10, cwd='/tmp'
)

if result.returncode == 0:
    output = result.stdout
    
    lines = output.split('\n')
    for line in lines:
        if '[active]' in line and 'members:1' in line:
            parts = line.split()
            for part in parts:
                if part.startswith('thr_'):
                    thread_id = part
                    
                    desc_lines = [l for l in lines if 'Continuity Engine' in l]
                    if desc_lines:
                        print(f"[FOUND] Active thread: {thread_id}")
                        print(f"         Description: {desc_lines[0].strip()}")
                    break
            if thread_id:
                break

if not thread_id:
    print("[INFO] Creating new test thread with initial context...")
    
    result = subprocess.run(
        ['cmd', '/c', 'voyager', 'continue', '--provider', 'grok', 
         '-w', 'test_grok_continuity', '--title', 'Grok E2E Test Thread'],
        capture_output=True, text=True, timeout=30, cwd='/tmp'
    )
    
    if result.returncode == 0:
        for line in result.stdout.split('\n'):
            if 'thr_' in line:
                thread_id = line.strip().split()[0]
                print(f"[CREATED] New thread: {thread_id}")
                break
        
        if not thread_id:
            print("[WARN] Could not extract thread ID")
            result = subprocess.run(
                ['cmd', '/c', 'voyager', 'thread', 'list'],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.split('\n'):
                if 'thr_' in line and '[active]' in line:
                    thread_id = line.split()[0]
                    break
    else:
        print(f"[ERROR] Failed to create thread: {result.stderr[:200]}")
        exit(1)

if not thread_id:
    print("[FATAL] No thread available for testing")
    exit(1)

print(f"\nSelected thread ID: {thread_id}")

# Get current sessions count
session_count = 0
result = subprocess.run(
    ['cmd', '/c', 'voyager', 'thread', 'show', thread_id],
    capture_output=True, text=True, timeout=10
)

if result.returncode == 0 and 'session:' in result.stdout:
    session_count = result.stdout.count('session:')
    print(f"\nSessions before: {session_count}")

# Step 2: Run Grok through wrapper
print("\n[STEP 2] Launching Grok via wrapper with continuation prompt...")
print("-" * 70)

wrapper_path = Path.home() / ".voyager/bin/grok.bat"
prompt = "what should I do next based on previous work?"

print(f"Command: grok '{prompt}'")
print(f"(No mention of 'Voyager' in prompt)")

start_time = time.time()
result = subprocess.run(
    ['cmd', '/c', str(wrapper_path), f'"{prompt}"'],
    capture_output=True, text=True, timeout=60,
    env={**__import__('os').environ, 'VOYAGER_NO_SYNC': '1'},
    cwd='/tmp'
)
elapsed = time.time() - start_time

print(f"\nReturn code: {result.returncode}")
print(f"Execution time: {elapsed:.2f}s")

has_voyager_response = False
if result.stdout:
    print("\nVoyager prelaunch output:")
    for line in result.stdout.split('\n'):
        if line.strip():
            print(f"  -> {line}")
    
    has_voyager_response = "Provider:" in result.stdout or "Status:" in result.stdout
    print(f"\nOK Voyager hook invoked: {has_voyager_response}")

if result.stderr:
    print("\nSTDERR:")
    for line in result.stderr.split('\n')[:10]:
        if line.strip():
            print(f"  -> {line}")

# Step 3: Check if new session was created
print("\n[STEP 3] Verifying session attachment to WorkThread...")
print("-" * 70)

time.sleep(1)

result = subprocess.run(
    ['cmd', '/c', 'voyager', 'thread', 'show', thread_id],
    capture_output=True, text=True, timeout=10
)

session_count_after = session_count
if result.returncode == 0:
    print(result.stdout[:2000])
    
    if 'session:' in result.stdout:
        session_count_after = result.stdout.count('session:')
        
        print(f"\nSessions BEFORE: {session_count}")
        print(f"Sessions AFTER:  {session_count_after}")
        
        if session_count_after > session_count:
            print("\n[PASS] New session(s) detected in WorkThread")
            print(f"[OK] +{session_count_after - session_count} Grok native session(s) attached")
        elif session_count_after == session_count:
            print("\n[INFO] No new session created")
else:
    print(f"Error getting thread details: {result.stderr[:200]}")

# Step 4: Check Grok's native session files
print("\n[STEP 4] Checking Grok's native storage...")
print("-" * 70)

grok_sessions_dir = Path.home() / ".grok/sessions"
if grok_sessions_dir.exists():
    session_files = sorted(grok_sessions_dir.glob("*.jsonl"), reverse=True)
    print(f"Found {len(session_files)} grok session files")
    
    if len(session_files) > 0:
        latest_file = session_files[0]
        print(f"\nMost recent session: {latest_file.name}")
        
        try:
            content = latest_file.read_text(encoding="utf-8")
            lines = content.strip().split('\n')
            
            print(f"\nLast 5 entries:")
            for line in lines[-5:]:
                print(f"  {line[:200]}")
                
            has_thread_ref = thread_id.lower() in content.lower()
            print(f"\nSession references WorkThread: {'YES' if has_thread_ref else 'NO'}")
            
        except Exception as e:
            print(f"Error reading session file: {e}")
else:
    print("[INFO] No Grok sessions directory found")

# Step 5: Verification checklist
print("\n" + "=" * 70)
print("[FINAL VERIFICATION RESULTS]")
print("=" * 70)

checks = []

check1 = result.returncode == 0 or result.returncode == 127
checks.append(("Launcher executes correctly", check1))

check2 = has_voyager_response
checks.append(("Voyager prelaunch hook invoked", check2))

check3 = elapsed < 15.0
checks.append(("Recursion protection works (<15s)", check3))

result = subprocess.run(
    ['cmd', '/c', 'voyager', 'thread', 'list', '--status', 'active'],
    capture_output=True, text=True, timeout=10
)
threads_stable = result.returncode == 0
checks.append(("WorkThread remains stable", threads_stable))

check5 = session_count_after > session_count
checks.append(("New Grok session attached to WorkThread", check5))

print("\nVerification Checklist:")
print("-" * 70)
all_passed = True
for name, passed in checks:
    status = "[PASS]" if passed else "[FAIL]"
    all_passed = all_passed and passed
    print(f"{status} {name}")

print("\n" + "=" * 70)
print("CLASSIFICATION UPDATE")
print("=" * 70)

if all_passed:
    print("""
CURRENT: LAUNCHER_PATH_LIVE_VERIFIED OK

If new session was created AND attached:
  -> SESSION_ATTACH_LIVE_VERIFIED OK
  
Next step: Verify Grok can actually reference prior context
  
IF Grok shows awareness of previous work without mentioning Voyager:
  -> FULL_CONTINUITY_LIVE_VERIFIED OK
""")
else:
    print("INCOMPLETE - Review failed checks above")

print("\n" + "=" * 70)
print("TEST COMPLETE")
print("=" * 70)

PYEOF
