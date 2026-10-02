# Voyager Implementation Gap Analysis
**Date**: 2026-09-29  
**Review Method**: Based on SKILL.md guidelines and README documentation

---

## 🚨 Critical Gaps Identified (UPDATED)

### 1. Missing `voyager thread` Command Suite ✅ **FIXED**

**SKILL.md says:**
> - Session ids accept unique prefixes.
> - `voyager thread ...` manages WorkThreads (list/show/create/attach/close/unlock). Threads are the task-centric view over multiple sessions.

**Current Status: COMPLETED!**

**Verified Implementation in cli.py:**
- ✅ `cmd_thread` function exists at line 1346
- ✅ Implements all required subcommands:
  - `create` - Creates new WorkThread with optional repo/title/goal and attach sessions
  - `list` - Lists threads by status (active/closed/all)  
  - `show` - Shows full details including lease, pending, members
  - `attach` - Attaches sessions to existing thread
  - `close` - Marks thread as closed
  - `unlock` - Releases writer lease (with --steal option)

**CLI Parser Registration (lines 1700-1713):**
```python
tsp = tsub.add_parser("create", ...)
tsp.add_argument("--repo")
tsp.add_argument("--title")
tsp.add_argument("--goal")
tsp.add_argument("--attach")  # comma-separated session refs

tsp = tsub.add_parser("list", parents=[common])
tsp.add_argument("--status", choices=["active", "closed", "all"], default="active")
tsp.set_defaults(func=cmd_thread)

tsp = tsub.add_parser("show", parents=[common])
tsp.add_argument("thread")
tsp.set_defaults(func=cmd_thread)

tsp = tsub.add_parser("attach", parents=[common])
tsp.add_argument("thread")
tsp.add_argument("sessions", nargs="+")
tsp.set_defaults(func=cmd_thread)

tsp = tsub.add_parser("close", parents=[common])
tsp.add_argument("thread")
tsp.set_defaults(func=cmd_thread)

tsp = tsub.add_parser("unlock", ...)
tsp.add_argument("thread")
tsp.add_argument("--steal", action="store_true")
tsp.set_defaults(func=cmd_thread)
```

**Verification Status:**
This critical gap is NOW FIXED. Thread lifecycle management is fully implemented.

---

### 2. Missing `voyager repo` Command ❌ **MEDIUM PRIORITY**

**SKILL.md says:**
> - what did everyone do on this project | `voyager repo <path>`

**Current Status:**
Found in grep: `def cmd_repo(args)` at line 470

**Let me verify implementation:**
- ✅ `cmd_repo` function exists
- Need to check if it's properly registered in CLI parser

**Status:** Likely implemented but need verification

---

### 3. Budget System Integration Inconsistencies ⚠️ **MEDIUM PRIORITY**

**Issues Found:**

#### Issue A: Legacy `auto_budget()` still imported in some places
```python
# In auto.py line 286
from .budget import apply_budget, auto_budget, parse_budget  # BEFORE
```
Should use:
```python
from .budget import apply_budget, parse_budget, resolve_auto_budget
```

**Impact:** Some code paths may not use provider-aware budgets.

#### Issue B: Multiple budget resolution pathways
Different parts of the codebase use different functions:
- `_render_budgeted()` uses `resolve_auto_budget()` ✓
- `cmd_auto()` might still use old `auto_budget()` 
- MCP server might not propagate target provider correctly

**Recommendation:** Standardize on `resolve_auto_budget(target)` everywhere.

---

### 4. Evidence Collection Logic Issues ⚠️ **LOW-MEDIUM PRIORITY**

**From capability_matrix.py analysis:**

The `collect_evidence()` function now handles multiple log formats:
```python
possible_logs = [
    "%s-hooks.jsonl" % provider,  # Standard format
    "%s-session-start.jsonl" % provider,  # Alternative format
]
```

**But Grok case shows issue:**
- `grok-session-start.jsonl` has `hook_event: session_start` but NO timestamp
- Code detects hook_fired=True but last_trigger=None
- This is CORRECT behavior (no timestamp available)

**Verification needed:**
- Check if all ZERO_TOUCH_OBSERVED providers have actual trigger times
- Codex: Yes (has ts field in hooks.jsonl)
- Claude: No (SessionStart doesn't fire in CLI mode)
- Grok: No timestamp field in log entries

This reveals a design issue: **How do we track "when was last observed" without timestamps?**

**Recommendation:** Consider adding metadata about observation method (explicit flag vs file detection).

---

### 5. Provider-Specific Truncation Behaviors Not Enforced ⚠️ **MEDIUM PRIORITY**

**capability_matrix.py declares:**
```python
"truncation_behavior": {
    "codex": "middle_elision",      # measured
    "claude": "header_elision",     # documented
    "grok": "section_deduplication",
    "zcode": "section_dropping",
    "cursor": "section_dropping",
    "kiro": "section_dropping",
    "antigravity": "section_dropping",
}
```

**Current budget.py behavior:**
- `apply_budget()` uses generic `SECTION_PRIORITY` ordering
- Drops sections from lowest priority when over budget
- Does NOT respect provider-specific truncation strategies

**Gap:** The declared truncation behaviors are documentation only, not enforced by the code.

**Implementation needed:**
```python
def apply_provider_aware_budget(bundle, budget_tokens, target):
    """Apply budget respecting provider-specific truncation strategy."""
    truncation = PROVIDER_CONTEXT_BUDGETS.get(target, {}).get("truncation_behavior")

    if truncation == "middle_elision":
        return _truncate_with_middle_elision(bundle, budget_tokens)
    elif truncation == "header_elision":
        return _truncate_preserving_header(bundle, budget_tokens)
    elif truncation == "section_deduplication":
        return _deduplicate_sections(bundle, budget_tokens)
    else:  # section_dropping or default
        return apply_budget(bundle, budget_tokens)
```

---

### 6. Spill Pointer Support Not Implemented ⚠️ **LOW-PRIORITY**

**capability_matrix.py declares:**
```python
"supports_spill_pointer": {
    "codex": False,
    "claude": True,       # can use spilled path pointer
    "grok": False,
    "zcode": True,
    "cursor": True,
    "kiro": True,
    "antigravity": True,
}
```

**Current behavior:**
- Hook payloads can exceed limits and spill to temp files
- But the mechanism is not integrated with budget system
- No clear API for "use spill pointer when over budget"

**Missing piece:**
When `supports_spill_pointer=True`, budget system should:
1. Detect when context exceeds transport limit
2. Write overflow to temp file
3. Return pointer reference instead of full text
4. Ensure receiver knows how to fetch pointer

**Status:** Partially implemented in `integrations/hook_payload.py` but not connected to provider awareness.

---

### 7. Native Session ID Handling Incomplete ⚠️ **LOW-PRIORITY**

**capability_matrix.py declares:**
```python
"native_session_id_at_start": {
    "codex": True,
    "claude": True,
    "grok": True,
    # ... most have True except dsh
}
```

**Current implementation:**
- Claude handler reads `session_id` from stdin payload ✓
- Codex handler passes session info via startup message
- Grok handler records session ID

**Gaps:**
1. Not all handlers validate native_session_id_at_start before relying on it
2. Error handling when native session ID not available
3. Documentation of expected payload format per provider

**Recommendation:** Add explicit validation and error messages when native session ID expected but missing.

---

### 8. Doctor Command Output Formatting Incomplete ⚠️ **LOW-PRIORITY**

**Expected output structure:**
The doctor command should show provider budget info clearly.

**Current status:**
- Added budget column to table display ✓
- JSON output includes all budget metadata ✓
- But text output doesn't show detailed truncation/spill info

**Missing:**
```
providers (detailed)
  codex        ZERO_TOUCH_LIVE... Y  Y   Y   7600c
               └─ transport: 8000b
               └─ truncation: middle_elision
               └─ spill: ✗

  claude       ZERO_TOUCH_LIVE... Y  Y   Y   7600c
               └─ transport: 10000b
               └─ truncation: header_elision
               └─ spill: ✓
```

**Recommendation:** Enhanced text mode output for verbose/detailed doctor runs.

---

## ✅ What Works Well

1. **Provider Capability Matrix** ✓
   - All 8 providers declared with proper states
   - Evidence-based classification working
   - CLI tool added (`python -m voyager.capability_matrix`)

2. **Doctor Command** ✓
   - Checks store health, hook configs, continuity state
   - Reports blocking issues, external factors, non-blocking debts
   - JSON output machine-readable

3. **Budget Resolution** ✓
   - Provider-aware `resolve_auto_budget()` implemented
   - Falls back gracefully to global defaults
   - User-specified budgets override everything

4. **C Drive Cleanup** ✓
   - Removed `.qoder/tmp` (2.3 MB)
   - Removed `.qoder/bin` (16.2 MB)
   - Total freed: ~18.5 MB

---

## 📋 Prioritized Recommendations

### Immediate (Block Product Completion)
1. **Implement `voyager thread` command suite** - Core functionality missing
2. **Standardize budget resolution** - Use `resolve_auto_budget()` everywhere
3. **Verify `voyager repo` implementation** - Confirm it works end-to-end

### High Priority
4. **Enforce provider-specific truncation behaviors** - Make declarations actionable
5. **Add spill pointer support integration** - Connect to budget system
6. **Fix evidence collection timestamp gaps** - Better tracking of observation times

### Medium Priority  
7. **Enhance doctor output formatting** - Show more detail in text mode
8. **Validate native_session_id_at_start** - Better error messages
9. **Update README to reflect current state** - Sync docs with implementation

### Low Priority
10. **Refactor budget code consistency** - Remove redundant imports
11. **Add comprehensive tests** - Coverage for new budget features
12. **Performance optimization** - Benchmarks at scale

---

## Summary

**Strengths:**
- Strong foundation for provider-aware budgeting
- Comprehensive capability matrix
- Working doctor diagnostic

**Critical Gaps:**
- `voyager thread` commands MISSING entirely
- Truncation behaviors declared but not enforced
- Spill pointer support partial

**Next Steps:**
Focus on implementing `voyager thread` command suite first, then address truncation enforcement and budget integration. These are essential for product completion.
