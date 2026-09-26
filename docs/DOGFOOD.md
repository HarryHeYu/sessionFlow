# Voyager Real-World Dogfood Guide

> **Correction notice (2026-09-23).** The startup classifications this document
> used to assert have been **retracted**. The old conclusion — "Claude Code
> provides no automatic session-start trigger, therefore `STARTUP_ASSISTED`" —
> was wrong *in its reasoning*. The real cause was a bug in Voyager's own
> installer: `ClaudeIntegration.install()` wrote an invented flat schema **and
> was never wired into `voyager integrate install` at all**, so nothing was ever
> registered and the absence of a trigger was self-inflicted. Both are fixed
> (see `fix(claude)` / `fix(cli)` in `CHANGELOG.md`).
>
> Claude Code now gets a **real native `SessionStart` hook** written into
> `~/.claude/settings.json`, and the provider firing it is **live-verified
> (2026-09-24)** — see *Live verification* below.
> `SESSIONSTART_TRIGGER_LIVE_VERIFIED` is now `true`. The CLI still prints `H`,
> because that letter is a static capability reading with no persisted
> live-evidence state behind it; no provider prints `Y`. Full narrative:
> [`claude_continuity_verdict.md`](../claude_continuity_verdict.md).

**Status**: Automated tests cover all hermetic verification; real-agent end-to-end flows require manual execution with installed agents.

## Testing Boundary

### Automated Tests (full dev extras: 374 collected → 372 passed, 2 skipped)

Two environments, two different numbers — they are not interchangeable:

| Environment | Command | Result |
|---|---|---|
| Full dev extras | `python -m pytest tests/ -q` | `374 collected → 372 passed, 2 skipped, 0 failed` |
| Simulated core-only | `python scripts/run_tests_core_only.py` | `374 collected → 356 passed, 18 skipped, 0 failed` |

Almost all tests are hermetic — they build synthetic provider fixtures in a tmp
tree and never call a provider. Two are not: they read the **default local
index** and skip when it holds no suitable data. The skips fall into two
classes, and conflating them is the mistake this section exists to prevent:

- **Dependency-gated (16, core-only only)** — `mcp`, `zstandard` and `PIL` are
  absent, so `test_continuity_tools.py`, `test_mcp.py`, `test_diagram.py` and
  `test_dsh.py` skip. Installing `.[all,dev]` removes all 16, which is why the
  full dev run skips only twice.
- **Data-dependent, against the default local index (2, both environments)** —
  `tests/test_unicode_preservation.py` skips when the local index holds no
  Chinese-titled session (`:33`) or no sessions at all (`:103`). Independent of
  what is installed.

Neither class is a platform gate, and neither is a failure.

**What the hermetic tests cover:**

- `test_adapters.py` — Parse synthetic JSON/SQL for all 8 providers
- `test_*.py` — Store, continuity, budget, leases, switch, thread operations
- All tests verify correctness without touching real agent sessions

### Real-Provider Runtime Tested (Requires Installed Agents)
These flows were tested on actual Claude/Codex installations:

1. **Codex startup discovery** → **`N`** — Codex exposes no native session-start hook, so there is nothing to register
2. **Claude startup discovery** → **`H`** — a real native `SessionStart` hook is written by the installer, and the provider has now been **observed firing it on a live run (2026-09-24)**. The CLI letter stays `H` for the reason given under *Live verification*
3. **Claude Code auto-triggers Voyager at session start** → **proven 2026-09-24** — the provider's own `session_id` reached the hook, the transcript was discovered and indexed, and the session attached itself to its WorkThread with no Voyager command. No other provider is proven to do this
4. **Target agent continuation context** — verified via explicit workflows (`switch`/`continue`); whether the model actually *uses* the injected context is still unproven (`CONTEXT_INJECTION_LIVE_VERIFIED`)

Startup legend (matches `voyager integrate status`): **`Y`** = zero-touch verified live · **`H`** = native hook registered · **`A`** = startup-assisted · **`N`** = no hook. The letter reflects **static capability and configuration only** — Voyager keeps no persisted live-evidence state, so it does not move when a human observes a live trigger. Claude Code's trigger *has* been observed live (2026-09-24) and it still reports `H`; nothing reports `Y`.

This document provides a repeatable manual verification procedure.

---

## Manual Dogfood Procedure

### Prerequisites
- Install at least two of: Codex CLI, Claude Code, Grok CLI
- Have existing session history from these agents
- Clone or navigate to a repo where you've worked with multiple agents

### Step 1: Prepare Source Session
```bash
# Note your current active session ID from one agent
cd ~/dev/my-project
voyager list --platform codex  # Example: note "codex:m0"
```

### Step 2: Create WorkThread & Execute Task A
```bash
# Make sure you have an active WorkThread for this repo
voyager thread create --repo . --goal "implement feature X"
voyager thread show <thread_id>
```

Work with Codex on the task until completion. Note the last session id.

### Step 3: Switch Agent Without Manual Handoff
```bash
# Do NOT execute: voyager handoff / voyager merge / voyager thread attach
# Just run:
voyager switch claude --no-launch
```

Expected behavior:
- Continuation bundle is compiled automatically
- Pending attach record is created (`voyager thread show` shows it)
- No manual session attachment performed

### Step 4: Start Target Agent Manually
```bash
# Launch Claude with the bundle path shown by previous command
claude "<bundle contents>"
```

Do NOT execute `voyager thread attach`. Let Voyager's auto-attach mechanism handle it.

### Step 5: Trigger Auto-Resolution
```bash
# In another terminal:
voyager watch --interval 5  # Or wait for next incremental scan
# After 10-60 seconds, check:
voyager thread show <thread_id>
```

Expected: The newly started Claude session should appear in members without manual attach.

### Step 6: Verify Context Reception
Ask Claude the same question Codex was working on:
- "What's the current state of feature X?"
- "Where did we leave off?"

If Claude references specific file paths, decisions, or errors from the Codex session → continuation context was read.

### Step 7: Chain Test (Optional)
Repeat the flow:
```bash
# From Claude → Grok
voyager switch grok --no-launch
grok "<bundle>"

# Back to Codex
voyager switch codex --no-launch
codex "<bundle>"
```

Track that WorkThread ID never changes across the chain.

---

## Verification Checklist

Mark each item as ✅ or ❌ after running:

| Check | Expected | Your Result |
|-------|----------|-------------|
| Source session recorded | Native session ID captured before switch | |
| WorkThread maintained | Same thread ID throughout chain | |
| Bundle compilation | Continuation bundle generated automatically | |
| Pending attach created | `voyager thread show` lists pending | |
| Target launch | Agent launches with bundle prompt | |
| Auto-attach resolution | New session appears in thread members within 60s | |
| No manual handoff | Never executed `voyager handoff`/`merge`/`attach` | |
| Context preservation | Target agent knows prior state/file paths | |
| Lease transfer | No orphaned lease after switch | |
| Git dirty warning | Warning appeared if uncommitted changes exist | |

---

## Known Limitations

### ZCode Desktop Edition
- No CLI resume path
- Requires manual paste of bundle contents
- Auto-attach does not work (no native session registration)
- Status: **Explicit-only**

### Cursor / Kiro IDE-only
- No external CLI available
- Cannot test automatic continuity via CLI
- Status: **Unsupported** (for CLI workflows)

### DSH (Desktop Shell Helper)
- Has CLI resume path but requires specific installation
- Integration test available via fixture but real-world not verified

---

## Provider Capability Matrix (Based on Code Analysis + Fixture Tests)

| Provider | Index Ingest | Native Resume | Startup Discovery | Skill-triggered | MCP-triggered | Auto Attach | Direct Auto Verified? | Transcript Writer | Final Classification |
|----------|--------------|---------------|-------------------|-----------------|---------------|-------------|----------------------|-------------------|---------------------|
| **Codex** | ✅ Via adapter | ✅ `codex resume` | ❌ No native hook surface → **`N`** | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **No auto at startup** | ✅ Implemented | **`N`** — first-turn/Skill guidance only |
| **Claude Code** | ✅ Via adapter | ✅ `claude --resume` | ✅ Real native `SessionStart` hook written to `settings.json` → **`H`** | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **Registered, firing not yet observed** | ⏳ Not yet implemented | **`H`** — registered; `Y` not claimed |
| **Grok** | ✅ Via adapter | ✅ `grok -r` | ✅ Real native `SessionStart` hook written to `~/.grok/hooks/` → **`H`** | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **Registered; firing observed 2026-09-25** | ✅ Implemented | **`H`** — registered; `Y` not claimed |
| **DSH** | ✅ Via adapter | ✅ `dsh --resume` | ❌ No native hook → **`N`**; launcher + session watcher | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **Best-effort** | ✅ Implemented | **`N`** — launcher path; real env not verified |
| **ZCode** | ✅ Via SQLite | ❌ Desktop only | ❌ No CLI → **`N`** | ❌ No SKILL.md | ❌ No tool | ❌ Manual attach | **No** | ❌ N/A | **Explicit-only** (manual paste required) |
| **Cursor** | ✅ Via SQLite | ❌ IDE only | ❌ No CLI → **`N`** | ❌ No SKILL.md | ❌ No resume | ❌ Manual attach | **No** | ❌ N/A | **Unsupported** (no CLI surface) |
| **Kiro** | ✅ Via JSON | ❌ IDE only | ❌ No CLI → **`N`** | ❌ No SKILL.md | ❌ No resume | ❌ Manual attach | **No** | ❌ N/A | **Unsupported** (no CLI surface) |

`claude` and `grok` set `has_startup_hook = True` in `PROVIDER_CONFIG` and register a real native `SessionStart` hook; the other providers return `N` for the startup column because the platform has no hook surface to register into. `H` is deliberately *not* `Y`: the letter is computed from static capability and configuration, so it has no way to represent a live observation. Both providers' triggers *have* now been observed (2026-09-24 / 2026-09-25) — the letter simply cannot say so.

### Classification Criteria
- **`Y` (zero-touch verified live)**: a human has observed the provider invoking Voyager at session start with no user command. **The observation now exists for Claude Code (2026-09-24), but nothing prints `Y` — see *Live verification*.**
- **`H` (hook registered)**: the installer wrote a schema-valid hook and `voyager integrate status` reports it. This is all the CLI can see; it says nothing about whether the provider fired.
- **`N` (no hook)**: the platform exposes no session-start hook surface. Continuity is still available through `voyager switch`, MCP tools, or Skill guidance.
- **Best-effort**: continuity works via MCP/Skill/launcher, but no startup automation is claimed.
- **Explicit-only**: requires manual paste of the bundle; no automation possible.
- **Unsupported**: no external CLI, cannot participate in CLI-based continuity.

> Note: pytest passing is **not** evidence of live zero-touch. Hermetic tests use synthetic fixtures and never touch a real provider. Only a manual observation can establish the trigger — and even then the CLI letter stays `H`, because no persisted live-evidence state exists to move it.

---

## Running This Procedure

Execute manually in your own environment:

```bash
# Clone project first
git clone https://github.com/HarryHeYu/sessionFlow && cd sessionFlow

# Install dependencies
pip install -e ".[mcp]"

# Run manual procedure above
voyager --help
python -m pytest tests/ -q  # For hermetic verification
```

The **automated tests** guarantee that all core logic is correct. This manual procedure validates that real agents can actually consume the output.

---

## Closing the remaining gap

The scratch-home run verifies everything except the provider's own behaviour,
without touching your real profile:

```bash
# Point the verifier at a scratch home and let it install + run the hook.
voyager integrate install claude --home "$SCRATCH"
python scripts/verify_claude_sessionstart.py --home "$SCRATCH"
```

That checks settings.json, the matcher, path resolution, the shell invocation,
exit code, the `hookSpecificOutput` protocol, and the 10 000-char cap. It
proves the handler works **when called** — it cannot prove Claude Code *calls*
it, because that is the provider's behaviour, not ours.

The provider-behaviour half is therefore a manual observation:

```bash
# Ask Claude Code itself to run SessionStart headlessly, then inspect it.
claude --debug hooks --init-only
python scripts/verify_claude_sessionstart.py --e2e
```

The criterion: `claude --init-only` exits 0 **and** the hook's own log shows an
entry whose `session_id` is a real Claude Code session (not the synthetic
`voyager-verify-0001`) ⇒ `SESSIONSTART_TRIGGER_LIVE_VERIFIED = true`.

> Do not promote on the strength of a green test suite or a passing simulation.
> pytest uses synthetic fixtures; the scratch-home simulation uses a synthetic
> `session_id`. Neither observes the provider.

### Live verification (2026-09-24) — done

Both halves of that criterion held on a real machine, and the chain ran all the
way through:

| Step | Evidence |
|---|---|
| provider fires the hook | `SessionStart_parsed` `session_id=3ae4d2b8-83b8-488e-94bd-3c27147b9782`, `cwd=E:\code\voyager` |
| continuity runs | `thread_id=thr_0854d50b88`, `attach_status=pending_resolve`, `context_length=79886`, `context_source=fresh_compile` |
| pending persisted | `thread_pending.native_session_id=3ae4d2b8-…`, `note='native session start: awaiting index'` |
| transcript discovered | a real interactive session wrote `~/.claude/projects/E--code-voyager/08563967-36f7-48e4-bd62-ed786ccfbd53.jsonl` |
| indexed | `claude:08563967-36f7-48e4-bd62-ed786ccfbd53`, `repo_root=E:/code/voyager` |
| pending resolved | `status='resolved'`, `resolved_sid='claude:08563967-…'` |
| attached to the WorkThread | `thread_sessions` row `(thr_0854d50b88, claude:08563967-…, ord=2)` |

```text
LIFECYCLE_TRIGGER_LIVE_VERIFIED    = true
SESSION_DISCOVERY_LIVE_VERIFIED    = true
SESSION_ATTACH_LIVE_VERIFIED       = true
CONTEXT_INJECTION_LIVE_VERIFIED    = false
SESSIONSTART_TRIGGER_LIVE_VERIFIED = true
```

**`CONTEXT_INJECTION_LIVE_VERIFIED` stays `false`.** The hook emitted an
`additionalContext` payload of 9 000 chars (full bundle 79 886 chars, spilled to
`~/.voyager/context/`), but nothing here shows the *model* read and used it.
Establishing that needs the private-sentinel test: plant a marker that exists
only in the session history, then ask the agent about it without naming it.

**Why the CLI still prints `H`.** `voyager integrate status` derives the letter
from static capability and configuration. Voyager keeps **no persisted
live-evidence state**, so the CLI cannot reflect a manual observation, and
nothing prints `Y`. Making `Y` a product state means designing a persisted
verification record deliberately — not having the CLI read
`provider-hooks.jsonl` at runtime and treat a log as a database.

**Zero-Touch Final Acceptance is CLOSED (2026-09-25).** The cross-provider leg
ran live: a private sentinel written only in Claude was recovered by a normally
launched Grok session, and that native Grok session attached itself to the same
WorkThread with no manual scan. `CONTEXT_INJECTION_LIVE_VERIFIED`,
`CROSS_PROVIDER_INVISIBLE_CONTINUITY` and `FULL_CONTINUITY_LIVE_VERIFIED` are all
**true**. Session ids and the state-transition evidence are in
[`grok_continuity_verdict.md`](../grok_continuity_verdict.md#final-acceptance--closed-2026-09-25).

---

## Summary

- **Automated verified** — current collection `381`. Sandbox gate, measured with the delete-guard shim dropped: `381 collected → 378 passed, 2 skipped, 1 deselected, 0 failed` (deselected: `tests/test_switch.py::test_switch_warns_on_dirty_repo`, an environment effect — see the changelog). Simulated core-only: `381 collected → 363 passed, 18 skipped, 0 failed`. The 2 full-dev skips are data-dependent reads of the default local index; core-only adds 16 dependency-gated skips (`mcp` / `zstandard` / `PIL`).
- **Real-provider runtime status** (as of 2026-09-25):
  * Claude Code = **`H`** (CLI letter) / **trigger live-verified** — a schema-valid native `SessionStart` hook is registered by the installer, and the provider **has been observed firing it** (2026-09-24). `SESSIONSTART_TRIGGER_LIVE_VERIFIED = true`. The letter stays `H` because the CLI has no persisted live-evidence state.
  * Codex = **`N`** — no native hook surface; first-turn/Skill guidance only
  * Grok = **`H`** (CLI letter) / **zero-touch live-verified** — a native `SessionStart` hook records the pending attach with the `GROK_SESSION_ID` Grok provides, and `$GROK_HOME/rules/` carries the continuation context into an interactive session. The cross-provider acceptance ran on this path. The letter is `H` for the same reason Claude's is: the status command derives it from static capability and configuration, and Voyager keeps no persisted live-evidence record.
  * DSH = **`N`** — launcher + session watcher; real environment not verified
- **Provider-limited**: ZCode desktop, Cursor/Kiro IDE-only
- **MCP registration**: Fully automated, no manual setup required
- **Zero-Touch Final Acceptance: CLOSED / PASS (2026-09-25)** — `CONTEXT_INJECTION_LIVE_VERIFIED`, `CROSS_PROVIDER_INVISIBLE_CONTINUITY` and `FULL_CONTINUITY_LIVE_VERIFIED` are **true**. No provider prints `Y`: that letter would require a persisted live-evidence record, which Voyager deliberately does not keep.

The product goal "user doesn't re-explain prior context when switching agents" now
holds **with no Voyager command at the handoff**: a private sentinel written only
in the Claude WorkThread was recovered by a normally launched Grok session
together with the prior task state, and that native Grok session attached itself
to the same WorkThread without a manual scan. Session ids and the state-transition
evidence are in
[`grok_continuity_verdict.md`](../grok_continuity_verdict.md#final-acceptance--closed-2026-09-25).
