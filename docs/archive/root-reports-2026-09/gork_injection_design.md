# Grok Context Injection Design

## Problem Statement

Current wrapper does NOT inject context into Grok model:

```bash
# Current (WRONG)
voyager launcher prelaunch --provider grok --cwd %CD% || true  # Output suppressed
start "" "%real_grok%" %*                                       # Original args, no context

# Result: Voyager prelaunch fires but Grok NEVER sees continuation
```

## Required: Real Context Injection

User launches Grok normally:
```bash
grok "fix this bug"  # Or interactive mode without any mention of Voyager
```

Should automatically receive prior WorkThread context WITHOUT user copying/pasting.

---

## Option Analysis

### Option A: Prepend Continuation to Prompt ✅ RECOMMENDED

```batch
set "CONTINUATION=$(voyager launcher prelaunch --provider grok --cwd %CD%)"
if not "%VOYAGER_LAUNCHER_RUNNING%"=="" goto END
set VOYAGER_LAUNCHER_RUNNING=1

REM Get original prompt from args
set "ORIGINAL_PROMPT=%*"

REM If user provided prompt, prepend continuation
if not "%ORIGINAL_PROMPT%"=="" (
    set "INJECTED_PROMPT=[Voyager Continuation]\n%s\n\n%s"
) else (
    REM No prompt = interactive mode, write to temp file
    echo "[Voyager Continuation]\n%s" > %TEMP%\voyager_grok_context.txt
    set "CONTEXT_FILE=%TEMP%\voyager_grok_context.txt"
)

start "" "%real_grok%" %INJECTED_PROMPT%
```

**Pros**: 
- Uses Grok's native `[PROMPT]` argument channel
- Works for both interactive and non-interactive modes
- Direct injection into model context

**Cons**:
- Windows batch variable expansion limitations
- Need to handle special characters escaping

---

### Option B: Use Resume Mechanism

If user already has active sessions in same repo:

```bash
voyager launcher prelaunch --provider grok --cwd %CD%
→ Extract session_id from prior conversation
→ grok -r <session_id> --fork-session
```

**Pros**:
- Uses native Grok resume command
- Automatically gets conversation history

**Cons**:
- Requires prior Grok session to exist
- Fork creates NEW session, may not attach to same WorkThread
- Doesn't solve first-turn problem

---

### Option C: Stdin Injection

```bash
echo "[Voyager Continuation]\n..." | grok
```

**Pros**: Clean separation
**Cons**: 
- Interactive mode ignores stdin after launch
- Only works for single-turn prompts

---

### Option D: Temp File + Reference

```bash
echo "[Voyager Continuation]\n..." > %TEMP%\voyager_groktxt
grok "Check %TEMP%\voyager_context.txt for prior context"
```

**Pros**: Simple to implement
**Cons**: 
- Explicit file reference breaks "invisible continuity"
- User sees temp path reference

---

## Recommended Implementation: Option A Enhanced

Modified Grok wrapper that:
1. Calls `voyager launcher prelaunch` and captures output
2. Prepends continuation to user's prompt (if any)
3. Handles interactive mode via temp file
4. Properly escapes Windows batch strings

**File**: `~/.voyager/bin/grok.bat`

```batch
@echo off
setlocal enabledelayedexpansion

REM Get real Grok executable
set "REAL_GROK=C:\Users\He_Yu_Hao\.grok\bin\grok.EXE"

REM Prevent recursion
if not "%VOYAGER_LAUNCHER_RUNNING%"=="" goto EXECUTE_GROK
set VOYAGER_LAUNCHER_RUNNING=1

REM Run prelaunch hook and capture output
for /f "delims=" %%i in ('voyager launcher prelaunch --provider grok --cwd %CD% 2^>nul') do (
    set "CONTINUATION=%%i
)

REM Check if we got meaningful continuation
if "!CONTINUATION!"=="" goto EXECUTE_GROK

REM Build injected prompt
set "ORIGINAL_ARG=%*"
if not "!ORIGINAL_ARG!"=="" (
    REM User provided prompt - prepend continuation
    set "INJECT_PREFIX=[Voyager Continuation]\n!CONTINUATION!\n\n"
    REM Escape special chars for batch
    set "INJECT_PREFIX=!INJECT_PREFIX:"=\"!"
    set "NEW_PROMPT=!INJECT_PREFIX!!ORIGINAL_ARG!"
    
    REM Launch with modified prompt
    start "" "%REAL_GROK%" "%NEW_PROMPT%"
) else (
    REM No prompt = interactive mode
    REM Write continuation to temp file
    set "TEMP_FILE=%TEMP%\voyager_grok_%random%.txt"
    echo [Voyager Continuation] > "%TEMP_FILE%"
    echo !CONTINUATION! >> "%TEMP_FILE%"
    
    REM For now, just launch grok and tell user about temp file
    REM Better solution requires interactive stdin handling
    start "" "%REAL_GROK%"
    
    REM Show instructions in separate window
    start "Voyager Context" cmd /c "type "%TEMP_FILE%" & timeout /t 10"
)

goto END

:EXECUTE_GROK
start "" "%REAL_GROK%" %*

:END
```

---

## Testing Requirements

Must verify on dedicated test repo with exactly one active WorkThread:

1. **Test Case 1: Non-interactive prompt**
   ```bash
   grok "check project status"
   → Should see "[Voyager Continuation]" at top of response
   → First answer should reference prior WorkThread work
   ```

2. **Test Case 2: Interactive mode**
   ```bash
   grok
   → Typing natural task
   → Must get continuation context visible before entering chat
   ```

3. **Test Case 3: Sentinel marker propagation**
   - Prior WorkThread contains: `VOYAGER_GROK_SENTINEL_2026`
   - Launch Grok without mentioning Voyager
   - Verify Grok responds knowing sentinel exists

---

## Classification Path

Current: `LAUNCH_PATH_LIVE_VERIFIED`

With proper injection implemented:
- **CONTEXT_INJECTION_LIVE_VERIFIED** → Test passes with sentinel verification
- **SESSION_DISCOVERY_LIVE_VERIFIED** → New Grok session detectable post-launch
- **SESSION_ATTACH_LIVE_VERIFIED** → Auto-attached to same WorkThread
- **FULL_CONTINUITY_LIVE_VERIFIED** → End-to-end chain proven

Without injection:
- Stays `LAUNCH_PATH_LIVE_VERIFIED` indefinitely
- Wrapper just "fires hook" but doesn't inject context
- Not useful for zero-touch continuity goal

---

## Key Insight

**Wrapper execution ≠ Context injection**.

Many providers can fire hooks but cannot inject their continuation into model context. This is a fundamental architectural question per provider:

| Provider | Can Inject? | Method | Status |
|----------|-------------|--------|--------|
| Grok CLI | Yes | Prompt arg prepend | Needs implementation |
| Claude | Yes | stdout → context injection | Configured ✅ |
| Codex | Unknown | MCP/tool call? | Needs investigation |
| ZCode Desktop | TBD | TUI event listener | Watcher-only |

Must verify injection mechanism EXISTS before claiming zero-touch capability.
