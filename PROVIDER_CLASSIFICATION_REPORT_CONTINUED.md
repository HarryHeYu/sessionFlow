# Provider Zero-Touch Capability Classification Report (Continued)

> ⚠️ **Superseded snapshot — not current status.** This is a point-in-time
> classification pass from 2026-09. Its `STARTUP_ASSISTED` / `BEST_EFFORT` labels
> (and the "provisional until verification" caveats on Cursor, Antigravity, etc.)
> predate the `Unreleased` fixes in [`CHANGELOG.md`](CHANGELOG.md). The Claude
> entry in particular assumed no hook surface existed; the real cause was
> Voyager's own installer writing an invented schema and never being invoked.
> Current state: **Claude Code = `H`** (native hook registered, live trigger
> unverified); **every other provider = `N`**. Narrative:
> [`claude_continuity_verdict.md`](claude_continuity_verdict.md).

---

### DSH → LAUNCHER_ZERO_TOUCH ✅

**Configuration:**
```bash
~/.dsh/skills/voyager/SKILL.md     # Skill guidance only
~/.voyager/bin/dsh                 # Launcher wrapper (opt-in)
~/.dsh/sessions/                   # Session file watcher target
```

**Capabilities Verified:**
- ✅ CLI launcher available (`dsh` executable)
- ✅ Skill system (~/.dsh/skills/voyager/SKILL.md)
- ❌ No MCP support
- ❌ No native session-start hook
- ℹ️ Session file polling may provide attach-only continuity

**Strategy:**
```
Launcher wrapper installed at ~/.voyager/bin/dsh
  → Sets VOYAGER_LAUNCHER_RUNNING env var
  → Runs: voyager launcher prelaunch --provider dsh --cwd "$PWD"
  → Launches real dsh with original args
  → Optional: session watcher monitors ~/.dsh/sessions/ for new files
  → Attaches new sessions to existing WorkThread
```

**Implementation:**
- File: `voyager/integrations/dsh.py`
- Install command: `voyager integrate install dsh`
- Creates launcher wrapper + optional watcher setup

**Notes:**
- Context injection unverified—may require manual paste for some workflows
- Attachment via file polling is viable

---

### ZCode → WATCHER_ATTACH_ONLY ⚠️ BLOCKED

**Configuration:**
```bash
~/.zcode/cli/db/db.sqlite          # Hardcoded path concern
Process + SQLite watcher           # Pending implementation
```

**Capabilities Verified:**
- ❓ Source discovery over-reliant on hardcoded paths
- ❓ Need dynamic environment variable fallback
- ❓ Desktop vs CLI split requires separate strategies

**Known Blockers:**
1. **ZCODE_SOURCE_DISCOVERY_BLOCKER**: `scan()` hardcodes `~/.zcode/cli/db/db.sqlite`
2. No proven process lifecycle introspection
3. Session row creation timing unknown

**Proposed Strategy:**
```
Watchdog thread:
  → Monitors ~/.zcode/*/db/*.sqlite for new rows
  → On new session row detected:
    - Extract repo/workspace field
    - Match to active WorkThread
    - Attach session
→ No proven context injection mechanism
```

**Implementation Status:**
- File: `voyager/integrations/zcode.py` (stub)
- Requires robust source discovery before full implementation
- Currently classified as `WATCHER_ATTACH_ONLY` pending blocker resolution

---

### Cursor → PENDING_VERIFICATION 🔍

**Configuration:**
```bash
~/.cursor/mcp.json                 # MCP support confirmed
Extension API                      # SESSION_START hook? TBD
```

**Capabilities Verified:**
- ✅ MCP support exists
- ✅ Built on VS Code extension platform
- ⚠️ Session-start hook capability UNVERIFIED

**Needs Investigation:**
1. Does Cursor expose `sessionStart` event via extension API?
2. What workspace/session metadata available in events?
3. Can we inject context via stdout in headless mode?

**Proposed Strategy:**
```
If Cursor provides sessionStart event:
  → SESSION_START_ZERO_TOUCH via extension listener
  → Extension invokes: voyager hook startup --provider cursor
  
If no event available:
  → Fall back to STARTUP_ASSISTED via MCP
```

**Implementation Status:**
- File: `voyager/integrations/cursor.py` (stub)
- Classification provisional: `STARTUP_ASSISTED` until verification
- Verification needed: Check Cursor extension docs and test real extension

---

### Kiro → IDE vs CLI Split 🔄

**Configuration:**
```bash
CLI:  ~/.voyager/bin/kiro         # Launcher possible
IDE:  ~/.kiro/workspace-session.json  # JSON file watcher
```

**Capabilities Verified:**
- ✅ CLI launcher available (if `kiro` in PATH)
- ✅ Workspace-session JSON exists (file watcher viable)
- ✅ Skill system (~/.kiro/skills/)

**Separate Strategies per Mode:**

**CLI Mode → LAUNCHER_ZERO_TOUCH:**
```
User runs: kiro <task>
  → Wrapper at ~/.voyager/bin/kiro intercepts call
  → Runs voyager prelaunch hook
  → Launches real kiro with original args
```

**IDE Mode → WATCHER_ATTACH_ONLY:**
```
Watchdog thread:
  → Monitors ~/.kiro/workspace-session.json for changes
  → On new session detected:
    - Parse session data from JSON
    - Match to active WorkThread by repo
    - Attach session
→ No proven injection mechanism
```

**Implementation Status:**
- File: `voyager/integrations/kiro.py`
- Reports both modes separately
- CLI mode higher priority (proven launcher approach)

---

### Antigravity → BEST_EFFORT 🔒

**Configuration:**
```bash
Protobuf-encoded SQLite          # Black-box storage format
No public schema known
```

**Capabilities Verified:**
- ❌ Storage format undocumented (heuristic decode only)
- ❓ Plugin API needs verification
- ❓ SQLite watcher viability unconfirmed

**Current Assessment:**
```
Adapter currently decodes protobuf blobs heuristically.
Without schema stability guarantee, watcher strategy risky.
Best effort via Skill guidance only.
```

**Strategy:**
- File: `voyager/integrations/antigravity.py`
- Classification: `BEST_EFFORT` until schema documentation or stable ABI found
- Priority: Monitor for official schema release

---

## Summary Table

| Provider | Level | Strategy | Status | Blocker/Note |
|----------|-------|----------|--------|--------------|
| **Claude** | FIRST_TURN_ZERO_TOUCH | CLAUDE.md + Skill | ✅ Implemented | Pending real dogfood |
| **Codex** | FIRST_TURN_ZERO_TOUCH | AGENTS.md + Skill | ✅ Implemented | Pending real dogfood |
| **Grok** | LAUNCHER_ZERO_TOUCH | Opt-in wrapper script | ✅ Implemented | |
| **DSH** | LAUNCHER_ZERO_TOUCH | Opt-in wrapper + watcher | ✅ Implemented | Context injection unverified |
| **ZCode** | WATCHER_ATTACH_ONLY | SQLite watcher | ⚠️ Blocked | Source discovery hardcoding |
| **Cursor** | STARTUP_ASSISTED | MCP + extension audit | 🔍 Pending | Session-start hook verification |
| **Kiro** | LAUNCHER/WORKER | CLI launcher + IDE watcher | ✅ Partial | Separate CLI/IDE modes |
| **Antigravity** | BEST_EFFORT | Skill guidance only | 🔒 Black-box | Schema undocumented |

---

## Installation Commands

All integrations now use unified CLI:

```bash
# Install complete integration for a provider
voyager integrate install <provider>

# Supported providers
voyager integrate install codex    # FIRST_TURN_ZERO_TOUCH
voyager integrate install claude   # FIRST_TURN_ZERO_TOUCH
voyager integrate install grok     # LAUNCHER_ZERO_TOUCH
voyager integrate install dsh      # LAUNCHER_ZERO_TOUCH
voyager integrate install zcode    # WATCHER_ATTACH_ONLY (blocked)
voyager integrate install cursor   # PENDING_VERIFICATION
voyager integrate install kiro     # CLI+IDE split
voyager integrate install antigravity  # BEST_EFFORT

# Check status for all providers
voyager integrate status

# Remove integration
voyager integrate remove <provider>
```

---

## Hook CLI Reference

New `voyager hook` command for provider-native invocation:

```bash
# Startup continuity handler
voyager hook startup \
  --provider <provider> \
  --cwd "$PWD" \
  --session-id "<session-id>" \
  --goal "primary goal"

# Exit codes:
#   0 - Success, context provided
#   1 - No active WorkThread found
#   2 - Ambiguous thread resolution
#   3 - Error during compilation
```

This enables providers with actual hooks to invoke Voyager automatically at appropriate lifecycle points.

---

## Next Steps (Priority Order)

1. **Claude + Codex real dogfood** - Verify instruction-following actually triggers `voyager_startup` before first turn
2. **Grok/DSH launcher testing** - Confirm wrapper scripts work correctly and don't break provider behavior
3. **ZCode source discovery fix** - Replace hardcoded path with environment-aware detection
4. **Cursor extension investigation** - Verify or disprove session-start hook availability
5. **Kiro IDE watcher** - Implement JSON file monitoring for desktop mode
6. **Antigravity schema monitoring** - Watch for official protocol documentation

---

## Conclusion

**Key Achievement**: This is the first truly **capability-driven** classification of Voyager's supported providers. We no longer assume universal zero-touch; instead we leverage each platform's strongest native surface:

```
Native Hook > First-turn instruction > Plugin > Launcher > Watcher > Assisted
```

The architecture is now ready for real-world verification across all 8 providers. Each has a clear installation path and documented strategy based on what the platform actually supports—not marketing assumptions.

**Testing Target**: Add comprehensive tests for all 8 providers' integration modules (expected: 208 → 250+ tests).
