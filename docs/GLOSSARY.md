# Glossary

The canonical names for the vocabulary this project uses, extracted from the
implementation rather than from prose. Where a term is defined in code, the
definition is a link to that code, because code is the thing that cannot drift.

Two rules this file exists to enforce:

- **Use one name for one concept.** If a document, an issue or a commit message
  invents a second name for something here, it is a bug in the document.
- **Do not rename a public field, a database column or an error code to match
  this glossary.** The glossary describes the names that already exist. Where a
  name here and a name in the code differ, the code wins and this file is wrong.

## The four distinctions that get confused

These are called out first because each one has already caused a wrong claim.

| | |
|---|---|
| **Context Handoff ≠ Native Resume** | A handoff is a *file Voyager writes* containing synthesised context that a human or agent then reads. Native resume is *the provider's own command* reopening its own session. Handoff works on every provider; native resume depends on a provider capability and an intact source. A handoff is not evidence that resume works. |
| **Source Missing ≠ Deleted Session** | A missing source marks `source_state='SOURCE_MISSING'` and **retains** the normalised history. Nothing is deleted. Search and timeline keep working; only *live* continuity stops. |
| **Assistant Conclusion ≠ Verified Fact** | Text produced by an agent — including text Voyager itself compiled — is a claim. Only a row in the verification evidence tables, or a test, is a fact about this repository. The continuation bundle labels claims as claims on purpose. |
| **WorkThread ≠ Single Session** | A WorkThread groups *many* sessions, potentially across different providers. One session belongs to at most one WorkThread; a WorkThread outlives any of its sessions. |

## Terms

### Agent / Provider

**Agent** (智能体) — the product a human runs: Claude Code, Codex, ZCode, DSH,
Grok CLI, Cursor, Kiro IDE, Antigravity.

**Provider** (提供方) — the integration identity sessionFlow uses for one agent.
It is the `provider` column on `sessions` and the value of `--platform`.
Today there is exactly one provider per agent, but they are different concepts:
the agent is a process, the provider is an adapter.

Code: `voyager/adapters/` (one module per provider) · `voyager/capability_matrix.py`

### Session

**Session** (会话) — one recorded conversation of one agent. Stored with a
composite primary key `"provider:native_id"`, so the same underlying id from two
providers cannot collide. A session carries repo/git metadata, counts, and
resume capability flags.

Distinct from WorkThread: a session is one conversation; a WorkThread is the work
that spans conversations.

Code: `voyager/store.py` (`sessions` table) · `voyager/model.py`

### Native Session ID

**Native Session ID** (原生会话 ID) — the identifier the *provider* uses for its
own session, kept as `native_id`. It is what gets handed back to the provider for
a native resume, so it must be byte-exact. For Codex it is derived from the
rollout **filename** (a UUID shape), not from a `session_id` field inside the
file.

Distinct from the sessionFlow session id, which is `provider:native_id`.

Code: `voyager/adapters/codex.py` · `voyager/adapters/base.py`

### Source

**Source** (来源) — a file or database on disk that backs a session's history:
one JSONL file, or one SQLite artifact that may back *many* sessions. A session
can have several sources; a multi-session artifact is one source path shared by
N sessions.

This multiplicity is the reason fingerprinting is deferred: see
[SCAN_CONSISTENCY.md](SCAN_CONSISTENCY.md).

Code: `voyager/store.py` (`sources` table) · `voyager/scanner.py`

### Event

**Event** (事件) — one recorded step inside a session: a user message, an
assistant message, a tool call, a shell command with its exit code, a file edit.
Events carry `seq` for ordering and `origin` for provenance.

Code: `voyager/store.py` (`events`, `event_fts`) · `voyager/adapters/*.py`

### WorkThread

**WorkThread** (工作线程) — a named unit of work that groups sessions, with a
goal, a repo scope and a status. It is the thing continuity operates on: the
question "what was I doing" is answered for a WorkThread, not for a session.

A WorkThread may span providers. Attaching a session to an explicit thread is an
explicit action; the resolver never guesses.

The resolver reports `status` as one of `success`, `no_thread` or `ambiguous`.
**`ambiguous` is not `no_thread`:** it means several threads are in scope and
nothing chose between them, so a caller that must act on one thread has to stop
and ask. Recency may offer a *default*, and a default must be labelled as one
wherever it is shown. Callers that read "no current thread" out of an ambiguous
result are stating something false.

Code: `voyager/store.py` (`threads`, `thread_sessions`) ·
`voyager/auto.py::resolve_work_thread` (the single entry point)

### Context

**Context** (上下文) — the text handed to an agent so it can continue work: the
thread goal, recent real conversation, repository state. It is *compiled*, under
a budget, from session evidence — never dumped.

Code: `voyager/continuity.py` · `voyager/budget.py` · `voyager/ranker.py`

### Continuation Bundle

**Continuation Bundle** (续接包) — the compiled context written to a file
(default `~/.voyager/bundles/`). Produced when the work shape is multi-session
or multi-thread; a single session produces a package printed to the working
directory instead. The engine reports which style it actually chose and the
renderer follows it.

Distinct from Handoff: a bundle is the multi-session form of the same engine
output. Both come from `continuity.handoff_thread()`.

Code: `voyager/continuity.py` · `voyager/export.py`

### Handoff

**Handoff** (交接) — transferring work context from one agent to another: compile
the context, write it where the receiving agent will read it. One engine,
`continuity.handoff_thread()`; `switch`, `continue`, `handoff`, `merge` and the
MCP tools are thin adapters over it.

The engine does not print, does not launch anything, does not write provider
files and does not create a WorkThread. A handoff does **not** resume anything.

Code: `voyager/continuity.py::handoff_thread` · `docs/DECISIONS.md` D14

### Switch

**Switch** (切换) — handoff plus an intent to keep working *now*, in the named
agent. It is the same engine with a different entry point.

Code: `voyager/cli.py` (`switch`) · `voyager/continuity.py`

### Merge

**Merge** (合并) — handoff whose context is compiled from **several** sessions or
threads rather than one. Scoped to the providers of the sessions being merged.

Code: `voyager/cli.py` (`merge`) · `voyager/continuity.py`

### Native Resume

**Native Resume** (原生恢复) — the provider's *own* command reopening its own
session (`codex resume`, `claude --resume`, `grok -r`). sessionFlow records the
command in `resume_cmd` and whether the provider declares the capability in
`can_resume`.

**A declared capability is not an observed one.** No native resume has been
exercised end to end on any provider; the capability matrix distinguishes
`SUPPORTED` (implemented) from `LIVE_VERIFIED` (seen working), and the README
says to read every resume tick as `NOT VERIFIED` until a verification row says
otherwise.

Code: `voyager/adapters/base.py` (`can_resume`, `resume_cmd`) ·
`voyager/capability_matrix.py`

### Source Missing

**Source Missing** (来源缺失) — the canonical `source_state` value meaning *all*
known sources for a session have disappeared from disk. The normalised history is
retained; search and timeline still return it; only live continuity stops. It is
**not** a deletion and must never be turned into one.

The sibling value `ACTIVE_SOURCE` means at least one usable source exists.
`NULL`, `'LIVE'` and `''` are migration inputs from pre-O2 databases only, and no
runtime path produces them.

Code: `voyager/store.py` (`sessions.source_state`) · `voyager/auto.py` ·
`tests/test_retention.py`

### Archived Canonical

**Archived Canonical** (归档正本) — the canonical `source_state` value
`ARCHIVED_CANONICAL`: Voyager's copy of this session's history is deliberately
kept as the authoritative record even though the live source is gone. Unlike
`SOURCE_MISSING`, which is derived from disk, this state is set only by an
explicit archive action — and cleared only by an explicit one.

Code: `voyager/store.py::mark_canonical_archived`

### Provenance

**Provenance** (来源归属) — *who actually produced an event*. A provider's
transcript can contain events that are not the human's: subagent output, injected
system text, tool-generated content. Provenance records that, so a bundle can say
"the assistant claimed X" rather than "X".

`NULL` in storage means "not enriched" and surfaces as `unknown` — it does not
mean the human wrote it.

Code: `voyager/provenance.py` · `voyager/continuity.py`

### Lease

**Lease** (租约) — a per-WorkThread, time-bounded permission saying which
provider is currently allowed to write that thread's log. It exists to stop two
live agents writing the same WorkThread at once.

**A lease is not a scan lock.** Its scope is one WorkThread and its lifetime is
the holder's; the scanner takes no lease and is protected only by SQLite WAL and
the busy timeout. Do not reuse it to serialise scans.

Code: `voyager/store.py` (`thread_leases`, `thread_lease_*`) ·
`docs/DECISIONS.md` D13

### Checkpoint

**Checkpoint** (检查点) — a deterministic, LLM-independent snapshot of a
WorkThread's state: goal, phase, decisions, blockers, next steps. Deliberately
not summarised by a model, so two checkpoints of the same state are identical.

Code: `voyager/checkpoint.py`

## Keeping this honest

`tests/test_docs.py` checks that the terms above are spelled the same way in the
documents that use them, and that the lifecycle states and error codes named here
still exist in the code. If a rename lands in the code and not here, that test is
what fails.
