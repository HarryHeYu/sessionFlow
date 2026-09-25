# Grok Continuity Verification Verdict

## Executive Summary

**Status**: CLOSED — `FULL_CONTINUITY_LIVE_VERIFIED` (2026-09-25, live)

**Why the assessment below changed.** It was written when the only surface was
the opt-in launcher wrapper, and that wrapper never ran: it only takes effect
when `~/.voyager/bin` precedes the real binary on `PATH`, which is not the
default. Every "unproven" item was resolved by implementing Grok's two *native*
surfaces instead of a wrapper — a `SessionStart` hook that records the pending
attach with the `GROK_SESSION_ID` Grok hands it, and the `$GROK_HOME/rules/`
file that carries the continuation context into an interactive session. The live
acceptance is recorded in [Final Acceptance](#final-acceptance--closed-2026-09-25).

---

## Verification Level Matrix

```text
SUPPORTED:                           yes (CLI at ~/.grok/bin/grok.exe)
CONFIGURED:                          yes (native SessionStart hook + rules file)
UNIT_VERIFIED:                       yes (tests/test_grok_native.py, mutation-verified)
LAUNCHER_ZERO_TOUCH:                 yes (wrapper still installed; not required)
LAUNCH_PATH_LIVE_VERIFIED:           yes (`grok inspect` lists the rules file)
CONTEXT_INJECTION_LIVE_VERIFIED:     yes (2026-09-25 — see Final Acceptance)
SESSION_DISCOVERY_LIVE_VERIFIED:     yes (2026-09-25)
SESSION_ATTACH_LIVE_VERIFIED:        yes (2026-09-25)
CROSS_PROVIDER_INVISIBLE_CONTINUITY: yes (2026-09-25)
FULL_CONTINUITY_LIVE_VERIFIED:       yes (2026-09-25)
```

---

## Final Acceptance — CLOSED (2026-09-25)

A real cross-provider run: a fresh private sentinel was written **only** in the
Claude WorkThread, then Grok was started normally — no Voyager command at the
Grok launch, no context copied by hand, no `switch` / `handoff` / `continue`.

The sentinel value is deliberately not recorded here; it was generated for this
run alone and never written to the repository.

### The chain

```text
Claude WorkThread thr_0854d50b88
  → fresh private sentinel written in Claude session 6452817d-d82d-4c97-9507-e02bf3b3f1fd
  → voyager scan                    (source preparation; the protocol allows this one)
      ↳ grok continuation rule refreshed → ~/.grok/rules/voyager-continuation.md
  → normal `grok`                   (no voyager command, no pasted context)
  → Grok recovers the sentinel + prior task state                    ← A
  → native Grok session 01a0d890-6803-7710-ab08-068c8420db1f
  → attached to thr_0854d50b88 with no manual scan                   ← B
```

### A — context injection: PASS

Grok reproduced the private sentinel — which had only ever existed inside the
Claude session — together with the WorkThread (`thr_0854d50b88`), its title and
goal, and an accurate description of the work in progress. The channel is the
rules file, and Grok's own configuration report confirms it is loaded:

```text
$ grok inspect --json
projectInstructions: {"path": "C:\\Users\\He_Yu_Hao\\.grok\\rules\\voyager-continuation.md",
                      "scope": "global", "fileType": "rules",
                      "sizeBytes": 94427, "approxTokens": 23606}
projectTrusted: true
```

The same report lists the hook that records the pending attach:

```text
hooks: {"event": "session_start", "hookType": "command",
        "target": "voyager-session-start.cmd",
        "source": {"type": "user", "path": "C:\\Users\\He_Yu_Hao\\.grok\\hooks"}}
```

### B — automatic native attach: PASS

State-transition evidence, re-read from `~/.voyager/index.db` after the run:

| fact | value |
|---|---|
| pending row `(thr_0854d50b88, grok)` | `native_session_id = 01a0d890-6803-7710-ab08-068c8420db1f` |
| pending `created_at` → `resolved_at` | 20:36:38 → 20:36:39 |
| `resolved_sid` | `grok:01a0d890-6803-7710-ab08-068c8420db1f` |
| thread membership | `grok:01a0d890-6803-7710-ab08-068c8420db1f` ∈ `thr_0854d50b88` |
| indexed session | `repo_root=E:/code/voyager/`, `started_at` 20:35:37, 9 messages |

The pending carried the native id, so the resolver matched it **by identity**
rather than by the "exactly one candidate" heuristic, and the id in the pending
is the same id that appears in the thread membership.

**Attribution — what the data can and cannot carry.** The state transition above
is database-verifiable. That no manual `voyager scan` / `switch` / `handoff` /
`continue` ran between the Grok launch and the attach is an *operator record*,
not something the index can prove. What the data does support is that the
**timing is consistent with automatic resolution**: the watcher in use was
started 20:26:36, so its cycles fall at ≈20:26:36 / ≈20:31:4x / ≈20:36:4x, and
no Claude hook event is logged near 20:36:39 (`logs/provider-hooks.jsonl`), so
the resolution lines up with a watcher cycle rather than a provider-triggered
scan.

### Environment the acceptance ran against

```text
hook     = 125686e   (fresh process per event, so always the on-disk code)
watcher  = 125686e   (PID 39828, CreationDate 2026-09-25 20:26:36,
                      "C:\Python314\pythonw.exe" -m voyager.cli watch --interval 300)
resolver = 125686e
rules    = 125686e
```

The watcher was restarted for this run precisely so that no 2026-09-21 process
took part: an earlier watcher (PID 31556, started 2026-09-23 10:11) predated the
implementation and would have made the attach evidence a mixed-version result.

### Markers set by this run

```text
CONTEXT_INJECTION_LIVE_VERIFIED      = true
CROSS_PROVIDER_INVISIBLE_CONTINUITY  = true
FULL_CONTINUITY_LIVE_VERIFIED        = true
Zero-Touch Final Acceptance          = CLOSED / PASS
```

---

## What Passed ✅

### 1. Wrapper Script Installation
```
Location: C:/Users/He_Yu_Hao/.voyager/bin/grok.bat
Content: Opt-in launcher preventing recursion + voyager prelaunch call
```

### 2. Recursion Protection
Environment variable `VOYAGER_LAUNCHER_RUNNING` prevents infinite loops.

### 3. Pre-launch Hook Invocation
Test evidence:
```bash
$ grok --help 2>&1 | grep "Voyager"
Provider: grok
Status: no_thread
```

✅ Voyager prelaunch fires on grok invocation  
✅ Output captured and parsed correctly  

---

## Critical Limitations ❌

### Limitation 1: Test Coverage Gap

**Current test**: `grok --help`  
**Problem**: Command-line help does NOT prove conversation/session lifecycle

**What's missing**:
- Real conversational prompt: `grok "<task>"`
- Interactive mode test: `grok` → then input task
- Post-prompt session inspection: Are ANY files created?

Without these, cannot claim Grok CLI creates/persists sessions.

### Limitation 2: Active WorkThread Required

Current test repo (`E:/code/voyager`) has:
- Multiple active threads OR
- None configured for testing

For full E2E: Need dedicated test repo with exactly one active WorkThread containing known marker.

### Limitation 3: Unknown Context Injection Channel

**Critical question**: How does prelaunch output reach the model?

**Current wrapper flow**:
```bash
voyager launcher prelaunch --provider grok --cwd "$PWD"
↓ (outputs markdown text)
exec real_grok "$@"
```

**Missing link**: If prelaunch outputs to stdout/stderr, how does that enter Grok's context?

**Possible channels** (all need verification):
1. **CLI argument**: Does wrapper inject bundle as prompt?
   ```bash
   grok "[Voyager Continuation]\n..." <user_prompt>
   ```
2. **stdin**: Pipes context to stdin before waiting for input?
3. **Temp file**: Writes bundle to disk, Grok reads via instruction?
4. **Just logging**: Wrapper prints to terminal, user must manually copy?

**If only #4**: This is "prelaunch lookup", NOT "context injection".  
**If #1-3**: Then true zero-touch continuity possible.

---

## Next Tests Required

### Phase 1: Session Creation Verification

**Create test in dedicated repo**:
```bash
cd /tmp/voyager-grok-session-test
git init
echo "# Session Test" > README.md && git add . && git commit -m "Init"
voyager thread create --title "Session Test" --repo "$(pwd)"
```

**Launch real conversation**:
```bash
~/voyager-grok-test-repo/../voyager/voyager/integrations/grok/bat <<'EOF'
Check current project state and report progress
EOF
```

**Inspect after 5 seconds**:
```bash
ls -la ~/.grok/sessions/E%3A%5Ctmp%5Cvoyager-grok-session-test/
# If directories created: Sessions ARE persisted
# If empty: CLI may be ephemeral-mode design
```

**Verdict options**:
- Sessions created → Mark `SESSION_CREATION_LIVE_VERIFIED`
- No sessions → Mark `NO_DISCOVERABLE_NATIVE_SESSION`

### Phase 2: Context Injection Verification

**Setup**: Dedicated repo with exactly one active WorkThread containing marker `VOYAGER_GROK_SENTINEL_2026`.

**Test**:
```bash
cd /tmp/voyager-grok-context-test
# Create WorkThread attached to existing ZCode session with marker
voyager thread attach thr_xxx zcode:sess_yyy

# Launch via wrapper (no Voyager mention in prompt)
~/.voyager/bin/grok "继续工作，检查当前项目状态"
```

**Verify after launch**:
1. Does prelaunch output go somewhere usable?
2. Is continuation visible in any log/file?
3. Did new session get created with marker visible?
4. Can subsequent Grok commands reference prior context?

**Output analysis**:
```bash
# Check chat_history.jsonl entries
tail ~/.grok/sessions/E%3A%5Ctmp%5Cvoyager-grok-context-test/*/chat_history.jsonl
```

**Verdicts**:
- Marker visible in session → `CONTEXT_INJECTION_LIVE_VERIFIED`
- No marker visible but sessions exist → `SESSION_CREATION_LIVE_VERIFIED, CONTEXT_INJECTION_FAILED`
- No markers anywhere → `CONTEXT_INJECTION_CHANNEL_UNKNOWN`

---

## Evidence vs Assumption Audit

| Claim | Evidence | Status |
|-------|----------|--------|
| Grok CLI creates sessions | Only `active_sessions.json` observation | ❓ |
| Session directory structure is persistent | Manual ls shows folders | ✅ |
| `--help` proves lifecycle | False equivalence | ❌ |
| Wrapper fires prelaunch | Terminal output captured | ✅ |
| Context injected into model | Unknown channel | ❓ |
| New sessions discoverable by adapter | Adapter code reviewed | ✅ |

---

## Technical Debt Resolved During Testing

No major technical debt discovered during Grok testing. Existing integration code clean.

**Files reviewed**:
- `voyager/adapters/grok.py`: Correctly parses `~/.grok/sessions/<cwd>/<uuid>/` structure
- `voyager/integrations/grok.py`: Opt-in wrapper with recursion protection
- `~/.voyager/bin/grok.bat`: Windows batch wrapper (exists)

---

## Recommended Classification Path

> **Superseded (2026-09-25).** This section was written when the launcher wrapper
> was the only surface. The elevation it lists as "required" has since been
> reached through Grok's native surfaces — see
> [Final Acceptance](#final-acceptance--closed-2026-09-25). Kept as the record of
> what was believed at the time.

### Current state at the time of writing: LAUNCH_PATH_LIVE_VERIFIED

Good foundation: wrapper exists, hook fires, prelaunch works.

### Required for Elevation

#### To `SESSION_CREATION_LIVE_VERIFIED`:
- Run Phase 1 tests showing sessions created post-prompt

#### To `CONTEXT_INJECTION_LIVE_VERIFIED`:
- Identify and verify injection channel (arg/stdin/file)
- Show marker propagates through chain

#### To `FULL_CONTINUITY_LIVE_VERIFIED`:
- All above PLUS: new session attached to WorkThread automatically

---

## Files Generated

- `grok_continuity_verdict.md` - This analysis
- `/tmp/test_grok_real_prompt.sh` - Automated Phase 1 test (requires execution)
- `test_full_grok_continuity.py` - Previous incomplete test (replaced by Phase 1 approach)

---

## Git Commit Status

Pending commits include:
- `voyager/store.py` - Reverted (removed non-canonical API)
- `voyager/integrations/hook.py` - Unicode handling, return type fixes
- Existing: ZCode discover→scan chain fix
- Existing: Schema validation PRAGMA row index fix

All production-ready.

---

## Final Recommendation

Maintain conservative classification until:
1. Real prompt → session persistence proven (Phase 1)
2. Context injection channel identified (Phase 2)

Do NOT extrapolate from `--help` or `active_sessions.json` emptiness.

Actual evidence required, not assumption-driven claims.
