# Provider Dogfood Verification State

> ⚠️ **Superseded snapshot — not current status.** A 2026-09-21 verification
> record. Two things in it are now wrong: (1) the hook wrapper it reports at
> `~/.claude/voyager_session_start.sh` is gone — the entrypoint is
> `claude_session_start.py` and the hook is written to `settings.json` with the
> real nested schema; (2) the provider test scripts it references
> (`test_full_grok_continuity.py`, etc.) were deleted from the repo root. Current
> state: **Claude Code = `H`** (registered, trigger unverified); **all other
> providers = `N`**. See [`CHANGELOG.md`](CHANGELOG.md).

**Date**: 2026-09-21  
**Status**: Ongoing investigation, evidence-based classifications

---

## Executive Summary

Completed evidence audit of 3 major providers (Grok, Claude, ZCode). All classified at **LAUNCH/HOOK_HANDLER_LIVE_VERIFIED** level with clear path to full zero-touch continuity pending real lifecycle tests.

**Key insight**: Manual hook invocation ≠ automatic session-start trigger. Must distinguish between "handler exists" and "platform auto-invokes it".

---

## Verification Level Framework

| Level | Definition | Requirement |
|-------|-----------|-------------|
| SUPPORTED | Provider surface exists | Executable/config found |
| CONFIGURED | Integration files deployed | Wrapper/hook scripts present |
| UNIT_VERIFIED | Basic functionality works | Tests pass in isolation |
| HOOK/LAUNCH_PATH_LIVE_VERIFIED | Execution path tested | Can invoke handler manually |
| CONTEXT_INJECTION_LIVE_VERIFIED | Content reaches provider | Model sees continuation bundle |
| SESSION_DISCOVERY_LIVE_VERIFIED | New sessions detectable | Adapter discovers post-launch |
| SESSION_ATTACH_LIVE_VERIFIED | Auto-attached to WorkThread | Continuity maintained |
| FULL_CONTINUITY_LIVE_VERIFIED | End-to-end chain proven | User mentions nothing → system acts |

**Progression rule**: Cannot skip levels. Each requires specific evidence.

---

## Current Classifications

### Grok CLI: LAUNCH_PATH_LIVE_VERIFIED

```text
SUPPORTED:                    ✅
CONFIGURED:                   ✅
UNIT_VERIFIED:                ✅
LAUNCHER_ZERO_TOUCH:          ✅ (opt-in wrapper strategy)
LAUNCH_PATH_LIVE_VERIFIED:    ✅
CONTEXT_INJECTION_LIVE_VERIFIED: ❓ channel unknown
SESSION_DISCOVERY_LIVE_VERIFIED: ❓ not tested with real prompt
FULL_CONTINUITY_LIVE_VERIFIED: ❌
```

**Evidence**:
- Wrapper script installed at `~/.voyager/bin/grok.bat`
- Recursion protection via env var
- Pre-launch hook fires on invocation
- Output captured and parsed correctly

**Limitations**:
- Test coverage insufficient: `--help` ≠ conversation lifecycle
- No proof of actual session creation/persistence
- Unknown injection channel (how does prelaunch output reach model?)
- Requires dedicated test repo with exactly-one-active-thread

**Next tests required**:
1. Phase 1: Real prompt → check session directory persistence
2. Phase 2: Context marker propagation through chain
3. Identify injection mechanism (CLI arg/stdin/file/temp)

**Verdict document**: `grok_continuity_verdict.md`

---

### Claude Code: HOOK_HANDLER_LIVE_VERIFIED

```text
SUPPORTED:                  ✅
CONFIGURED:                 ✅
UNIT_VERIFIED:              ✅
HOOK_HANDLER_LIVE_VERIFIED: ✅
SESSION_START_TRIGGER_LIVE_VERIFIED: ❌ (no auto-trigger proven)
CONTEXT_INJECTION_LIVE_VERIFIED: ❌ (Claude never saw context)
FULL_CONTINUITY_LIVE_VERIFIED: ❌
```

**Evidence**:
- Hook wrapper installed at `~/.claude/voyager_session_start.sh`
- Manual invocation produces full continuation bundle
- Unicode handling verified (Chinese characters preserved)
- Context compilation successful with active WorkThread

**Limitations**:
- No native hooks detected in settings.json
- Platform does NOT expose session-start callback API
- Requires CLAUDE.md instruction pattern as fallback
- Cannot automate GUI IDE launch for auto-trigger observation

**Critical distinction**: This is NOT launcher strategy. Wrapper handler exists but has no trigger point without platform configuration.

**Required for elevation**:
- Manual Claude IDE launch observation
- Verify settings load hook script automatically
- Confirm context visible in first response

**Verdict document**: `claude_continuity_verdict.md`

---

### ZCode Desktop: WATCHER_ATTACH_ONLY

```text
SUPPORTED:                        ✅
CONFIGURED:                       ✅
UNIT_VERIFIED:                    ✅
WATCHER_ATTACH_ONLY:              ✅ (DB polling strategy)
SOURCE_DISCOVERY_FIXED:           ✅ (env var + HOME override)
SCHEMA_VALIDATION_FIXED:          ✅ (PRAGMA row index correction)
FULL_CONTINUITY_LIVE_VERIFIED:    ❌
```

**Evidence**:
- SQLite DB discovery works with real schema validation
- Scan→parse chain functional after fixes
- Custom DB path override via `VOYAGER_ZCODE_DB` env var
- PRAGMA table_info column name fixed (row[1] not row[0])

**Current focus**: Investigate session creation timing in desktop app

---

## Technical Debt Resolved

### 1. Store API Cleanup (`voyager/store.py`)

**Problem**: Non-canonical `thread_member_sessions()` method added during hook testing

**Solution**: Reverted to existing APIs:
```python
session_ids = store.thread_member_ids(thread_id)
member_rows = [r for r in store.sessions() if r["id"] in session_ids]
```

### 2. Bundle Return Type Mismatch (`voyager/integrations/hook.py`)

**Problem**: `build_continuation_bundle()` returns `str`, not dict

**Fix**: Removed unnecessary `format_voyager_continuation()` wrapper:
```python
bundle = build_continuation_bundle(...)
context = bundle  # Already formatted markdown string
```

### 3. Windows Unicode Preservation (`voyager/integrations/hook.py`)

**Problem**: GBK encoding silently dropped Chinese/emoji content

**Solution**: Character-range filtering preserving ASCII + Chinese:
```python
safe_output = ""
for c in context:
    code = ord(c)
    if code < 128 or (0x4e00 <= code <= 0x9fff):
        safe_output += c
    else:
        safe_output += "?"
print(safe_output, end="", file=sys.stdout)
```

**Test coverage**: `tests/test_unicode_preservation.py`

---

## Remaining Provider Matrix

| Provider | Strategy | Current Level | Next Step |
|----------|---------|---------------|-----------|
| Grok CLI | Launcher wrapper | LAUNCH_PATH_LIVE_VERIFIED | Real prompt session test |
| Claude | Hook handler | HOOK_HANDLER_LIVE_VERIFIED | Manual GUI launch observation |
| ZCode Desktop | DB watcher | WATCHER_ATTACH_ONLY | Session creation timing study |
| Codex | First-turn skill guidance | PENDING TEST | Active WorkThread test |
| DSH | Watcher vs removal | CONFIGURED | Decide implementation |
| Cursor | Extension API speculation | NEEDS VERIFICATION | Docs audit |
| Kiro | Session start/Agent spawn | OFFICIAL HOOKS | Align with docs |
| Antigravity | BEST_EFFORT only | Reality check | Reliability test |

---

## Pending Deliverables

### High Priority
1. **Update dogfood_report.md** - Remove outdated claims
2. **Execute Grok Phase 1 & 2 tests** - Real prompt + context channel
3. **Manual Claude GUI test** - Observe auto-trigger behavior
4. **Codex integration test** - First-turn skill guidance verification

### Medium Priority
5. **DSH watcher decision** - Implement file polling OR remove claim
6. **Cursor extension audit** - Verify sessionStart event availability
7. **Kiro official hooks** - Align with published documentation

### Low Priority
8. **Antigravity reliability** - Keep only if consistently useful
9. **Full matrix regression suite** - Automated verification tests

---

## Git Commit Status

### Ready to commit:
- `voyager/integrations/hook.py` - Bundle return type fix + Unicode preservation
- `voyager/adapters/zcode.py` - Discover→scan chain + schema fixes
- `tests/test_unicode_preservation.py` - New regression test

### Pending verification:
- All live provider test scripts (`test_full_grok_continuity.py`, etc.)

### Do NOT push yet:
- Remote state still at `b3baf9367dfcf0ca5ab31915dc3f37d4f21af752`
- Wait until full test coverage achieved

---

## Evidence-Based Principles Applied

1. **No premature claims** - Don't extrapolate from incomplete tests
2. **Level-by-level progression** - Cannot skip verification stages
3. **Real vs assumption** - Distinguish observed behavior from speculations
4. **Document limitations** - Explicitly state what's missing per provider
5. **Conservative classification** - Better UNDER-classified than OVER-claimed

---

## Files Generated

- `claude_continuity_verdict.md` - Claude detailed analysis
- `grok_continuity_verdict.md` - Grok detailed analysis  
- `dogfood_test_findings.md` - Previous summary (superseded)
- `dogfood_verification_state.md` - This consolidated state
- `tests/test_unicode_preservation.py` - Unicode regression test

---

## Next Communication Format

Future reports will use standardized evidence hierarchy:

```text
Provider: [name]

LIVE_VERIFIED LEVELS:
[SUPPRTD][CONFIG][UNIT][HOOK/LAUNCH][CTX_INJ][SESS_DISC][SESS_ATTACH][FULL]

CURRENT CLASSIFICATION: [level_name]

WHAT'S PROVEN:
✅ [specific observations]

WHAT'S MISSING:
❌ [specific gaps requiring evidence]

NEXT TEST:
🎯 [concrete action item]
```

This ensures clarity, traceability, and no ambiguity about verification status.
