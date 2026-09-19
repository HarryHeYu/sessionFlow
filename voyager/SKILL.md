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

When starting work in a repository, BEFORE responding to the user:

1. Run `voyager status` (or `voyager status --json`) — if it reports
   READY or PENDING_ATTACH, an active WorkThread exists for this repo.
2. If a continuation bundle is available (`voyager_context` MCP tool or
   `voyager continue --no-launch`), read it — it contains the goal,
   decisions, failures and next steps from previous sessions.
3. If the tool reports your session id is unattached and the repo
   matches, attach yourself: `voyager thread attach <thread-id> <your-id>`
   (or it may have been auto-attached already — check `voyager status`).
4. Do NOT create a parallel WorkThread for the same repo; use the
   existing one.

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
