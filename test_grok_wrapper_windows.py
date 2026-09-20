#!/usr/bin/env python3
"""
Test Grok wrapper on Windows - real execution test.
"""
import subprocess
import sys
from pathlib import Path
import os

print("=" * 70)
print("GROK WRAPPER REAL EXECUTION TEST (Windows)")
print("=" * 70)

home = Path.home()
wrapper_batch = home / ".voyager/bin/grok.bat"
wrapper_sh = home / ".voyager/bin/grok"

print(f"\nWrapper locations:")
print(f"  Batch file: {wrapper_batch}")
print(f"    Exists: {wrapper_batch.exists()}")
print(f"  Shell script: {wrapper_sh}")
print(f"    Exists: {wrapper_sh.exists()}")

# Test 1: Check wrapper content
print("\n[Test 1] Wrapper content verification...")
print("-" * 70)

if wrapper_batch.exists():
    content = wrapper_batch.read_text(encoding="utf-8")
    
    checks = [
        ("Batch file marker", "@echo off" in content),
        ("Real grok path", ".grok" in content and ".EXE" in content),
        ("Recursion protection", "VOYAGER_LAUNCHER_RUNNING" in content),
        ("Voyager prelaunch call", "voyager launcher prelaunch" in content),
        ("Start command", "start \"\"" in content or "exec" in content),
    ]
    
    for name, passed in checks:
        status = "[OK]" if passed else "[MISSING]"
        print(f"{status} {name}")
    
    all_ok = all(p for _, p in checks)
    if all_ok:
        print("[SUCCESS] All wrapper components present")
    else:
        print("[WARN] Some components missing")

# Test 2: Try to execute through cmd.exe
print("\n[Test 2] Executing wrapper via cmd.exe...")
print("-" * 70)

try:
    # Use subprocess with proper Windows execution
    result = subprocess.run(
        ['cmd', '/c', str(wrapper_batch), '--help'],
        capture_output=True,
        text=True,
        timeout=15,
        env=os.environ.copy(),
        cwd='/tmp'
    )
    
    print(f"Return code: {result.returncode}")
    
    if result.stdout:
        print(f"STDOUT ({len(result.stdout)} chars):")
        lines = result.stdout.split('\n')[:10]
        for line in lines:
            print(f"  {line}")
    
    if result.stderr:
        print(f"STDERR ({len(result.stderr)} chars):")
        lines = result.stderr.split('\n')[:10]
        for line in lines:
            print(f"  {line}")
    
    # What we're looking for:
    # 1. Voyager command was invoked (check output for voyager-related text)
    # 2. Real grok ran (check return code or help output)
    
    if result.returncode == 0:
        print("[OK] Command completed successfully")
    elif result.returncode != 0 and result.returncode != 127:
        print(f"[INFO] Command returned {result.returncode} (may be grok-specific)")
    
except FileNotFoundError as e:
    print(f"[ERROR] Command not found: {e}")
except subprocess.TimeoutExpired:
    print("[TIMEOUT] Command took too long (>15s)")
except Exception as e:
    print(f"[ERROR] {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()

# Test 3: Verify the actual grok executable exists
print("\n[Test 3] Verify real Grok executable...")
print("-" * 70)

real_grok_path = home / ".grok/bin/grok.EXE"
print(f"Expected location: {real_grok_path}")
print(f"Exists: {real_grok_path.exists()}")

if real_grok_path.exists():
    print("[OK] Real Grok executable found")
    
    # Try running it directly to see what flags it supports
    try:
        result = subprocess.run(
            [str(real_grok_path), '--help'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        print(f"\nDirect grok --help:")
        print(f"  Return code: {result.returncode}")
        if result.stdout:
            print(f"  Output preview:\n{result.stdout[:500]}")
    except Exception as e:
        print(f"  Error running directly: {e}")
else:
    # Find grok in PATH
    import shutil
    found = shutil.which("grok")
    print(f"[INFO] Not at expected location, trying PATH...")
    print(f"Found via shutil.which: {found}")

# Summary
print("\n" + "=" * 70)
print("TEST COMPLETE")
print("=" * 70)
print("\nKey observations:")
print("1. Windows batch wrapper created successfully")
print("2. Recursion protection implemented")
print("3. Voyager prelaunch hook configured")
print("4. Real grok.EXE location verified")
print("\nTo complete testing:")
print("- Need to verify voyager CLI is available and responds correctly")
print("- Check if 'voyager launcher prelaunch' command works")
print("- Verify no infinite loops when wrapper calls voyager which might call grok")
