# Real-Provider Dogfood Testing Guide

This document provides step-by-step instructions for testing Voyager's zero-touch startup continuity with actual Claude Code and Codex agents.

## Prerequisites

Before testing, ensure **MCP auto-registration works**:

```bash
# Install integration (this creates config files automatically)
voyager integrate install codex --force
voyager integrate install claude --force

# Check status - should show registered
voyager integrate status
```

Expected output:
```
Provider          Skill  MCP      Startup    Auto
----------------------------------------------------------------------
Codex             Y      R        N          Y
Claude            Y      R        H          Y
```

Legend: **R** = registered, **H** = native hook registered (live trigger unverified), **A** = startup-assisted, **N** = no hook. Only **Y** would mean the provider fires the hook by itself — nothing claims that yet.

`H` appears for Claude only once `voyager integrate install claude` has actually written the hook into `~/.claude/settings.json`. If it prints `A` or `N` instead, the install did not land — re-run it and inspect the settings file.

## Test Philosophy

**Zero-Touch Goal:** An Agent starts in a repo → automatically discovers existing WorkThread → loads session context without any `voyager` command.

**Reality Check (corrected 2026-09-23):** The previous revision of this section claimed "neither platform provides verified automatic startup triggers" and blamed the platforms. That reasoning was **wrong**. Claude Code does support native `SessionStart` hooks; the reason nothing fired was a bug in Voyager's own installer — `ClaudeIntegration.install()` emitted an invented flat schema and was never wired into `voyager integrate install` in the first place. That is now fixed, and a schema-valid hook is registered. Current honest state:

- ✅ Core functionality COMPLETE (discovery + attach + bundle compilation)
- ✅ Claude hook **registered** — handler verified, schema verified, payload protocol verified
- ❌ **Provider firing the hook is still unobserved** → `SESSIONSTART_TRIGGER_LIVE_VERIFIED = false`
- 📊 Claude = **`H`**; every other provider = **`N`** (no hook surface). Zero-Touch Final Acceptance remains **OPEN**.

## Test Scenario 1: Claude → Codex Continuity

### Step 1: Create Initial Session in Claude Code

```bash
cd /path/to/test-repo
# Start Claude Code manually
claude-code
```

Inside Claude Code session:
```
I'm going to implement feature X. Let me start by creating a new file.
```

**Record:**
- Native session ID (from Claude Code UI or logs)
- Repository path
- Feature being worked on

### Step 2: Let Claude Make Some Changes

Have Claude code do something substantive:
- Create a file
- Commit changes
- Make multiple edits

Let it work for 5-10 minutes with visible activity.

### Step 3: Switch to Codex WITHOUT Any voyager Commands

Close Claude Code session completely.

Open terminal in SAME repository:

```bash
cd /path/to/test-repo
# START CODEX DIRECTLY - NO voyager COMMANDS!
codex
```

### Step 4: Observe Codex Behavior

Watch what happens when Codex starts:

**Expected Zero-Touch (NOT yet verified):**
- Codex calls `voyager_startup` tool automatically at session start
- Context appears immediately in conversation
- No need to re-explain "what were we working on?"

**Actual Expected Behavior:**
- Codex needs instruction to call `voyager_startup`
- Check if Skill file is loaded (instructs Codex how to use Voyager)

**How to check:**
Look at Codex tool trace if available, or ask Codex directly:
```
What's your current context about?
```

If you get an immediate relevant answer → ✅ CONTEXT LOADED

If you need to say "use voyager_brief" first → ⚠️ MANUAL SETUP REQUIRED

### Step 5: Verify Session Attachment

In Codex, after it loads context:

```
Check if my Claude Code session is still attached.
Run: voyager thread list
```

**Expected:** Both Claude Code AND Codex appear in same WorkThread.

### Step 6: Verify Lease State

```
voyager lease show
```

Should show:
- Thread has lease held by both sessions
- No conflict between platforms

## Test Scenario 2: Cross-Agent Handoff Without Commands

### Setup

1. Start Claude Code in repo `/project`
2. Do some work (create files, commit)
3. Note session ID

### Manual Continuation in Codex (no voyager commands)

Instead of running `voyager continue`, just START Codex:

```bash
cd /project
codex
```

Then in Codex conversation:

**Option A: Ask Claude to transfer work**
```
"I'm in Codex now. Please continue Claude's work on [feature name]"
```

**Option B: Use Skill routing**
```
"Continue from Claude Code"
```

### What to Record

For each test run, log:

| Test | Platform Started | Automatic Discovery | Context Loaded | Manual Intervention Needed |
|------|------------------|---------------------|----------------|----------------------------|
| C→Cd | Claude           | ?                   | ?              | ?                          |
| Cd→C | Codex            | ?                   | ?              | ?                          |

## Verification Checklist

After each test run, verify these items:

- [ ] Skill file installed: `~/.{platform}/skills/voyager/SKILL.md`
- [ ] MCP registered: Config file contains Voyager entry
- [ ] Bootstrap instructions present: `voyager_{platform}_bootstrap.md`
- [ ] WorkThread exists: `voyager thread list` shows active thread
- [ ] Multiple sessions can attach: Add Codex to existing Claude thread

## Known Limitations

### Platform Limitations

1. **Codex CLI**
   - No native session-start hook surface — there is nothing for the installer to register into
   - Must rely on agent reading Skill file and following instructions
   - Status: **`N`**

2. **Claude Code**
   - Native `SessionStart` hooks **are** supported, and Voyager now registers one (`hooks.SessionStart[].hooks[]`, `type: command`, matcher `startup`)
   - The handler's output protocol (`hookSpecificOutput.additionalContext`), exit codes, and the 10 000-UTF-16-unit payload cap are all covered by tests
   - What is missing is only the live observation: no run has yet confirmed the provider actually invokes it
   - Status: **`H`** (registered, trigger unverified)

3. **Grok CLI / DSH**
   - No startup hooks at all
   - Status: **`N`** (best-effort via launcher shim / Skill guidance)

### What's Actually Verified

✅ **Voyager core functionality**:
- Thread discovery based on repo path
- Multi-session attachment logic  
- Context bundle compilation via ranker
- Budget-aware pruning
- Lease management across providers

❌ **Not yet verified**:
- Actual automatic invocation at agent startup
- Real-time session-to-thread linking
- Zero user intervention required

## Next Steps After Testing

### If Tests Succeed (Zero-Touch Works)

Update documentation:
```markdown
| Provider | Skill | MCP | Startup | Verification |
|----------|-------|-----|---------|--------------|
| Claude   | Y     | R   | Y       | ✓ observed   |
```

Only a provider whose hook was **observed firing** may be marked `Y`. Update `README.md` and `docs/DOGFOOD.md` to reflect that.

### If Tests Require Manual Intervention (Likely)

This IS acceptable! Update honestly:

```markdown
| Provider | Skill | MCP | Startup | Verification                        |
|----------|-------|-----|---------|-------------------------------------|
| Claude   | Y     | R   | H       | Hook registered, firing unobserved  |
| Codex    | Y     | R   | N       | No hook surface; Skill guidance     |
```

`H` is the honest resting state for a provider whose hook is registered but never seen firing. Do **not** promote it to `Y` on the strength of a green test suite — pytest uses synthetic fixtures and proves the handler, not the provider.

Document WHAT manual step is required:
- "Agent must read Skill file"
- "User must invoke voyager_startup tool"
- "Skill routing recommended but not automatic"

## Important Notes

**DO NOT** claim zero-touch until:
- You observe `voyager_startup` called WITHOUT user typing any voyager command
- Context appears naturally in conversation flow
- Agent says something like "Continuing from previous session..." automatically

**DO** document what actually happens, even if it requires manual steps. The product goal is "context not lost" not necessarily "zero commands".

## Reporting Results

Create a summary file after testing:

```markdown
# Dogfood Results - YYYY-MM-DD

## Tests Run

### Claude → Codex
- Date: 
- Repos: 
- Duration Claude work: 
- Duration Codex session: 

### Observations

Did Codex auto-discover Claude session? ☐ Yes ☐ No ☐ Unclear

Was context loaded immediately? ☐ Yes ☐ No ☐ Unclear

Manual steps taken:
1. ...
2. ...

Conclusion: STARTUP_*_STATUS

## Recommendations

Based on observations:
- ...
```

## Contact

Questions about this process? Open an issue at `github.com/HarryHeYu/sessionFlow/issues`.