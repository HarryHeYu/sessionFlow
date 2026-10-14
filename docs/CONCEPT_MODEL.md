# Concept model

The domain in three parts: how things are **classified**, how they
**relate**, and the **invariants** that must hold. Every entry names the code
that implements it and, where one exists, the test that would fail if it broke.

This is a description, not a system: there is no RDF, no graph database and no
separate knowledge store. The model lives in SQLite tables, Python dataclasses
and the tests.

## Taxonomy

How the domain is classified, and where each classification is defined in code.
There is no second registry here: every category below names the enum, the
column or the tuple that already owns it. If a category and the code disagree,
the code is the contract and this document is the bug.

### 1. Provider

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

### 2. Session source type

The shape of what is on disk, because it determines how much can be recovered
from one read.

| Type | Providers | Consequence |
|---|---|---|
| One file per session | `codex`, `claude`, `grok`, `kiro`, `dsh` | a failed write costs one session; the file can be re-read alone |
| One artifact, many sessions | `zcode`, `cursor`, `antigravity` | the whole artifact must be read before anything is written; a partial write must not be stamped complete |

The second row is the source of defects D5 and D7 and of issue #18. The rule it
implies — never stamp a shared artifact until every session in it is written — is
in `docs/SCAN_CONSISTENCY.md`.

### 3. Integration surface

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

### 4. Verification level

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

### 5. Source lifecycle

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

### 6. Continuity operation

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

## Entities and relationships

```
Provider ──produces──▶ Session ──contains──▶ Event
                          │
                          │ backed by
                          ▼
                       Source ──(lifecycle)──▶ ACTIVE_SOURCE
                          │                     SOURCE_MISSING
                          │                     ARCHIVED_CANONICAL
                          ▼
                    recoverable?

WorkThread ──groups──▶ Session (many, possibly across Providers)
     │
     ├── Checkpoint  (deterministic snapshot of the work)
     └── Lease       (which Provider may write the thread log, right now)

Session evidence ──compiled by──▶ Context ──packed as──▶ Continuation Bundle
                                        └──transferred by──▶ Handoff
                                                                 │
                                                     Switch / Merge (same engine)

Native Resume ──uses──▶ Provider capability + an intact Source   (not a Handoff)
```

| Relationship | Implemented in | Pinned by |
|---|---|---|
| Provider produces Session | `voyager/adapters/*.py` → `store.replace_session` | `tests/test_adapters.py` |
| Session is backed by Source | `sources` table; `(provider, path, sid)` | `tests/test_scan_consistency.py` |
| Session contains Event | `events`, `event_fts` | `tests/test_store.py`, `tests/test_search.py` |
| WorkThread groups Sessions | `threads`, `thread_sessions` | `tests/test_threads.py` |
| WorkThread is resolved, never guessed | `auto.resolve_work_thread` | `tests/test_auto.py` |
| Bundle synthesises session evidence | `continuity.handoff_thread` | `tests/test_continuity.py` |
| Handoff transfers context | `continuity.handoff_thread` (one engine) | `tests/test_continuity.py` |
| Native resume needs capability **and** source | `adapters/base.py`, `capability_matrix` | `tests/test_capability_truth.py` |
| Source lifecycle gates recovery | `store.py` `source_state`, `auto.py` | `tests/test_retention.py` |
| Provenance attributes an event | `voyager/provenance.py` | `tests/test_provenance.py` |

## Invariants

**I1 — A missing Source does not delete its Session.**
When every source of a session disappears, the row is marked
`SOURCE_MISSING`; history, search and timeline keep returning it, and only live
continuity stops. Nothing in the codebase deletes a session because a file went
away. Reversal is a scan, not a command.
Code: `store.py` (`source_state`), `auto.py` · Test: `tests/test_retention.py`

**I2 — A WorkThread can span Providers.**
Thread membership is a list of session ids, and a session id is
`provider:native_id`; nothing requires the providers to match. Continuity
compiles across them, and a merge is scoped to the providers of the sessions
being merged rather than to one.
Code: `store.py` (`thread_sessions`), `continuity.py` · Test: `tests/test_continuity.py`

**I3 — An assistant conclusion is not a verified fact.**
Text produced by an agent — including text this project compiled — is a claim.
Only a verification-evidence row or a test is a fact about the repository. The
bundle marks claims as claims, and the capability matrix separates `SUPPORTED`
(implemented) from `LIVE_VERIFIED` (observed) for exactly this reason.
Code: `provenance.py`, `capability_matrix.py` · Test: `tests/test_capability_truth.py`

**I4 — Native resume capability is not context synthesis capability.**
A provider can declare `can_resume` and still have no session-start hook, no
context injection, and no way to attach a session automatically. The two are
measured as separate dimensions; a green resume tick says nothing about
continuity.
Code: `capability_matrix.py::DIMENSIONS` · Test: `tests/test_capability_consistency.py`

**I5 — Two agents can disagree about the same task.**
Sessions from different providers carry different conclusions about the same
repository. Voyager stores both and attributes each; it does not pick a winner,
and continuity does not present one agent's claim as the state of the work.
Code: `provenance.py`, `continuity.py` · Test: `tests/test_continuity.py`

**I6 — A partial write is never recorded as complete.**
For a shared artifact, no source is stamped fresh until every session in it has
been written. A failed write withholds the stamp for the whole artifact, so the
next scan re-reads it. Stamping the survivors is the defect D5 and would strand
the failed session permanently.
Code: `scanner.py` (`stamp_sources`, `_pre_read_fingerprints`) ·
Test: `tests/test_scan_error_isolation.py`, `tests/test_scan_source_change.py`

**I7 — Opening the index never needs the write lock.**
Every entry point — scan, continue, status, the MCP tools, the DSH bridge — must
be able to open the database while another process is writing. `Store.__init__`
performs no unconditional writes.
Code: `store.py::__init__` · Test: `tests/test_concurrent_open.py`

**I8 — The lease is not a scan lock.**
A lease is scoped to one WorkThread and its holder's lifetime. The scanner takes
no lease; concurrent scans are protected by SQLite WAL and the busy timeout, and
the measured behaviour is convergence without contention.
Code: `store.py` (`thread_leases`), `scanner.py` ·
Evidence: `docs/releases/v1.1.1-reliability-hardening.md`

## What this model is not

It is not a promise that every entity is independently replaceable. `Store` is
one module on purpose: the tables above share transaction boundaries, and
splitting them into per-entity repositories would make the invariants above
harder to state, not easier. That was assessed in
[releases/p7-architecture-audit.md](releases/p7-architecture-audit.md) and the
conclusion was to leave it as it is.
