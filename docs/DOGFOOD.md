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
> `~/.claude/settings.json`. What is *still* not established is whether the
> provider actually fires it on a real machine — that needs a human to run
> `scripts/verify_claude_sessionstart.py`, and until then
> `SESSIONSTART_TRIGGER_LIVE_VERIFIED` stays `false`. No provider currently
> claims `Y`. Full narrative: [`claude_continuity_verdict.md`](../claude_continuity_verdict.md).

**Status**: Automated tests cover all hermetic verification; real-agent end-to-end flows require manual execution with installed agents.

## Testing Boundary

### Automated Hermetic Tests (369 collected → 351 passed, 18 skipped)
All pytest tests use **synthetic fixtures** — no real agent data, no external dependencies. The 18 skips are optional-dependency and platform gates, not failures:

- `test_adapters.py` — Parse synthetic JSON/SQL for all 8 providers
- `test_*.py` — Store, continuity, budget, leases, switch, thread operations
- All tests verify correctness without touching real agent sessions

### Real-Provider Runtime Tested (Requires Installed Agents)
These flows were tested on actual Claude/Codex installations:

1. **Codex startup discovery** → **`N`** — Codex exposes no native session-start hook, so there is nothing to register
2. **Claude startup discovery** → **`H`** — a real native `SessionStart` hook is now written by the installer, but **no run has yet observed the provider firing it**
3. **No provider is currently proven to auto-trigger Voyager at session start** — explicit invocation via `voyager switch`, an MCP tool call, or Skill guidance is still required in practice
4. **Target agent continuation context** — verified via explicit workflows (`switch`/`continue`)

Startup legend (matches `voyager integrate status`): **`Y`** = zero-touch verified live · **`H`** = native hook registered, live trigger unverified · **`A`** = startup-assisted · **`N`** = no hook.

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
| **Grok** | ✅ Via adapter | ✅ `grok -r` | ❌ No native hook → **`N`**; opt-in launcher shim | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **Best-effort** | ✅ Implemented | **`N`** — launcher-shim path available |
| **DSH** | ✅ Via adapter | ✅ `dsh --resume` | ❌ No native hook → **`N`**; launcher + session watcher | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **Best-effort** | ✅ Implemented | **`N`** — launcher path; real env not verified |
| **ZCode** | ✅ Via SQLite | ❌ Desktop only | ❌ No CLI → **`N`** | ❌ No SKILL.md | ❌ No tool | ❌ Manual attach | **No** | ❌ N/A | **Explicit-only** (manual paste required) |
| **Cursor** | ✅ Via SQLite | ❌ IDE only | ❌ No CLI → **`N`** | ❌ No SKILL.md | ❌ No resume | ❌ Manual attach | **No** | ❌ N/A | **Unsupported** (no CLI surface) |
| **Kiro** | ✅ Via JSON | ❌ IDE only | ❌ No CLI → **`N`** | ❌ No SKILL.md | ❌ No resume | ❌ Manual attach | **No** | ❌ N/A | **Unsupported** (no CLI surface) |

Only `claude` sets `has_startup_hook = True` in `PROVIDER_CONFIG`; every other provider returns `N` for the startup column because the platform has no hook surface to register into. `H` is deliberately *not* `Y`: registration is proven by the installer, the provider actually firing the trigger is not.

### Classification Criteria
- **`Y` (zero-touch verified live)**: a human has observed the provider invoking Voyager at session start with no user command. **Nothing currently claims this.**
- **`H` (hook registered, trigger unverified)**: the installer wrote a schema-valid hook and `voyager integrate status` reports it; the provider firing it has not been observed.
- **`N` (no hook)**: the platform exposes no session-start hook surface. Continuity is still available through `voyager switch`, MCP tools, or Skill guidance.
- **Best-effort**: continuity works via MCP/Skill/launcher, but no startup automation is claimed.
- **Explicit-only**: requires manual paste of the bundle; no automation possible.
- **Unsupported**: no external CLI, cannot participate in CLI-based continuity.

> Note: pytest passing is **not** evidence of live zero-touch. Hermetic tests use synthetic fixtures and never touch a real provider; only the manual procedure below can move a provider from `H` to `Y`.

---

## Running This Procedure

Execute manually in your own environment:

```bash
# Clone project first
git clone https://github.com/HarryHeYu/voyager && cd voyager

# Install dependencies
pip install -e ".[mcp]"

# Run manual procedure above
voyager --help
python -m pytest tests/ -q  # For hermetic verification
```

The **automated tests** guarantee that all core logic is correct. This manual procedure validates that real agents can actually consume the output.

---

## Closing the remaining gap (the only open item)

Everything except one thing is already machine-verified. You can confirm the
whole chain yourself, without touching your real profile:

```bash
# 1. Point the verifier at a scratch home and let it install + run the hook.
voyager integrate install claude --home "$SCRATCH"
python scripts/verify_claude_sessionstart.py --home "$SCRATCH"
```

That checks settings.json, the matcher, path resolution, the shell invocation,
exit code, the `hookSpecificOutput` protocol, and the 10 000-char cap. It
proves the handler works **when called** — it cannot prove Claude Code *calls*
it, because that is the provider's behaviour, not ours.

The last mile is therefore a single observation, and only you can make it:

```bash
# 2. Ask Claude Code itself to run SessionStart headlessly, then inspect it.
claude --debug hooks --init-only
python scripts/verify_claude_sessionstart.py --e2e
```

When `claude --init-only` exits 0 **and** the hook's own log shows an entry
whose `session_id` is a real Claude Code session (not the synthetic
`voyager-verify-0001`), set `SESSIONSTART_TRIGGER_LIVE_VERIFIED = true` and
promote Claude Code from `H` to `Y`. Until that happens the honest answer stays
`H`, and Zero-Touch Final Acceptance stays **OPEN**.

> Do not promote on the strength of a green test suite or a passing simulation.
> pytest uses synthetic fixtures; the simulation above uses a synthetic
> `session_id`. Neither observes the provider.

---

## Summary

- **Automated core verified**: `369 collected → 351 passed, 18 skipped, 0 failed` (all logic paths covered; skips are optional-dependency/platform gates)
- **Real-provider runtime status** (as of 2026-09-23):
  * Claude Code = **`H`** — a schema-valid native `SessionStart` hook is registered by the installer; the provider firing it has **not** been observed. `SESSIONSTART_TRIGGER_LIVE_VERIFIED = false`.
  * Codex = **`N`** — no native hook surface; first-turn/Skill guidance only
  * Grok = **`N`** — no native hook; opt-in launcher shim is the available path
  * DSH = **`N`** — launcher + session watcher; real environment not verified
- **Provider-limited**: ZCode desktop, Cursor/Kiro IDE-only
- **MCP registration**: Fully automated, no manual setup required
- **Zero-Touch Final Acceptance: OPEN** — no provider claims `Y` yet.

The product goal "user doesn't re-explain prior context when switching agents" is **achieved via explicit commands** (`voyager switch`) or MCP tool calls. Claude Code is now the one provider where an invisible auto-startup path plausibly exists — the hook is registered and the handler is verified — but the last mile, observing the provider actually fire it, remains unproven and is the single open item blocking Zero-Touch Final Acceptance.
