---
name: voyager
description: >
  Query and continue work across every AI coding agent on this machine
  (Codex, Claude Code, ZCode, DSH, Grok, Cursor, Kiro, Antigravity) via
  the voyager CLI. Local-only index; never writes provider session files.
---

# Voyager — cross-agent session continuity

You (the agent reading this) share the machine with other AI coding
agents. Their past sessions are indexed locally by `voyager`. Use the
CLI below instead of guessing about past work — it is fast, local and
provenance-bound.

## When to use what

| User says / needs | Do this |
|---|---|
| "what are my agents doing lately" / recent activity | `voyager brief` |
| "where did we handle X" / find a fact | `voyager search "X"` |
| "what did agent Y do on repo Z" | `voyager list --platform Y --repo Z` |
| read one session in detail | `voyager show <id>` (add limits, see NEVER) |
| what did everyone do on this project | `voyager repo <path>` |
| continue recent work here | `voyager continue` (add `--launch` when the user wants it started) |
| continue from several sessions | `voyager continue --from id1,id2` |
| hand this task to another agent | `voyager handoff <id> --to <agent>` / `voyager merge ... --to <agent>` |
| switch agent and keep working | `voyager switch <agent>` (when available) |
| with a specific objective | add `--goal "..."` to handoff / merge / continue |
| large context, keep it small | add `--budget compact` (or `balanced` / `full` / `Nk`) |

## Startup protocol (automatic continuity)

At the beginning of work in a repository, BEFORE asking the user to repeat
prior context:

1. Call `voyager_startup` MCP tool with your provider name and session id:
   ```
   voyager_startup(provider="YOUR_PROVIDER", cwd="$PWD", 
                   native_session_id="YOUR_SESSION_ID")
   ```

2. If it returns `continuity_available=true`:
   - Read the returned `context` field — it contains goal, state, decisions,
     failures, next steps from previous sessions on this WorkThread
   - Check `attach_status` to see if you were auto-attached or need manual attach

3. If `auto_attach_reason` indicates success, your session is already linked
   to the WorkThread and you can continue immediately.

4. If `ambiguity_error` is present (multiple active threads), do NOT guess —
   ask the user which thread to use.

5. If `continuity_available=false`, no prior WorkThread exists; start fresh.

6. Never create a parallel WorkThread when one already exists for this repo.

### Startup continuity result fields

- `continuity_available`: is there an active WorkThread for this repo?
- `thread_id`: the active WorkThread identifier
- `goal`: the task objective from the original session
- `previous_provider/session`: who was working before and what session id
- `current_provider/session`: your identity as the new worker
- `lease_state`: whether the thread is held/expired/stale
- `attach_status`: attached | pending | error_* | unattached
- `context`: ranked continuation bundle (if available)
- `context_stale`: should we recompile from source sessions?
- `recommended_action`: explicit instructions (attach | switch-agent | proceed)

## Rules

1. Prefer `voyager brief` / `voyager search` over `voyager list` dumps —
   they are one-shot digests.
2. `voyager show <id>` prints a bounded timeline; that is the right dose.
   NEVER dump a full `voyager export` into the conversation — export to a
   file and point the user at it instead.
3. NEVER write into any agent's session directory
   (`~/.codex/sessions`, `~/.claude/projects`, `~/.grok/sessions`,
   `~/.dsh/sessions`, ...). Voyager reads them; nothing else may touch them.
4. NEVER bypass or force-release a WorkThread lease — `--steal` is for the
   user to type, not for you to decide.
5. Bundles/packages are files: hand over the path (`~/.voyager/bundles/...`),
   do not inline more than the ranked-evidence section.
6. Index freshness is handled automatically (`voyager watch` or the
   built-in pre-compile scan) — do not run `scan --force` "to be safe".

## Cross-agent handoff

When the user says "switch to Codex" / "交给 Codex" / "hand this to Claude":

- `voyager switch <agent>` — resolves the active WorkThread, handles the
  lease, compiles the continuation and launches the target.
- Add `--goal "..."` and `--budget compact` as needed.
- Do NOT explain handoff parameters to the user — just run the switch.

## Notes

- Session ids accept unique prefixes.
- `voyager thread ...` manages WorkThreads (list/show/create/attach/close/
  unlock). Threads are the task-centric view over multiple sessions.
- The index is local (`~/.voyager/index.db`); there is no cloud sync.
