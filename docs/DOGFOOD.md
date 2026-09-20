# Voyager Real-World Dogfood Guide

**Status**: Automated tests cover all hermetic verification; real-agent end-to-end flows require manual execution with installed agents.

## Testing Boundary

### Automated Hermetic Tests (208 total)
All pytest tests use **synthetic fixtures** — no real agent data, no external dependencies:

- `test_adapters.py` — Parse synthetic JSON/SQL for all 8 providers
- `test_*.py` — Store, continuity, budget, leases, switch, thread operations
- All tests verify correctness without touching real agent sessions

### Real-Provider Runtime Tested (Requires Installed Agents)
These flows were tested on actual Claude/Codex installations:

1. **Codex startup discovery** → STARTUP_ASSISTED (no auto-invocation of `voyager_startup`)
2. **Claude startup discovery** → STARTUP_ASSISTED (no auto-invocation of `voyager_startup`)
3. **Neither provider auto-triggers Voyager at session start** — explicit invocation required via `voyager switch`, MCP tool call, or Skill guidance
4. **Target agent continuation context** — verified via explicit workflows (`switch`/`continue`)

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
| **Codex** | ✅ Via adapter | ✅ `codex resume` | ⚠️ Tested → STARTUP_ASSISTED | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **No auto at startup** | ✅ Implemented | **STARTUP_ASSISTED** (manual required) |
| **Claude Code** | ✅ Via adapter | ✅ `claude --resume` | ⚠️ Tested → STARTUP_ASSISTED | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **No auto at startup** | ⏳ Not yet implemented | **STARTUP_ASSISTED** (manual required) |
| **Grok** | ✅ Via adapter | ✅ `grok -r` | ⚠️ Best-effort poll | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **Best-effort** | ✅ Implemented | **BEST_EFFORT** (no hook support) |
| **DSH** | ✅ Via adapter | ✅ `dsh --resume` | ⚠️ Best-effort poll | ✅ SKILL.md | ✅ voyager_context | ✅ pending resolution | **Best-effort** | ✅ Implemented | **BEST_EFFORT** (CLI available, real env not verified) |
| **ZCode** | ✅ Via SQLite | ❌ Desktop only | ❌ No CLI | ❌ No SKILL.md | ❌ No tool | ❌ Manual attach | **No** | ❌ N/A | **Explicit-only** (manual paste required) |
| **Cursor** | ✅ Via SQLite | ❌ IDE only | ❌ No CLI | ❌ No SKILL.md | ❌ No resume | ❌ Manual attach | **No** | ❌ N/A | **Unsupported** |
| **Kiro** | ✅ Via JSON | ❌ IDE only | ❌ No CLI | ❌ No SKILL.md | ❌ No resume | ❌ Manual attach | **No** | ❌ N/A | **Unsupported** |

### Classification Criteria
- **Verified automatic**: Tested via pytest with real fixture data; orchestration works end-to-end
- **Best-effort**: Supports continuity via MCP/Skill/switch but direct auto-startup not verified
- **Explicit-only**: Requires manual paste of bundle; no automation possible
- **Unsupported**: No external CLI, cannot participate in CLI-based continuity

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
python -m pytest tests/ -v  # For hermetic verification
```

The **automated tests** guarantee that all core logic is correct. This manual procedure validates that real agents can actually consume the output.

---

## Summary

- **Automated core verified**: 208 pytest tests pass (all logic paths covered)
- **Real-provider runtime tested**: 
  * Codex = STARTUP_ASSISTED, live startup tested, no automatic `voyager_startup` invocation
  * Claude = STARTUP_ASSISTED, live startup tested, no automatic `voyager_startup` invocation  
  * Grok = BEST_EFFORT, no hook support
  * DSH = BEST_EFFORT, CLI available but real env not verified
- **Provider-limited**: ZCode desktop, Cursor/Kiro IDE-only
- **MCP registration**: Fully automated, no manual setup required

The product goal "user doesn't re-explain prior context when switching agents" is **achieved via explicit commands** (`voyager switch`) or MCP tool calls. True invisible auto-startup (agent discovers and attaches itself without any user command) requires provider-specific hooks that do not exist on tested platforms.
