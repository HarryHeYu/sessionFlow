#!/usr/bin/env python3
"""Claude SessionStart Hook Testing.

Test Plan:
1. Verify Claude installation
2. Check if actual installed version has session-start hooks (NOT from old docs)
3. If hooks exist, install integration and verify invocation
4. If hooks don't exist, classify as FIRST_TURN_ZERO_TOUCH (instruction-based)
"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import os

home = Path.home()
print("=" * 60)
print("CLAUDE SESSIONSTART HOOK VERIFICATION")
print("=" * 60)

# Step 1: Check Claude existence
claude_exe = shutil.which("claude-code") or shutil.which("claude")
if not claude_exe:
    print("\n[STATUS] Claude NOT FOUND - SKIP FULL TESTS")
    print(f"\nClassification: BEST_EFFORT (no launcher)")
    print("\nRecommendation: Skip zero-touch testing until Claude installed")
    sys.exit(0)

print(f"\n[CHECK 1/5] Claude executable found: {claude_exe}")

# Step 2: Inspect settings.json for hook configuration
settings_file = home / ".claude/settings.json"
has_hook_config = False
hook_details = []

if settings_file.exists():
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        
        # Look for any hook-related configuration
        for key in settings.keys():
            if any(x in key.lower() for x in ["hook", "on_", "session", "start", "spawn"]):
                has_hook_config = True
                hook_details.append(f"  - {key}: {str(settings[key])[:100]}")
    
    except Exception as e:
        print(f"[WARN] Could not parse settings.json: {e}")
else:
    print("[CHECK 2/5] settings.json NOT EXISTS")

if has_hook_config:
    print(f"\n[CHECK 2/5] Hook CONFIGURATION FOUND:")
    for h in hook_details:
        print(h)
else:
    print("\n[CHECK 2/5] NO native hook configuration in settings.json")

# Step 3: Check for Voyager wrapper script
voyager_hook = home / ".claude/voyager_session_start.sh"
wrapper_exists = voyager_hook.exists()

print(f"\n[CHECK 3/5] Voyager wrapper script: {'EXISTS' if wrapper_exists else 'MISSING'}")
if wrapper_exists:
    content = voyager_hook.read_text(encoding="utf-8")
    print(f"Content preview: {content[:200]}...")

# Step 4: Test hook invocation manually
print("\n[CHECK 4/5] Testing hook invocation...")

# Create a test directory
test_dir = Path("/tmp/claude-test-$$")
test_dir.mkdir(exist_ok=True)
os.chdir(test_dir)

try:
    result = subprocess.run(
        ["bash", str(voyager_hook)] if wrapper_exists else ["echo", "No hook"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "VOYAGER_HOOK_CWD": str(test_dir)}
    )
    
    print(f"Return code: {result.returncode}")
    if result.stdout:
        print(f"Stdout: {result.stdout[:300]}")
    if result.stderr:
        print(f"Stderr: {result.stderr[:300]}")
    
    hook_invoked = result.returncode == 0
    
except Exception as e:
    print(f"[ERROR] Hook invocation failed: {e}")
    hook_invoked = False

# Step 5: Attempt actual Claude launch via wrapper
print("\n[CHECK 5/5] Testing Claude launch with Voyager context injection...")

# First check if there's an active WorkThread
from voyager.core.threads import list_threads
try:
    threads = list_threads()
    active_threads = [t for t in threads if t.get("active", False)]
    
    if active_threads:
        print(f"Active threads: {len(active_threads)}")
        print("Would test Claude launch here, but requires TTY input")
        claude_launchable = "PENDING_TTY_TEST"
    else:
        print("No active threads available")
        claude_launchable = "NO_ACTIVE_THREAD"
        
except Exception as e:
    print(f"[ERROR] Thread listing failed: {e}")
    claude_launchable = f"ERROR: {e}"

# Cleanup
os.chdir(Path.cwd())

# Final verdict
print("\n" + "=" * 60)
print("FINAL VERDICT")
print("=" * 60)

classification = "BEST_EFFORT"
reasons = []

if has_hook_config:
    classification = "SESSION_START_ZERO_TOUCH"
    reasons.append("Native session-start hooks verified in settings.json")
elif wrapper_exists and hook_invoked:
    classification = "LAUNCHER_ZERO_TOUCH" 
    reasons.append("Wrapper script exists and invokes correctly")
else:
    reasons.append("No native hooks detected; will rely on CLAUDE.md instruction pattern")

print(f"\nClassification: {classification}")
print("Reasons:")
for r in reasons:
    print(f"  • {r}")

print(f"\nVerification Level: UNVERIFIED_CLAIM")
print("Note: Full E2E requires TTY launch which cannot be tested in script environment")

