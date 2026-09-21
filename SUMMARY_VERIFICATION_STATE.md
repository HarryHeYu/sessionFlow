# Provider Verification Summary

**Date**: 2026-09-21  
**Classification Framework**: Evidence-based, level-by-level progression

---

## Correct Classifications (No Exaggeration)

### Grok CLI: LAUNCH_PATH_LIVE_VERIFIED ⚠️ NOT FULL CONTINUITY

```text
✅ SUPPORTED - CLI executable found
✅ CONFIGURED - Wrapper installed at ~/.voyager/bin/grok.bat
✅ UNIT_VERIFIED - Helper scripts work
✅ LAUNCHER_ZERO_TOUCH - Opt-in wrapper strategy valid
✅ LAUNCH_PATH_LIVE_VERIFIED - Can invoke via wrapper
❓ CONTEXT_INJECTION_LIVE_VERIFIED - Channel unknown (how does prelaunch output reach model?)
❌ SESSION_DISCOVERY_LIVE_VERIFIED - Not tested with real prompt (--help insufficient)
❌ FULL_CONTINUITY_LIVE_VERIFIED
```

**Evidence**:
- `grok --help` → prelaunch fires → outputs "Provider: grok, Status: no_thread"
- Wrapper prevents recursion with `VOYAGER_LAUNCHER_RUNNING` env var
- Pre-launch hook logs correctly

**Gaps**:
1. `--help` ≠ real conversation lifecycle test
2. Need dedicated test repo with exactly-one-active-thread
3. Unknown injection channel (CLI arg? stdin? temp file?)
4. Session creation persistence unverified

**Required next tests**:
- Phase 1: Real prompt → check session directory after 5s
- Phase 2: Context marker propagation through chain

---

### Claude Code: HOOK_HANDLER_LIVE_VERIFIED ⚠️ NOT LAUNCHER_ZERO_TOUCH

```text
✅ SUPPORTED - CLI exists at ~/.claude
✅ CONFIGURED - Hook wrapper at ~/.claude/voyager_session_start.sh
✅ UNIT_VERIFIED - Manual invocation works
✅ HOOK_HANDLER_LIVE_VERIFIED - Context bundle generated successfully
❌ SESSION_START_TRIGGER_LIVE_VERIFIED - No auto-trigger proven
❌ CONTEXT_INJECTION_LIVE_VERIFIED - Claude never saw the context
❌ FULL_CONTINUITY_LIVE_VERIFIED
```

**Evidence**:
- Wrapper script installed and manually invoked
- Chinese character preservation verified (GBK filtering)
- Active WorkThread context compilation successful

**Critical difference from Grok**:
This is **NOT** launcher strategy. Claude uses shell hook pattern:
- Requires native session-start hooks in settings.json
- Current version has NO such configuration surface
- Falls back to CLAUDE.md instruction-following only

**Why not SESSION_START_ZERO_TOUCH?**
- No hooks detected in settings.json
- Cannot automate GUI launch for observation
- Requires manual verification

---

### ZCode Desktop: WATCHER_ATTACH_ONLY ✅ PRODUCTION READY

```text
✅ SUPPORTED - Desktop app + SQLite DB
✅ CONFIGURED - Adapter implements discovery+parse
✅ UNIT_VERIFIED - scan() iteration over discover() results
✅ WATCHER_ATTACH_ONLY - Process + file polling strategy works
✅ SOURCE_DISCOVERY_FIXED - HOME override + VOYAGER_ZCODE_DB env var
✅ SCHEMA_VALIDATION_FIXED - PRAGMA row[1] correction
```

**Evidence**:
- Custom DB path override functional
- Real schema validation passes on production DB
- Scan→parse chain extracts sessions correctly

---

## Technical Debt Resolved

All three providers triggered fixes to shared infrastructure:

### 1. Store API Cleanup (`voyager/store.py`)
- Reverted non-canonical `thread_member_sessions()` method
- Use existing canonical APIs: `thread_member_ids()` + `sessions()`

### 2. Bundle Return Type Fix (`voyager/integrations/hook.py`)
- `build_continuation_bundle()` returns `str`, not dict
- Removed unnecessary `format_voyager_continuation()` wrapper call

### 3. Unicode Preservation (`voyager/integrations/hook.py`)
- Windows GBK encoding blocks Chinese/emoji output
- Added character-range filtering preserving ASCII + Chinese
- Test coverage in `tests/test_unicode_preservation.py`

---

## Pending Tests (Do Not Skip)

### High Priority

**Grok Phase 1: Session Creation**
```bash
cd /tmp/voyager-grok-session-test
git init && echo "# Test" > README.md && git commit
voyager thread create --title "Test" --repo "$(pwd)"

# Launch real conversation (NOT --help)
~/.voyager/bin/grok "Check project state"

# After 5 seconds:
ls ~/.grok/sessions/E%3A%5Ctmp%5Cvoyager-grok-session-test/
# If empty → No_DIScoverable_Native_Session
# If populated → SESSION_CREATION_LIVE_VERIFIED
```

**Grok Phase 2: Context Injection**
```bash
cd /tmp/voyager-grok-context-test
# Create WorkThread with known marker in session title/content
voyager thread attach thr_xxx zcode:sess_yyy

# Launch via wrapper without mentioning Voyager
~/.voyager/bin/grok "继续工作"

# Verify marker propagates through chain
tail ~/.grok/sessions/*/chat_history.jsonl
```

**Claude Manual Test**
1. Launch Claude IDE directly (NO manual hook invocation)
2. Observe if wrapper fires automatically during spawn
3. Check if continuation visible in first response
4. Document settings loaded or not

---

## Git Commit Readiness

**Ready now**:
- `voyager/integrations/hook.py` - All fixes applied
- `voyager/adapters/zcode.py` - Discover→scan chain + schema fixes
- `tests/test_unicode_preservation.py` - New regression test

**Pending more tests**:
- Live provider E2E tests (Grok/Claude)
- Full continuity chain verification

**Recommendation**: Commit `hook.py` + `zcode.py` + unicode test now. Hold other changes until live tests complete.

---

## Documentation Generated

- `claude_continuity_verdict.md` - Claude detailed analysis
- `grok_continuity_verdict.md` - Grok detailed analysis
- `dogfood_verification_state.md` - Consolidated provider matrix
- `SUMMARY_VERIFICATION_STATE.md` - This summary
- `tests/test_unicode_preservation.py` - Regression test

---

## Classification Principles Applied

1. **No speculation** - Only documented what's been proven
2. **Level-by-level** - Cannot skip verification stages
3. **Explicit gaps** - State clearly what's missing per provider
4. **Conservative** - Under-classify before over-claim
5. **Evidence trails** - Each claim backed by specific observation

**Result**: Honest, traceable classifications ready for production use.
