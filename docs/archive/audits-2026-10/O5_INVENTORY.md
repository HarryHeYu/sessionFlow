# O5.1 — Provider Verification Inventory

> **HISTORICAL PRE-O5 AUDIT SNAPSHOT (2026-10-03).**  Everything below the
> closure section describes the state of the repo *as it was when this audit
> was written* — including gaps that have since been closed (the
> CONTEXT_DELIVERED split, the hardcoded `ZERO_TOUCH_OBSERVED` set, the
> unwired codex/grok handlers).  Do not cite the findings below as current
> fact; see "CURRENT O5 CLOSURE" at the bottom for what holds at HEAD.

**Date**: 2026-10-03
**Status**: Historical audit snapshot (superseded by the closure section below)

## Executive Summary

There are **two parallel verification systems** that do not talk to each other:

1. **`capability_matrix.py`** (legacy) — uses a hardcoded `ZERO_TOUCH_OBSERVED = {"codex", "claude", "grok"}` set and log-file grepping to decide evidence. This is what `doctor`, `summary()`, and `provider_state()` consume.

2. **`verification_harness.py`** (proper) — uses an append-only `verification_events` table with correlation-chain derivation rules. This is what `voyager verify` consumes. But its `query_status()` still imports `declared_state` from `capability_matrix.provider_state()` — the hardcoded one.

Neither imports the other's evidence. The doctor's `check_verification()` reads exclusively from `capability_matrix`, completely ignoring `verification_harness`.

### Gaps Found

| Gap | Severity | O5 Step |
|-----|----------|---------|
| `codex_session_start.py` does NOT call `begin_hook`/`note_result` | Critical | O5.5 |
| `grok_native.py::session_start()` does NOT call `begin_hook`/`note_result` | Critical | O5.5 |
| `ZERO_TOUCH_OBSERVED` hardcoded set bypasses evidence table | Critical | O5.2 |
| `collect_evidence()` log-greps instead of querying `verification_events` | High | O5.2 |
| `query_status()` `declared_state` comes from hardcoded `provider_state()` | High | O5.2 |
| `doctor.check_verification()` reads from `capability_matrix`, not `verification_harness` | High | O5.7 |
| `voyager verify --all` flag does not exist | Medium | O5.8 |
| No `blocked_reason` / `best_chain` in doctor output | Medium | O5.7 |

---

## Per-Provider Inventory

### 1. CODEX

| Field | Value |
|-------|-------|
| DECLARED ceiling | `ZERO_TOUCH_LIVE_VERIFIED` (startup_hook, context, session_id, auto_attach, live_zero_touch) |
| Hook trigger | `~/.codex/hooks.json` SessionStart event |
| Context delivery path | `hookSpecificOutput.additionalContext` in stdout JSON |
| Native session id source | stdin `session_id` field in payload |
| Pending attach behavior | `auto_attach=True` → `pending_resolve` if session not yet indexed |
| Attach resolved path | `auto.py::resolve_pending_attaches()` → `note_attach_resolved_for_session()` |
| Observed evidence (legacy) | Hardcoded in `ZERO_TOUCH_OBSERVED` set |
| Observed evidence (harness) | **NONE** — handler does not call `begin_hook`/`note_result` |
| Highest derived state | `ZERO_TOUCH_LIVE_VERIFIED` (via hardcoded set, not evidence) |
| Missing evidence | Handler is NOT wired to `verification_harness` |

**Handler file**: `voyager/integrations/codex_session_start.py`
**Wiring**: Does NOT import `verification_harness`. Does NOT call `begin_hook` or `note_result`.
**Contrast**: Claude's handler (same provider class, Claude-compatible stdin) IS properly wired.

---

### 2. CLAUDE

| Field | Value |
|-------|-------|
| DECLARED ceiling | `ZERO_TOUCH_LIVE_VERIFIED` (startup_hook, context, session_id, auto_attach, live_zero_touch) |
| Hook trigger | `~/.claude/settings.json` SessionStart event |
| Context delivery path | `hookSpecificOutput.additionalContext` in stdout JSON |
| Native session id source | stdin `session_id` field |
| Pending attach behavior | `auto_attach=True` → `pending_resolve` or `already_attached` |
| Attach resolved path | `auto.py::resolve_pending_attaches()` → `note_attach_resolved_for_session()` |
| Observed evidence (legacy) | Hardcoded in `ZERO_TOUCH_OBSERVED` set |
| Observed evidence (harness) | Properly wired: `begin_hook("claude", ...)` + `note_result("claude", ...)` |
| Highest derived state | `ZERO_TOUCH_LIVE_VERIFIED` |
| Missing evidence | None for wiring. Gap: `query_status()` declared state still uses hardcoded set |

**Handler file**: `voyager/integrations/claude_session_start.py`
**Wiring**: Properly wired. `begin_hook` at line 217, `note_result` at line 230.

---

### 3. GROK

| Field | Value |
|-------|-------|
| DECLARED ceiling | `ZERO_TOUCH_LIVE_VERIFIED` (startup_hook, context, session_id, auto_attach, live_zero_touch) |
| Hook trigger | `~/.grok/hooks/*.json` SessionStart (passive — stdout ignored) |
| Context delivery path | Rules file written by launcher BEFORE `exec` (not via hook stdout) |
| Native session id source | `GROK_SESSION_ID` env var |
| Pending attach behavior | `startup_continuity(compile_context=False)` → `pending_resolve` |
| Attach resolved path | `auto.py::resolve_pending_attaches()` → `note_attach_resolved_for_session()` |
| Observed evidence (legacy) | Hardcoded in `ZERO_TOUCH_OBSERVED` set |
| Observed evidence (harness) | **NONE** — `grok_native.py::session_start()` does NOT call `begin_hook`/`note_result` |
| Highest derived state | `ZERO_TOUCH_LIVE_VERIFIED` (via hardcoded set, not evidence) |
| Missing evidence | Handler is NOT wired to `verification_harness` |

**Handler file**: `voyager/integrations/grok_native.py` (`session_start()` function)
**Wiring**: Does NOT import `verification_harness`. Does NOT call `begin_hook` or `note_result`.
**Note**: Grok's hook is passive (stdout ignored), so context delivery happens via rules file, not via the hook itself. The evidence chain would need to record CONTEXT_DELIVERED from the launcher, not from the hook handler.

---

### 4. ZCODE

| Field | Value |
|-------|-------|
| DECLARED ceiling | `UNIT_VERIFIED` (startup_hook, dynamic_context, session_id, auto_attach); `NOT_FOUND` (live_zero_touch) |
| Hook trigger | `~/.zcode/cli/config.json` |
| Context delivery path | `additionalContext` in envelope |
| Native session id source | stdin `session_id` |
| Pending attach behavior | Shared core path |
| Attach resolved path | Shared core path |
| Observed evidence (legacy) | NOT in `ZERO_TOUCH_OBSERVED`; log grepping finds nothing |
| Observed evidence (harness) | Properly wired: `begin_hook(PROVIDER, ...)` + `note_result(PROVIDER, ...)` |
| Highest derived state | `UNIT_VERIFIED` (no live observation — correct) |
| Missing evidence | No live observation on this machine. Correctly stays at UNIT_VERIFIED. |

**Handler file**: `voyager/integrations/zcode_session_start.py`
**Wiring**: Properly wired. `begin_hook` at line 132, `note_result` at line 157.

---

### 5. CURSOR

| Field | Value |
|-------|-------|
| DECLARED ceiling | `UNIT_VERIFIED` (startup_hook, dynamic_context, session_id, auto_attach); `NOT_FOUND` (live_zero_touch) |
| Hook trigger | `~/.cursor/hooks.json` |
| Context delivery path | Top-level `additional_context` |
| Native session id source | `session_id` / `conversationId` |
| Pending attach behavior | Shared core path |
| Attach resolved path | Shared core path |
| Observed evidence (legacy) | NOT in `ZERO_TOUCH_OBSERVED`; log grepping finds nothing |
| Observed evidence (harness) | Properly wired: `begin_hook(PROVIDER, ...)` + `note_result(PROVIDER, ...)` |
| Highest derived state | `UNIT_VERIFIED` (no live observation — correct) |
| Missing evidence | No live observation. Correctly stays at UNIT_VERIFIED. |

**Handler file**: `voyager/integrations/cursor_session_start.py`
**Wiring**: Properly wired. `begin_hook` at line 155, `note_result` at line 180.

---

### 6. KIRO

| Field | Value |
|-------|-------|
| DECLARED ceiling | `UNIT_VERIFIED` (startup_hook is project-scoped, none installed globally); `NOT_FOUND` (live_zero_touch) |
| Hook trigger | `.kiro/hooks/<id>.json` (project-scoped) |
| Context delivery path | Plain-text stdout contract |
| Native session id source | Payload session id |
| Pending attach behavior | Shared core path |
| Attach resolved path | Shared core path |
| Observed evidence (legacy) | NOT in `ZERO_TOUCH_OBSERVED` |
| Observed evidence (harness) | Properly wired: `begin_hook(PROVIDER, ...)` + `note_result(PROVIDER, ...)` |
| Highest derived state | `UNIT_VERIFIED` (no live observation — correct) |
| Missing evidence | No live observation. Correctly stays at UNIT_VERIFIED. |

**Handler file**: `voyager/integrations/kiro_session_start.py`
**Wiring**: Properly wired. `begin_hook` at line 133, `note_result` at line 156.

---

### 7. ANTIGRAVITY

| Field | Value |
|-------|-------|
| DECLARED ceiling | `UNIT_VERIFIED` (PreInvocation hook); `NOT_FOUND` (live_zero_touch) |
| Hook trigger | `~/.gemini/config/hooks.json` (PreInvocation, registered globally) |
| Context delivery path | `injectSteps.ephemeralMessage` |
| Native session id source | `conversationId` |
| Pending attach behavior | Shared core path |
| Attach resolved path | Shared core path |
| Observed evidence (legacy) | NOT in `ZERO_TOUCH_OBSERVED` |
| Observed evidence (harness) | Properly wired: `begin_hook(PROVIDER, ...)` + `note_result(PROVIDER, ...)` |
| Highest derived state | `UNIT_VERIFIED` (no live observation — correct) |
| Missing evidence | No live observation. Correctly stays at UNIT_VERIFIED. |

**Handler file**: `voyager/integrations/antigravity_session_start.py`
**Wiring**: Properly wired. `begin_hook` at line 158, `note_result` at line 181.

---

### 8. DSH

| Field | Value |
|-------|-------|
| DECLARED ceiling | `NOT_FOUND` for startup_hook, dynamic_context, session_id, auto_attach, live_zero_touch |
| Hook trigger | NONE (no native startup surface; profiles/plugins/ACP only) |
| Context delivery path | NONE |
| Native session id source | NONE at start |
| Pending attach behavior | NONE |
| Attach resolved path | NONE |
| Observed evidence (legacy) | NOT in `ZERO_TOUCH_OBSERVED`; no logs to grep |
| Observed evidence (harness) | Not wired (correctly — no hook surface exists) |
| Highest derived state | `NOT_FOUND` (correct) |
| Missing evidence | None expected. Provider has no startup surface. |

**Handler file**: `voyager/integrations/dsh.py` (launcher only, no session_start handler)
**Wiring**: Correctly NOT wired. No hook surface to record evidence from.

---

## Architecture: Two Parallel Systems

### System A: `capability_matrix.py` (Legacy)

```
ZERO_TOUCH_OBSERVED = {"codex", "claude", "grok"}   ← hardcoded
    ↓
collect_evidence(provider)
    → check ZERO_TOUCH_OBSERVED first (short-circuit)
    → else: grep ~/.voyager/logs/*-hooks.jsonl
    ↓
evidence_ceiling(ev) → ZERO_TOUCH_LIVE_VERIFIED | LIVE_VERIFIED | UNIT_VERIFIED
    ↓
provider_state(provider, ev)
    ↓
doctor.check_verification()  ← reads this
summary()  ← reads this
query_status().declared_state  ← reads this
```

**Problem**: The hardcoded set means codex/claude/grok are ALWAYS reported as ZERO_TOUCH_LIVE_VERIFIED regardless of what's in the evidence table. This is exactly the "claiming live verification it never did" that O5 exists to prevent.

### System B: `verification_harness.py` (Proper)

```
verification_events (append-only table)
    event_id = SHA256(provider|event_type|correlation_id|sid|src_sid|observed_at)
    ↓
chains_for(con, provider) → grouped by correlation_id
    ↓
Chain.status() → ordered derivation:
    HOOK_TRIGGERED → CONTEXT_DELIVERED → NATIVE_SESSION_ID_OBSERVED → ATTACH_RESOLVED
    (all in same chain, correct order, same session id)
    ↓
observed_state(con, provider) → best chain's status
    ↓
query_status():
    declared_state = capability_matrix.provider_state(p)  ← STILL HARDCODED
    effective_state = min(declared, observed)
```

**Problem**: `declared_state` still comes from the hardcoded `ZERO_TOUCH_OBSERVED` set. And the evidence table is empty for codex/grok because their handlers don't call `begin_hook`.

### The Fix Direction (O5.2)

1. `capability_matrix.collect_evidence()` should query `verification_harness.observed_state()` for runtime evidence, replacing the hardcoded set and log grepping.
2. `verification_harness.query_status()` should get `declared` from `DECLARED` table directly, not from `provider_state()` (which is the resolved one, not the ceiling).
3. `doctor.check_verification()` should consume `verification_harness.query_status()` for observed/effective/best_chain, not `capability_matrix.collect_evidence()`.
4. `ZERO_TOUCH_OBSERVED` hardcoded set should be removed (or become a no-op fallback only when no evidence table exists).

### Wiring Fix Direction (O5.5)

1. `codex_session_start.py`: add `begin_hook("codex", ...)` + `note_result("codex", ...)` (same pattern as claude).
2. `grok_native.py::session_start()`: add `begin_hook("grok", ...)` + `note_result("grok", ...)`. Note: Grok's context is delivered via rules file by the launcher, not via the hook — the evidence chain needs to reflect this (CONTEXT_DELIVERED may need to come from `write_context_rules()`, not from `session_start()`).

---

## O5.3: State Machine Definition (Proposed)

```
NOT_FOUND_IN_CURRENT_AUDIT  (0)  — provider has no startup surface
    ↓ [adapter + handler exist in code]
SUPPORTED  (1)  — code implements it, not yet configured
    ↓ [hook config file exists on this machine]
CONFIGURED  (2)  — hook registered, not yet observed firing
    ↓ [handler covered by unit tests]
UNIT_VERIFIED  (3)  — handler tested, not yet observed live
    ↓ [HOOK_TRIGGERED + CONTEXT_DELIVERED in same chain]
LIVE_VERIFIED  (4)  — hook observed firing, context delivered
    ↓ [NATIVE_SESSION_ID_OBSERVED + ATTACH_RESOLVED in order, same chain]
ZERO_TOUCH_LIVE_VERIFIED  (5)  — bare resume resumed the WorkThread
```

**Promotion conditions** (all must be true to promote):
- `SUPPORTED → CONFIGURED`: hook config file exists at the declared path
- `CONFIGURED → UNIT_VERIFIED`: handler code exists and is covered by tests
- `UNIT_VERIFIED → LIVE_VERIFIED`: evidence table has HOOK_TRIGGERED + CONTEXT_DELIVERED in same correlation chain
- `LIVE_VERIFIED → ZERO_TOUCH_LIVE_VERIFIED`: evidence table has NATIVE_SESSION_ID_OBSERVED + ATTACH_RESOLVED in same chain, correct order, same session id

**Demotion**: evidence can only lower, never raise. If evidence says LIVE but declared says ZERO_TOUCH, effective = LIVE.

---

## O5.4: Correlation Chain Rules (Already Enforced)

The `verification_harness.Chain.status()` already enforces:
1. Same `correlation_id` for all events in a chain
2. HOOK_TRIGGERED must precede CONTEXT_DELIVERED
3. NATIVE_SESSION_ID_OBSERVED must precede ATTACH_RESOLVED
4. CONTEXT_DELIVERED must precede ATTACH_RESOLVED
5. Only one unique `native_session_id` per chain (two sessions = not one lifecycle)

These rules are correct and tested (test_verification_harness_v2.py). No changes needed.

---

## O5.6: Passive Verification Rules

Already enforced:
- `voyager verify` is strictly read-only (opens SQLite read-only mode)
- `query_status()` never writes
- `cmd_verify()` never calls `record_event()`

To enforce (O5.6):
- No code path should open a provider GUI, start VS Code, log in, or modify provider config
- The only writers are `begin_hook()`, `note_result()`, `note_attach_resolved()`, `note_attach_resolved_for_session()` — all called from within hook handlers that fire when the provider naturally starts
- No proactive probing or synthetic triggers


---

# CURRENT O5 CLOSURE (as of the O5 evidence-truth phase)

What actually holds at CURRENT HEAD:

* **One evidence system.**  `capability_matrix.collect_evidence` reads the
  append-only `verification_events` table; the hardcoded `ZERO_TOUCH_OBSERVED`
  set and log-file grepping are gone (guarded by tests).  `query_status`,
  `provider_state`, `doctor.check_verification` and `voyager verify --json`
  derive the effective state from the same evidence and agree on it
  (cross-surface convergence tests).
* **Canonical event vocabulary**: HOOK_TRIGGERED, CONTEXT_PREPARED,
  CONTEXT_EMITTED, NATIVE_SESSION_ID_OBSERVED, ATTACH_PENDING,
  ATTACH_RESOLVED.  `CONTEXT_DELIVERED` no longer exists anywhere in runtime
  code; PREPARED (compiled) and EMITTED (transport succeeded) are strictly
  separate, and EMITTED is only written after serialize + write + flush
  succeeded (claude's stdout writer was corrected to return success and gate
  the record on it).
* **Wired writers**: codex, claude, zcode, cursor, kiro, antigravity
  (begin_hook + note_result; the stdout providers with a LIVE declared
  ceiling record CONTEXT_EMITTED after the real write).  Grok's transport is
  the rules file: `write_context_rules` is the declared "rules-writer hook"
  and records PREPARED after compile and EMITTED only after the file write
  succeeded; the passive SessionStart recorder keeps a separate chain.  The
  two grok chains are never spliced into a fake zero-touch.
* **Promotion rules (evidence-only)**: HOOK+EMITTED → LIVE_VERIFIED;
  HOOK+EMITTED+SID+RESOLVED in one chain, correctly ordered, single session
  identity → ZERO_TOUCH_LIVE_VERIFIED.  PREPARED alone never promotes past
  UNIT.  Multi-chain or cross-session evidence fails closed (refuses, never
  picks newest/oldest).
* **DSH**: no startup surface → startup dimensions NOT_FOUND; plugin/CLI
  capability is a different dimension and does not promote startup
  verification.
* **Passive surfaces**: `voyager verify` and doctor are strictly read-only
  (digest regressions); doctor never fabricates evidence.

Known machine-local note: ZERO_TOUCH_LIVE_VERIFIED *observations* are
machine-local evidence; the DECLARED matrix describes implementation
capability, and README claims are capped at declared until a given machine's
evidence table proves more.
