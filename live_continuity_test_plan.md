# Live Continuity Test Plan

**Date**: 2026-09-21  
**Goal**: Verify cross-agent invisible continuity end-to-end

---

## Prerequisites Setup

### 1. Create Dedicated Test Repo

```bash
cd /tmp/voyager-cross-provider-test
git init
echo "# Cross-Provider Continuity Test" > README.md
git add . && git commit -m "Init"

# Create exactly ONE active WorkThread with sentinel
voyager thread create \
    --title "cross-provider-test" \
    --goal "Test Claude→Grok invisible continuity" \
    --repo "$(pwd)"

# Attach prior ZCode session containing sentinel marker
voyager thread attach thr_xxx zcode:sess_with_sentinel_marker
```

**Expected state**:
- Exactly one active WorkThread in this repo
- Thread contains: `VOYAGER_GROK_SENTINEL_2026` or similar known marker
- Thread has at least 1+ member session with conversation history

---

## Phase 1: Claude Native SessionStart Trigger

### Setup

**Verify settings.json configuration**:
```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "python -m voyager.integrations.claude_session_start"
      }]
    }]
  }
}
```

### Test Procedure

1. **Close all existing Claude instances**

2. **Launch Claude IDE normally** (double-click icon)
   - DO NOT manually invoke hooks
   - DO NOT mention "Voyager" in first message
   - Navigate to `/tmp/voyager-cross-provider-test` directory in Claude

3. **Type ordinary task prompt** (e.g., "check project structure")
   - User prompt does NOT contain Voyager/sentinel mentions

4. **Observe within 30 seconds**:

**Checklist**:
- [ ] Does Claude output "[Voyager Continuation] ===" in initial context area?
- [ ] If yes, does continuation contain meaningful prior work content?
- [ ] Does first model response reference prior ZCode conversation?
- [ ] Can Claude state knowledge of sentinel marker from prior session?
- [ ] Did new Claude native session file get created?

### Post-Test Verification

```bash
# Check for new Claude session
ls ~/.claude/sessions/*/chat_history.jsonl 2>/dev/null | tail -3

# Verify session is attached to WorkThread
voyager scan --provider claude --force

voyager thread show <thread-id> | grep claude
# Should show newly created Claude session with matching ID
```

### Success Criteria

| Criterion | Pass | Fail | Skip |
|-----------|------|------|------|
| Hook auto-invokes without manual trigger | Yes | No | N/A |
| Context generated correctly | Visible JSON/Markdown | Error/Empty | N/A |
| Output visible in Claude's context window | Yes | No | N/A |
| Model references prior sentinel/task | Yes | No | N/A |
| New native session created | Exists | Empty | N/A |
| Auto-attached to same WorkThread | Yes | No | N/A |

### Classification Path

If **ALL pass**:
```text
SESSION_START_TRIGGER_LIVE_VERIFIED = true
CONTEXT_INJECTION_LIVE_VERIFIED = true
SESSION_DISCOVERY_LIVE_VERIFIED = true
SESSION_ATTACH_LIVE_VERIFIED = true
FULL_CONTINUITY_LIVE_VERIFIED = true
```

Claude becomes first provider with full zero-touch continuity proven.

---

## Phase 2: Grok Headless Context Injection

### Setup

Same test repo as Phase 1 (or create fresh):
```bash
cd /tmp/voyager-grok-headless-test
git init
# ... same setup steps
```

### Test A: Prompt Mode

**Command**:
```bash
grok "请告诉我当前工作线程中正在处理什么任务"
```

Key constraints:
- User prompt does NOT contain "Voyager" keyword
- User prompt does NOT directly mention sentinel value
- Wrapper MUST intercept and inject context automatically

**Observation Points**:

1. **Temp file check** (if wrapper creates one):
```bash
ls -la /tmp/voyager_groktxt_*.txt 2>/dev/null | tail -1
cat /tmp/voyager_groktxt_*.txt
```
Should contain: `[Voyager Continuation] + prior work + user prompt`

2. **Grok response check**:
- First assistant response references prior workThread goal?
- Does it demonstrate awareness of sentinel marker without being told explicitly?

3. **Native session capture**:
```bash
# Capture sessionId from grok output if available
# Some builds support --output-format json which includes sessionId
```

### Test B: Interactive TUI Mode

**Command**:
```bash
grok
# Then in interactive mode type: check current workthread status
```

**Current implementation behavior**:
- Context temp file created but NOT injected into TUI session
- User just sees Grok TUI start normally
- No automatic context injection possible yet

**Expected result**:
```text
HEADLESS_INITIAL_PROMPT_ZERO_TOUCH = true
INTERACTIVE_TUI_ZERO_TOUCH = not_yet_supported
```

This is acceptable partial achievement. Don't count Notepad fallback as solution.

---

## Phase 3: Claude → Grok Cross-Provider Continuity

**This is THE critical acceptance test.**

### Setup

1. **Phase 1 passes**: Claude successfully demonstrated full continuity
   - Claude session attached to WorkThread X
   - Sentinel marker visible in Claude's context

2. **Close Claude completely**

3. **Ensure exactly one active WorkThread** (still exists, no other threads)

### Test Procedure

**User performs natural workflow**:
```bash
# Step 1: Open Claude (simulating Agent A completion)
claude
# Type: "complete this work on sentinel marker detection"
# Claude continues from prior context, completes task
# Claude session naturally attached to WorkThread X

# Step 2: User closes Claude, switches to Grok (Agent B)
# NO manual voyager commands
# NO copying/pasting context
# Just launching Grok normally

cd /path/to/test/repo
grok "继续刚才的工作，检查是否完成了 sentinel 检测"
```

**Critical Question**:
Does Grok's first response demonstrate:
- Knowledge that Claude just completed work on this WorkThread?
- Awareness of sentinel marker without user explicitly telling Grok?
- Natural continuation flow ("I see you're working on...")?

### Observation Checklist

- [ ] Grok wrapper intercepted launch
- [ ] Context injection via --prompt-file successful
- [ ] Response demonstrates awareness of:
  - Prior Claude session existence
  - Claude's work/product/conclusions
  - Sentinel marker without explicit re-statement
- [ ] New Grok native session created
- [ ] Session discoverable by Voyager adapter
- [ ] Session auto-attached to same WorkThread X
- [ ] WorkThread now shows: Claude → Grok as consecutive members

### Success Criteria

| Criterion | Pass | Fail |
|-----------|------|------|
| Grok knows prior Claude session existed | Yes | No |
| Grok knows Claude's conclusions/work | Yes | No |
| Sentinel awareness without explicit instruction | Yes | No |
| Native session created & discovered | Yes | No |
| Auto-attached to WorkThread X | Yes | No |

---

## Implementation Status Summary

### Claude Code

```text
SUPPORTED: yes
CONFIGURED: yes (python -m voyager.integrations.claude_session_start)
HOOK_HANDLER_VERIFIED: yes (manual invocation tested)
NATIVE_TRIGGER_LIVE_VERIFIED: PENDING (requires GUI launch test)
CONTEXT_INJECTION_LIVE_VERIFIED: PENDING
SESSION_ATTACH_LIVE_VERIFIED: PENDING
FULL_CONTINUITY_LIVE_VERIFIED: PENDING
```

**What's needed**: Manual Claude IDE launch observation

**Risk factors**:
- Anthropic CLI version compatibility
- Stdin/stdout contract interpretation
- Whether stdout actually enters model context (per official docs)

---

### Grok CLI

```text
SUPPORTED: yes
LAUNCH_PATH_LIVE_VERIFIED: yes
HEADLESS_CONTEXT_CHANNEL_IDENTIFIED: yes (--prompt-file)
CONTEXT_INJECTION_LIVE_VERIFIED: PENDING (needs real prompt test)
SESSION_DISCOVERY_LIVE_VERIFIED: PENDING
SESSION_ATTACH_LIVE_VERIFIED: PENDING
INTERACTIVE_TUI_ZERO_TOUCH: not_yet_supported
FULL_CONTINUITY_LIVE_VERIFIED: PENDING
```

**What's implemented**:
- Unified Python hook handler (`hook startup`)
- Temp file approach to handle large continuations
- Native `--prompt-file` channel for headless mode

**What's missing**:
- Real prompt test with sentinel verification
- Session ID capture & attach logic
- Interactive TUI zero-touch (currently unsupported)

---

## Next Steps Priority

1. **Execute Phase 1** - Manual Claude launch test
2. **Execute Phase 2A** - Grok prompt mode with sentinel
3. **Execute Phase 3** - Claude → Grok cross-provider (the real acceptance test)
4. **Document results** honestly, even if failures
5. **Only then claim** full zero-touch verified

Don't skip to Capabilities Audit or Production Ready labels.

---

## Evidence Collection Format

For each test run, produce:

```markdown
## Test Run: [date] [test-name]

### Environment
- OS: Windows/macOS/Linux
- Claude Version: x.x.x
- Grok Version: x.x.x
- WorkThread: thr_xxx with marker="..."

### Results
- Hook fired: YES/NO
- Context generated: YES/NO (attach sample)
- Model aware of context: YES/NO (attach response)
- Native session created: YES/NO (attach ID/path)
- Attached to WorkThread: YES/NO (attach thread state)

### Conclusion
PASS/FAIL/PARTIAL

### Notes
[Any relevant observations]
```

This enables reproducibility and honest assessment.
