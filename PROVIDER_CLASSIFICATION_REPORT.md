# Provider Zero-Touch Capability Classification Report

**Generated**: 2026-09-20  
**Method**: Real provider capability audit via `voyager/integrations/capabilities.py`  
**Status**: Complete audit of all 8 supported providers

> ⚠️ **Superseded snapshot — not current status.** The *hierarchy table* below is
> still the internal `ZeroTouchLevel` enum in
> `voyager/integrations/capabilities.py`, but the **per-provider classifications**
> further down are stale. In particular the audit hardcoded
> `has_session_start_hook = False` for Claude Code and therefore rated it
> `FIRST_TURN_ZERO_TOUCH`; that hardcode has been removed. Current state:
> **Claude Code = `H`** (real native `SessionStart` hook registered, live trigger
> unverified); **all other providers = `N`**. The user-facing legend is now
> `Y`/`H`/`A`/`N` as printed by `voyager integrate status`. See
> [`CHANGELOG.md`](CHANGELOG.md) and
> [`claude_continuity_verdict.md`](claude_continuity_verdict.md).

---

## Executive Summary

This report provides the first **real capability-based classification** of Voyager-supported providers for Zero-Touch Startup Continuity. No longer assuming universal behavior—we now classify each provider based on actual platform capabilities discovered through:

- Local `--help` output where available
- Existing config files on the system
- Official documentation references
- Executable existence checks
- Session lifecycle investigation

---

## Classification Hierarchy (Strongest → Weakest)

| Level | Description |
|-------|-------------|
| `SESSION_START_ZERO_TOUCH` | Native session-start hook; Voyager invoked automatically when agent spawns |
| `FIRST_TURN_ZERO_TOUCH` | Global/project instruction; Agent auto-calls Voyager before first user task |
| `LAUNCHER_ZERO_TOUCH` | Opt-in wrapper script provides continuity on launch |
| `WATCHER_ATTACH_ONLY` | Process/file watcher detects new sessions and attaches to WorkThread |
| `STARTUP_ASSISTED` | Requires explicit user invocation via MCP tool or prompt |
| `BEST_EFFORT` | Partial automation possible but limited by platform constraints |
| `UNSUPPORTED` | No automation surface available |

---

## Final Provider Classifications

### Claude Code → FIRST_TURN_ZERO_TOUCH ✅

**Configuration:**
```bash
~/.claude/CLAUDE.md    # Global instruction file
~/.claude/skills/      # Skill system support
~/.claude/mcp.json     # MCP server registration
```

**Capabilities Verified:**
- ✅ Global instruction via CLAUDE.md
- ✅ Skill system (~/.claude/skills/voyager/SKILL.md)
- ✅ MCP support (`~/.claude/mcp.json`)
- ❌ No verified native session-start hook in current version

**Strategy:**
```
Claude starts
→ Reads CLAUDE.md instructions
→ Before handling first user task:
   1. Call voyager_startup(provider="claude", cwd="$PWD")
   2. Query startup_continuity() for active WorkThread
   3. If found, incorporate context into first response
→ User never explicitly mentions "Voyager"
```

**Implementation:**
- File: `voyager/integrations/claude.py`
- Install command: `voyager integrate install claude`
- Installs Skill + CLAUDE.md instruction + MCP registration

**Verification Status:** Pending real dogfood test (instruction-following mechanism)

---

### Codex → FIRST_TURN_ZERO_TOUCH ✅

**Configuration:**
```bash
~/.codex/AGENTS.md     # Global instruction file
~/.codex/skills/       # Skill system support
~/.codex/config.toml   # MCP server registration
```

**Capabilities Verified:**
- ✅ Global instruction via AGENTS.md
- ✅ Skill system (~/.codex/skills/voyager/SKILL.md)
- ✅ MCP support (`config.toml`)
- ❌ No native session-start hook in current versions

**Strategy:**
```
Codex starts
→ Reads AGENTS.md instructions
→ Before handling first user task in repo:
   1. query Voyager startup continuity
   2. if an active WorkThread exists, incorporate that context
   3. do not ask the user to repeat context already available in Voyager
→ User never explicitly mentions "Voyager"
```

**Implementation:**
- File: `voyager/integrations/codex.py`
- Install command: `voyager integrate install codex`
- Installs Skill + AGENTS.md instruction + MCP registration

**Verification Status:** Pending real dogfood test (instruction-following mechanism)

---

### Grok CLI → LAUNCHER_ZERO_TOUCH ✅

**Configuration:**
```bash
~/.grok/skills/voyager/SKILL.md  # Skill guidance only
~/.voyager/bin/grok              # Launcher wrapper (opt-in)
```

**Capabilities Verified:**
- ✅ Skill system (~/.grok/skills/voyager/SKILL.md)
- ✅ CLI launcher available (`grok` executable)
- ❌ No MCP support
- ❌ No native session-start hook

**Strategy:**
```
User runs: grok <task>

Wrapper chain:
~/.voyager/bin/grok (if installed in PATH prefix)
  → Sets VOYAGER_LAUNCHER_RUNNING env var
  → Runs: voyager launcher prelaunch --provider grok --cwd "$PWD"
  → Forwards to: /usr/local/bin/gro