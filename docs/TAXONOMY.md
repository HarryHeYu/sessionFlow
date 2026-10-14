# Taxonomy

How this project classifies things, and where each classification is defined in
code. There is no second registry here: every category below names the enum, the
column or the tuple that already owns it. If a category and the code disagree,
the code is the contract and this file is the bug.

## 1. Provider

The eight providers sessionFlow integrates with, as the `provider` value used by
`--platform` and stored on every session.

| Provider | Agent | Source shape |
|---|---|---|
| `codex` | Codex (CLI / VSCode / Desktop) | rollout JSONL under `~/.codex/sessions/YYYY/MM/DD/` |
| `claude` | Claude Code | project JSONL under `~/.claude/projects/<munged>/` + file-history |
| `grok` | Grok CLI | `chat_history.jsonl` + `summary.json` |
| `zcode` | ZCode | SQLite (`~/.zcode/cli/db`) — one artifact, many sessions |
| `cursor` | Cursor | `state.vscdb` (SQLite) — one artifact, many sessions |
| `kiro` | Kiro IDE | workspace-session JSON under `%APPDATA%` |
| `antigravity` | Antigravity | conversation SQLite (protobuf payloads) |
| `dsh` | DSH | zstd JSONL under `~/.dsh/sessions` |

Defined once in `voyager/capability_matrix.py::PROVIDERS` and implemented as one
module each in `voyager/adapters/`.

**Cursor and Antigravity are experimental** and say so in the README; their
sources are parsed heuristically. That is a real difference between providers and
it is preserved on purpose — the taxonomy records it rather than flattening it.

## 2. Session source type

The shape of what is on disk, because it determines how much can be recovered
from one read.

| Type | Providers | Consequence |
|---|---|---|
| One file per session | `codex`, `claude`, `grok`, `kiro`, `dsh` | a failed write costs one session; the file can be re-read alone |
| One artifact, many sessions | `zcode`, `cursor`, `antigravity` | the whole artifact must be read before anything is written; a partial write must not be stamped complete |

The second row is the source of defects D5 and D7 and of issue #18. The rule it
implies — never stamp a shared artifact until every session in it is written — is
in `docs/SCAN_CONSISTENCY.md`.

## 3. Integration surface

How sessionFlow gets invoked inside an agent, strongest to weakest. Defined in
`voyager/integrations/capabilities.py::ZeroTouchLevel`.

| Level | Meaning |
|---|---|
| `SESSION_START_ZERO_TOUCH` | the agent's own session-start hook calls Voyager; nothing for the human to do |
| `FIRST_TURN_ZERO_TOUCH` | an instruction makes the agent call Voyager before the first task |
| `LAUNCHER_ZERO_TOUCH` | an opt-in wrapper script supplies continuity at launch |
| `WATCHER_ATTACH_ONLY` | a GUI IDE with no hook; continuity comes from watching its files |
| `STARTUP_ASSISTED` | the human asks, via an MCP tool or a prompt |
| `BEST_EFFORT` | partial automation, limited by the platform |
| `UNSUPPORTED` | no automation surface |

## 4. Verification level

How strongly a capability claim is supported. Defined in
`voyager/capability_matrix.py`; ordered, and a cell resolves to the **weaker** of
declared and observed.

| Level | Meaning |
|---|---|
| `NOT_FOUND_IN_CURRENT_AUDIT` | the provider was not present when the audit ran |
| `SUPPORTED` | the provider offers this and Voyager implements it — **never** "seen working" |
| `CONFIGURED` | installed and wired up locally |
| `UNIT_VERIFIED` | covered by a test |
| `LIVE_VERIFIED` | observed happening on this machine |
| `ZERO_TOUCH_LIVE_VERIFIED` | observed happening with no human action at all |

Only evidence lifts a cell above `CONFIGURED`, and no promotion happens without a
recorded reason. The distinction between `SUPPORTED` and `LIVE_VERIFIED` is what
stops this project from claiming verification it never did.

The measured dimensions are
`session_discovery`, `session_indexing`, `native_resume`, `startup_hook`,
`dynamic_context_injection`, `native_session_id_at_start`, `native_auto_attach`,
`live_zero_touch_continuity`, `transcript_import`, `tool_event_support`,
`cwd_repo_discovery`, `context_budget`, `truncation_behavior`.

## 5. Source lifecycle

The canonical `sessions.source_state` values. Only these three are written by
runtime code.

| State | Meaning | Reversible by |
|---|---|---|
| `ACTIVE_SOURCE` | at least one usable backing source exists; eligible for live continuity | a scan, when a source reappears |
| `SOURCE_MISSING` | all known sources have vanished; history retained, search and timeline still work | a scan, when a source reappears |
| `ARCHIVED_CANONICAL` | Voyager's copy is deliberately the authoritative record | an explicit archive action, and only that |

`NULL`, `'LIVE'` and `''` are migration inputs from pre-O2 databases; the
additive migration rewrites them at open and no runtime path produces them. There
is deliberately no `RETAINED` state — a state nothing sets is a state that gets
set wrongly.

## 6. Continuity operation

What a user can ask for, and what each one actually does.

| Operation | Context source | Launches anything? | Resumes a provider session? |
|---|---|---|---|
| `brief` | recent activity across providers | no | no |
| `continue` | one WorkThread | no | no |
| `switch` | one WorkThread, for a named agent | no | no |
| `handoff` | one WorkThread, written for another agent | no | no |
| `merge` | several sessions or threads | no | no |
| native resume | the provider's own session | the provider CLI | **yes** — and it is a separate capability |

Every row except the last goes through `continuity.handoff_thread()`; the last
one does not go through Voyager at all. See
[CONCEPT_MODEL.md](CONCEPT_MODEL.md) for how these relate.
