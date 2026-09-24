# Roadmap: Continuity Engine

> Voyager compiles scattered agent histories into the context the next agent actually needs.

**Status:** Phase 1 shipped (`voyager merge`, commit `ff13096`).
**Phase 1b shipped (`5e8271c`); Phase 2 COMPLETE — 2a WorkThread (`b580001`/`934dfd5`), 2b lease (`c41f99b`), 2c deferred items closed (MCP voyager_thread + deterministic --repo thread resolution; multi-signal auto-clustering re-scoped as a later enhancement under #3).**
**Phase 3 shipped (`d931b02`); Phase 4 shipped (`96fc752`); Phase 5 shipped (`2f01a7f`); **
**Phase 6 `voyager switch` shipped; Phase 7 shipped as core API + stdio bridge + VS Code extension scaffold (see CHANGELOG).**

**Continuity Engine 主线到此完整：`index → WorkThread → goal rank → budget → skill → lease → switch → API/UI client`。**
Phase 7 is partially shipped (API/bridge/scaffold done; Composer webview + one-click switch UI pending).

**Final assessment (2026-09-20; classification corrected 2026-09-23)**: All roadmap phases complete. Zero-Touch Startup Continuity core implementation verified via pytest (`374 collected → 372 passed, 2 skipped` with the full dev extras; `356 passed, 18 skipped` simulated core-only).

> **Correction.** The original assessment classified Codex/Claude as **STARTUP_ASSISTED**, reasoning that "no automatic `voyager_startup` invocation happens at agent startup". The *observation* was real; the *cause* was not the platforms. Voyager's own installer wrote an invented flat hook schema **and was never wired into `voyager integrate install`**, so nothing was ever registered. With that fixed, Claude Code registers a real native `SessionStart` hook and reports **`H`** — registered, live trigger unverified. No provider claims `Y`, and Zero-Touch Final Acceptance stays **OPEN**. See [`claude_continuity_verdict.md`](../claude_continuity_verdict.md).

**Phase 2 close-out audit (2026-09-18, checked against the code):**

*Implemented and tested:* thread model (additive migration covered),
explicit CRUD/attach, merge->thread, continue --thread, cwd->thread default
pickup, lease table, atomic acquire (BEGIN IMMEDIATE), expiry (heartbeat/pid),
token-bound renew/release, --steal + leases.log audit, thread unlock CLI,
watch heartbeat.

*Implemented without dedicated tests:* none (thread/lease behavior is
covered).

*Was deferred in the first audit — now CLOSED:*
- `continue --repo` now resolves the newest active WorkThread for the repo
  deterministically (loud thread pick, members-only compilation) and falls
  back to the newest session when no thread matches. The multi-signal
  auto-cluster (48h window + branch + file overlap + FTS overlap) is
  re-scoped as a **later enhancement** under #3: explicit threads are the
  product semantics, and silently swallowing same-repo sessions was the
  highest mislabeling risk.
- MCP `voyager_thread_list/show/attach/close` shipped as thin wrappers
  over the Store API.

*Belongs to other issues (not Phase 2):* `voyager switch` acquires the
lease (#7); transcript writer / ingest is #10 (gated behind the lease).

**=> issue #3 close-out (re-scoped):** the deterministic --repo thread
resolution and the MCP voyager_thread tools shipped; multi-signal
auto-clustering is re-scoped as a later enhancement (docs/POST-1.0.md).
Close #3 with this reconciliation; issue #11 closes per its own
reconciliation above (no gh credentials on this machine).

**Issue #9 close-out (commit `5e8271c`):** incremental pre-compile scan ✅,
freshness line ✅, idempotent ✅, watch stays background ✅, provider files
never written ✅. Scoped scan (provider/repo-limited) split out as a
follow-up optimization. Close issue #9 with this reconciliation.

**Issue #11 close-out (commit `c41f99b`):** `thread_leases` table with
additive migration ✅, atomic `BEGIN IMMEDIATE` acquire — a live lease
blocks the second writer and names its holder, an expired one never
blocks ✅, expiry = heartbeat >120s stale OR dead pid (OpenProcess probe
on nt, `kill(pid,0)` on POSIX) ✅, token-bound renew/release with
explicit `--steal` appended to `~/.voyager/leases.log` ✅,
`voyager thread unlock [--steal]` + lease line in `thread show` ✅,
`voyager watch` doubles as the D13 heartbeat (renews live pids; sleep
shortens to <=30s while a lease is live) ✅. Consumers of the lock are
#7 (`switch`) and #10 (writers). Close issue #11 with this
reconciliation.
**Companion:** [中文版](ROADMAP.zh-CN.md)

Voyager today is a unified **index** of every AI coding agent on the machine.
The next layer is a **continuity / context orchestration** layer: not only
"what did we talk about", but "reassemble work state from many agents and
sessions, and let a *different* agent continue".

```
Search past work, merge context across sessions, and continue seamlessly in any agent.
```

This is a bigger product jump than adding another adapter, a TUI, or more
search. The index is the foundation; the compiler is the moat.

---

## Positioning

| Now | Next |
|---|---|
| One searchable index across every AI coding agent on your machine. | One continuity layer across every AI coding agent. |
| Session-centric (`handoff <id>`, `continue` = latest session). | Task-centric (`WorkThread` of many sessions). |
| Conversation summary (truncate + concat). | Context synthesis (rank, dedup, resolve, budget). |
| "Seamless session migration" (not actually possible). | Seamless **work continuation**. |

Cross-agent resume cannot copy system prompts, hidden tool state, cached
reasoning, or provider runtime. Voyager will never claim it can. What it
*can* do is make the user experience one command:

```
voyager switch codex
```

find the active thread, **refresh the index**, extract state, check git,
write a Continuation Bundle, launch Codex, and have Codex read the
bundle and keep working.

---

## Can we switch chat history across agents?

This is the question that looks like session teleportation. It is three
different products, and only two of them are real:

| What people mean | Possible? | What the user actually gets |
|---|---|---|
| Same-provider native resume | **Yes, shipped** (`voyager resume` / `continue`) | The original chat, original tool state, original CLI. |
| Cross-agent *work* continuation | **Yes, Phase 1 shipped** (`merge` / `continue --from` / `handoff`) | A **new** session in the target agent that reads a Continuation Bundle and keeps working. |
| Cross-agent *chat transcript* in the other TUI | **Not the default.** Maybe later, JSONL CLIs only, opt-in | A synthetic text-only history under a **new** session id. Not the original session. |

Cross-agent resume cannot copy system prompts, hidden tool state, cached
reasoning, MCP connections, or provider runtime. Voyager will never
claim it can. The user-visible seam is one command (`voyager switch
codex`); underneath it is always **compile from a canonical index**,
never "move this file into the other agent".

A 2026-09-16 headless probe planted a secret only in a **new**
text-only session file, then native-resumed it:

| Target | Result |
|---|---|
| Grok `grok --resume --single` | **HIT** |
| Codex `codex exec resume` | **HIT** (needed `--ignore-user-config` on this machine) |
| Claude `claude -p --resume` | **TIMEOUT** at 60s |

So JSONL *writers* for Grok and Codex are real; Claude is not proven.
They still only run under a **single-writer lease** (below). They are
not a licence for two agents to share one native file.

---

## Format translation (why pairwise copy fails)

Eight agents, eight on-disk formats. They are not interchangeable.

| Provider | On-disk shape | Resume CLI | Safe to *write*? |
|---|---|---|---|
| Codex | `rollout-*.jsonl` `{timestamp,ordinal,type,payload}` | `codex resume` / `codex exec resume` | **yes**, new id, lease required (probe HIT) |
| Claude Code | project JSONL + `uuid`/`parentUuid` chain + file-history | `claude --resume` | not proven (headless resume timed out) |
| Grok | `chat_history.jsonl` (OpenAI-style) + `summary.json` | `grok -r` | **yes**, new id, lease required (probe HIT) |
| DSH | zstd JSONL | `dsh --resume` | later, opt-in, new id only |
| ZCode | SQLite `message`/`part` | none confirmed | **no** |
| Cursor | `state.vscdb` KV | none | **no** |
| Antigravity | SQLite + protobuf | none | **no** |
| Kiro | JSON `history[]`, no tools | none | **no** |

Tool names do not map (`Read` ≠ `shell_command` ≠ `read_file`).
Unmatched `tool_use` / `tool_result` pairs break the next API call.
Grok/Codex reasoning is encrypted. A pairwise converter matrix
(Claude→Codex, Codex→Grok, …) is 8×7 writers that rot whenever a
vendor bumps a JSONL schema.

So the hub is the index Voyager already has:

```
8 provider formats
      ↓  adapters (read-only)          shipped
canonical Session / Event
      ↓  continuity compiler           Phase 1 shipped
Continuation Bundle (Markdown, D8)
      ↓  launch target
new session in the other agent
```

An optional later branch — **transcript transplant** — also starts
from the canonical Event, never from a sibling provider file:

```
canonical Event
      ↓  flatten to user/assistant text (drop tool calls)
      ↓  per-adapter WRITER, JSONL CLIs only, NEW session id
target session file
      ↓  native resume
```

Writers are a new, test-covered adapter surface. They are not the
default switch path (D8, D11). Default stays: compile a bundle.

---

## Auto-sync (the actual seam)

"Switch chat history" fails in practice when the bundle is built from
a **stale index**, not when the Markdown is imperfect.

Today:

- `voyager scan` is manual.
- `voyager watch` polls every 300s (mtime + size, idempotent).
- `continue` / `handoff` / `merge` compile from whatever is already
  in `~/.voyager/index.db`.

If you just finished a Claude turn and immediately `voyager switch
codex`, the last turn may not be in the index yet. That is the
seam-breaker.

Required rules (D12):

1. **Scan-before-compile.** `handoff` / `merge` / `continue` /
   `switch` run an incremental scan (not `--force`) before they
   read the store. Targeted to the relevant provider/repo when
   possible.
2. **`voyager watch` stays the background daemon.** Switch must
   not depend on it having ticked recently.
3. **One-way only.** Provider files → index. Never two-way live
   mirroring: that is a format-translation problem *and* a race
   against the agent that owns the file.
4. **After switch.** The next scan/watch indexes the *new* target
   session and (Phase 2) attaches it to the WorkThread.
5. **Optional later.** OS filesystem events instead of a 300s
   poll. Correctness does not depend on this if (1) exists.

JSONL agents (Claude / Codex / Grok / DSH) bump mtime every turn,
so an incremental scan sees them. SQLite agents (ZCode / Cursor)
already rescan when the DB mtime changes.

This is Phase 1b / issue #9. It unblocks an honest `voyager switch`.

---

## Single-writer canonical log (lock + sync-on-open)

Yes: unify every agent's history into **one Voyager format**, sync it
into the agent you are about to open, then keep writing — **as long
as only one agent holds the pen**.

What must not happen: Claude and Codex both appending to the same
chat. Native files are not shareable; even the canonical log would
interleave two "current states" and the next compile would be
garbage.

```
                    ┌─ lease: grok (pid, heartbeat) ─┐
canonical log       │                                │
~/.voyager/         │   on open: materialize         │  while running:
threads/<id>/       │   canonical → grok JSONL       │  grok JSONL ──ingest──▶ canonical
                    │   (NEW session id)             │  (do NOT rewrite grok's file)
                    └────────────────────────────────┘
switch away → final ingest → release lease → next agent may acquire
```

### Lock

A WorkThread has at most one **lease**:

```sql
CREATE TABLE thread_leases (
    thread_id         TEXT PRIMARY KEY,
    holder            TEXT NOT NULL,   -- provider
    native_session_id TEXT,
    pid               INTEGER,
    acquired_at       REAL,
    heartbeat_at      REAL,
    lease_token       TEXT NOT NULL
);
```

- `voyager switch codex` **acquires** the lease or exits: "held by
  grok pid=1234, heartbeat 8s ago".
- `voyager watch` **heartbeats** the lease while it tails the holder.
- Stale if `heartbeat_at` is older than 120s **or** the pid is gone.
- `voyager thread unlock` / `--steal` for a stuck lease (logged).
- Same-provider native resume of the *leased* session is allowed;
  a second provider is not.

SQLite `BEGIN IMMEDIATE` on `index.db` is the atomic acquire. No
second process can take the same thread in the same transaction.

### Sync-on-open (not live rewrite)

When the lease moves to agent X:

1. Incremental scan of the **previous** holder (flush last turns).
2. Append those events onto the canonical thread log.
3. If X has a proven writer (Grok, Codex today): flatten
   user/assistant text → write a **new** native session → `resume`.
4. Else: Continuation Bundle + new session (D8). Claude stays here
   until headless resume of a synthetic JSONL is a HIT.
5. Attach the new native session to the thread; record it on the lease.

Voyager does **not** rewrite X's native file while X is alive.
That is the race the lock exists to prevent.

### Continuous write = ingest from the holder only

"Keep writing" means:

- The agent writes its own format (it already does).
- Voyager tails **that one session** and appends canonical Events
  (watch can poll that file every few seconds while a lease is held).
- Other providers' leftover files for this thread are ignored until
  they become the holder again (and then they get a **new** id).

It does **not** mean: push every new turn into Claude, Codex, and
Grok at once. That is multi-writer mirroring and is a non-goal.

### Why this is operable

| Piece | Status |
|---|---|
| Canonical Event / SQLite | shipped |
| Scan / watch ingest | shipped (#9) |
| WorkThread identity | shipped (Phase 2a) |
| Lease table + acquire/heartbeat/release | shipped (Phase 2b, `c41f99b`) |
| Writer Grok / Codex | shipped (opt-in, lease-gated, new ids only — #10) |
| Writer Claude | blocked on a HIT |
| Live rewrite of an open native file | never |

User-visible still one command: `voyager switch grok`. Internally:
flush → acquire lease → materialize or bundle → launch → tail.

---

## What already shipped (the foundation)

Do not rebuild this. The continuity layer sits on top.

| Layer | Today |
|---|---|
| Adapters | 8 providers → normalized `Session` / `Event` (`voyager/model.py`) |
| Index | SQLite + FTS5 trigram (`voyager/store.py`), idempotent scan |
| Per-session handoff V1 | `voyager/handoff.py`: one session → Markdown Context Package → launch `claude` / `codex` / `grok` |
| Continue V1 | newest session; native resume when `can_resume`, else handoff |
| Merge / Continuity V1 | `voyager/continuity.py`: N sessions → Continuation Bundle; `voyager merge`; `continue --from`; MCP `voyager_merge` |
| Watch / scan | interval poll + freshness-before-compile (Phase 1b shipped) |
| MCP | `brief` / `search` / `list` / `show` / `handoff` / `merge` |
| Constraints | provider files read-only; no network; no telemetry; core has zero deps |

Handoff V1 already extracts original request, follow-ups, last assistant
message, touched files, commands, errors, and a condensed timeline, then
injects by **file path** (D8) rather than stuffing the prompt into argv.

The gap: it is **session-centric**. Real work looks like:

```
Voyager project
 └── improve adapter reliability
      ├─ Codex A   "analyse adapter"
      ├─ Claude B  "fix DSH adapter"
      ├─ Codex C   "test CI"
      └─ Grok D    "next architecture"
```

Voyager currently sees four sessions. It should start seeing **one
WorkThread**.

---

## Principles

1. **Work continuation, not session migration.** Same-provider native
   resume stays first-class (D7). Cross-provider is a new session seeded
   with a bundle.
2. **Goal-directed extraction, not history dump.** The bundle serves the
   *next* task. ` --goal "fix CI"` keeps pytest / Actions / failures and
   drops UI talk.
3. **Pointer over prose.** Anything the next agent can re-read from git
   or the filesystem is a path / commit / test name, not a pasted blob.
   (Borrowed from mature handoff skills such as
   [jumpifequal/handoff-skill](https://github.com/jumpifequal/handoff-skill)
   and Matt Pocock's `handoff` skill.)
4. **Synthesis, not concat.** If A proposed X, B rejected X for Y, and C
   implemented Y, concat re-opens X. The bundle must record
   `X → Y (superseded)` with provenance.
5. **Provenance on every claim.** Decision / state / failure cites source
   session ids, and optionally a verifying commit or passing test.
6. **Deterministic compiler in core.** Continuity V1 ranks and extracts
   from the already-normalized index. No LLM, no network (FAQ +
   CONTRIBUTING). The *target agent* is the reasoner that reads the
   bundle. An optional LLM pass is a later extra, never a core
   dependency.
7. **One core, many fronts.** CLI / MCP / Skill / UI all call the same
   compiler. Do not grow a Claude plugin, a Codex plugin, and a Cursor
   plugin that each reimplement merge.
8. **UI last.** A sidebar is a viewer. The moat is the pipeline below.
9. **Canonical IR, never pairwise converters.** Claude JSONL is never
   rewritten as Codex JSONL. Adapters read; the compiler emits a
   bundle; optional writers (if they ever exist) also read the
   canonical Event.
10. **Sync the index, not the session files.** Auto-sync means
    scan-before-compile + `watch`. It does not mean two-way
    mirroring of live provider stores.
11. **One writer per WorkThread.** A thread is leased to at most one
    live agent. Canonical history is append-only in Voyager.
    Provider files are a *projection* of that log, materialized
    only at open, ingested only from the lock holder.

---

## The compiler pipeline

```
Raw history (many sessions, many providers)
      ↓  0. sync       incremental scan (mtime+size) so the index is not stale
      ↓  1. select     repo / time / files / branch / goal
      ↓  2. extract    facts with pointers (not essays)
      ↓  3. dedup      same file, same command, same error
      ↓  4. overlay    chronological conflict: later wins, earlier marked superseded
      ↓  5. pointers   replace pasteable blobs with path / commit / test
      ↓  6. budget     Full / Balanced / Compact (or N tokens)
      ↓  7. bundle     Continuation Bundle (Markdown, D8)
      ↓  8. launch     native resume XOR new session + "read this file"
```

User-facing name is **Context Budget**, not "compression".

---

## Object model: Session → Thread → Continuation

```
Project (repo_root)
 └── WorkThread          # NEW logical object
      ├─ goal, status, branch
      ├─ Session (codex A)
      ├─ Session (claude B)
      └─ Continuation Bundle   # compiled view, not stored history
           └── Events stay in the session table
```

`WorkThread` is additive. Sessions and events do not change shape.
Suggested tables (Phase 2; not in V1 merge):

```sql
CREATE TABLE threads (
    id            TEXT PRIMARY KEY,
    title         TEXT,
    repo_root     TEXT,
    git_branch    TEXT,
    goal          TEXT,
    status        TEXT,          -- active | parked | done
    created_at    REAL,
    updated_at    REAL,
    metadata_json TEXT
);
CREATE TABLE thread_sessions (
    thread_id  TEXT NOT NULL,
    sid        TEXT NOT NULL,
    added_at   REAL,
    PRIMARY KEY (thread_id, sid)
);
```

Auto-cluster signals for `continue --repo` (no LLM):

| Signal | Weight |
|---|---|
| same `repo_root` | required (or cwd fallback) |
| recency window (default 48h) | high |
| same `git_branch` | high |
| overlapping touched files | high |
| overlapping FTS terms from titles / last user goals | medium |
| same provider | low (cross-agent is the point) |

Explicit `voyager merge A B C` always wins over auto-cluster.

---

## Continuation Bundle (target schema)

Replace today's flat handoff Markdown with a structured package.
Heuristic V1 fills every section from index + git; it does **not**
invent a narrative.

```markdown
# Continuation Bundle

## Goal
…  (--goal, else earliest substantive user request across selected sessions)

## Current verified state
- branch / HEAD / dirty files   (live `git`, not the session snapshot)
- last assistant conclusions, newest session first

## Completed work
- …  (files written, tests run with exit 0, commits if detectable)

## Open tasks
- …  (unresolved user asks, failing commands, last "next" language)

## Decisions
- Use Y.  sources: claude#abc, codex#def  verified: commit 485bdc8
  superseded: X → Y   (X proposed in session A, rejected in B)

## Known failures
- X failed because …   sources: codex#def

## Relevant artifacts  (pointers only)
- voyager/adapters/dsh.py
- tests/test_dsh.py

## Current repo state
- branch, commit, dirty files  (re-checked at bundle time)

## Evidence
- sessions included, providers, time range
- confidence: extracted | inferred | unverified
```

**Confidence / provenance is the difference versus a summarizer.**
Claims the compiler cannot support stay `unverified` or are omitted.

---

## Context Budget

```
voyager continue --budget auto
voyager continue --budget 12k
voyager handoff <id> --to claude --budget compact
```

| Preset | Rough size | Contains |
|---|---|---|
| `compact` | ~4k tokens | goal, current state, next steps, critical decisions, artifact pointers |
| `balanced` | ~20k | + files, errors, recent relevant exchanges |
| `full` | ~100k | + selected raw evidence, more timeline |
| `Nk` / integer | that many tokens | fill by priority until the cap |
| `auto` | target-dependent | large-context CLIs → balanced/full; unknown → balanced |

Estimate tokens as `chars/4` in core (no tokenizer dependency).
Goal-conditioned ranking happens *before* the cap: CI goal keeps
pytest / Actions / failures; UI goal drops them.

---

## CLI / MCP / Skill surface (shared core)

Four fronts, one compiler. Phased in; not all in the first PR.

```
# explicit merge
voyager merge <s1> <s2> <s3> [--goal "..."] [--budget balanced] [-o FILE]

# continue from several sessions, or auto-pick a thread
voyager continue --from sA,sB,sC --to claude --goal "finish adapter tests"
voyager continue --repo voyager --to codex --budget 12k

# one command to change agent and keep working
voyager switch codex          # current thread → bundle → launch

# later
voyager thread list|show|attach|close
```

MCP (thin wrappers around the same functions):

| Tool | Role |
|---|---|
| `voyager_handoff` | keep; add `goal` / `budget` / multi-id |
| `voyager_merge` | new |
| `voyager_continue` | new (thread-aware) |
| `voyager_switch` | new |
| `voyager_thread_list/show/attach/close` | shipped |

Skill (`skills/voyager/SKILL.md`), installed into
`~/.codex/skills/voyager/`, `~/.claude/skills/voyager/`, …:

| User says | Skill does |
|---|---|
| "hand this to Codex" | `voyager handoff` / `voyager switch` |
| "what did Claude just do" | `voyager search` / thread |
| "continue Codex's work from yesterday" | `voyager continue` |
| "merge my recent Voyager sessions" | `voyager merge` / synthesize |

Natural language lives in the skill. Logic lives in Voyager.

---

## Front-end architecture (when UI happens)

```
              Voyager Core (compiler + store)
                       │
               local API / daemon
                       │
         ┌─────────────┼─────────────┐
         │             │             │
        CLI           MCP           UI
         │             │             │
   Agent Skill    Agent-native    VS Code sidebar
                                  (later: Context Composer)
```

**Do not** start with per-agent plugins. **Do not** start with a full
web app. The first UI, if any, is a VS Code sidebar for people who
already live there:

- current project / active thread
- member sessions (provider + recency)
- selected context (files, decisions, conflicts)
- Continue with + Context budget
- Timeline mixing agent events and git
- **Context Composer**: check sessions on the left, live bundle +
  estimated tokens on the right, Launch at the bottom

That UI is Phase 7. It is a viewer of the compiler, not a second
implementation.

---

## Phased plan

Priority is the pipeline, not the chrome. Each phase is independently
shippable and reviewable.

### Phase 0 — Keep V1 honest  *(docs / wording only)*

**Why.** Current copy can be read as "seamless session migration".

- README / WORKFLOWS: `handoff` / `continue` described as work
  continuation. Native resume vs cross-agent bundle called out.
- Point at this roadmap from README "What's next".
- Decision records D9 (continuity over TUI-first) and D10
  (deterministic compiler).

**Done when:** wording cannot be mistaken for session teleportation.

### Phase 1 — Multi-session Context Synthesis  **(shipped, `ff13096`)**

**Why.** More important than single-session handoff. Unblocks everything.

```
voyager merge A B C
voyager continue --from A,B,C --to claude
```

**Implement**

- Extract a compiler module (`voyager/continuity/` or grow
  `handoff.py` until a second PR splits it).
- Select N sessions → extract facts → dedup → chronological overlay →
  bundle Markdown.
- Overlay rule for V1: newest session's "where we stopped" is current
  state; older assistant conclusions go under **Prior (may be
  superseded)** with timestamps and source ids. Do not silently drop
  them; do not present them as current.
- Live git snapshot at bundle time (`branch`, `HEAD`, `status --short`).
- Keep D8: write a file, launch with "read this path".
- Default output: `~/.voyager/bundles/` (cwd leftovers were an accident
  of V1), override with `-o`.
- Tests: three synthetic sessions (propose X / reject X for Y /
  implement Y) → bundle mentions Y as current and X as superseded.
  Concat-regression test: X must not appear as an open option.

**Files:** `voyager/handoff.py` (or `voyager/continuity/`), `voyager/cli.py`,
`voyager/mcp_server.py`, `tests/test_handoff.py` / `tests/test_continuity.py`.

**Out of scope:** thread table, `--goal` ranking, token budget, UI.

### Phase 1b — Index freshness / auto-sync  **(shipped, `5e8271c`)**

**Why.** Cross-agent switch is only as fresh as the index. Format
translation already happens at scan time; the missing piece is
*when* scan runs.

```
voyager switch codex          # must scan first, then compile
voyager merge A B C --launch  # same
voyager watch                 # background; not a substitute
```

**Implement**

- `handoff` / `merge` / `continue` / `switch` call an incremental
  `scan` (respect mtime+size, never `--force`) before they read
  sessions. Prefer a provider/repo-scoped scan when the command
  already knows one.
- Print a one-line freshness note (`scanned 0.8s, 2 sources changed`)
  so a stale-feeling bundle is diagnosable.
- Keep `voyager watch` as the background path (default 300s). Do
  not require a daemon for switch to be correct.
- After a launched handoff/switch, the new target session is picked
  up by the next scan/watch. Phase 2 attaches it to the WorkThread.
- Tests: a fixture source whose mtime is bumped between two
  `merge` calls must appear in the second bundle; a source that did
  not change must not be re-parsed (idempotency).
- Still one-way: tests must assert we do not write into provider
  session directories.

**Files:** `voyager/cli.py`, `voyager/store.py` (if a scoped scan
helper is needed), `tests/test_cli.py` / `tests/test_continuity.py`.

**Out of scope:** OS filesystem events, two-way session mirroring,
transcript writers, WorkThread.

### Phase 2 — WorkThread **(complete: 2a/2b shipped; deferred items closed — see Deferred under #3)**

**Why.** `continue` should mean "the work I am in", not "the newest
chat".

```
voyager continue --repo voyager --to codex
voyager thread list
voyager thread show <id>
voyager thread attach <sid>
```

**Implement**

- `threads` / `thread_sessions` tables; additive schema migration
  (existing indexes keep working).
- ~~Auto-cluster on `continue --repo`~~ re-scoped: `continue --repo` resolves the newest active thread deterministically; multi-signal auto-clustering is a later enhancement.
- `voyager merge A B C` creates (or updates) a thread.
- `continue` with no args: cwd repo → active thread → native resume if
  the latest member can resume **and** `--to` is absent; otherwise
  compile + handoff.
- ✅ MCP `voyager_thread_list/show/attach/close` (CLI is stable).
- **Single-writer lease** (issue #11, D13): `thread_leases` table;
  `switch` acquires or refuses; `watch` heartbeats; stale pid/heartbeat
  releases; `--steal` is explicit and logged.
- While a lease is held, ingest **only** the holder's session into
  the canonical log. Do not rewrite that native file.

**Out of scope:** fancy topic modelling, renaming UX polish, UI,
writers (those are #10, and they *require* this lease).

### Phase 3 — Goal-directed handoff

```
voyager handoff A --to claude --goal "finish adapter tests"
voyager continue --repo voyager --goal "fix CI"
```

**Implement**

- Rank extractable facts against `--goal` (FTS + file-path + command
  heuristics: `pytest` / `.github/workflows` for CI, etc.).
- Low-relevance sections shrink or vanish before budgeting.
- Single-session `handoff` and multi-session `merge` share the ranker.

**Out of scope:** calling an LLM to "understand" the goal.

### Phase 4 — Context Budget / adaptive packing **(shipped, see CHANGELOG)**

```
voyager continue --budget auto
voyager handoff A --to codex --budget 12k
```

**Implement**

- Presets `compact` / `balanced` / `full` / `auto` / integer tokens.
- Fill by priority (goal → state → decisions → failures → pointers →
  evidence) until the cap.
- Print estimated tokens on the CLI.

**Out of scope:** learned compression, provider-specific tokenizers.

### Phase 5 — Voyager Skill

**Why.** Voyager has to live *inside* each agent, not only in a
terminal the human remembers to open.

```
skills/voyager/SKILL.md
voyager skill install          # copies into known skill dirs
```

**Implement**

- One skill file: when to call Voyager, which CLI/MCP tool, what not
  to do (don't dump `export --format md` into context).
- Installer targets Codex / Claude Code / Grok skill locations;
  unknown agents get a printed path.
- MCP tools from Phases 1–4 documented in the skill.
- Tests: SKILL.md exists, installer is idempotent, does not write
  into provider *session* directories.

**Out of scope:** per-agent plugin UIs.

### Phase 6 — `voyager switch <agent>`

The "one command, keep working" UX.

```
voyager switch codex
```

**Implement**

0. Incremental scan (Phase 1b / D12). Refuse to compile from a
   store that has not been refreshed in this process.
1. Resolve active thread (cwd / `--repo` / explicit `--thread`).
2. Same provider + `can_resume` **and no `--to`** → native resume (D7).
3. Else compile bundle (goal + budget defaults) → launch target (D8).
4. Re-check git working tree; warn if dirty in a surprising way.
5. Do **not** write a synthetic session into the target agent's
   store (D11). Bundle file + "read this path" is the injection.

User-visible: one command. Internally: scan → select → compile → launch.

### Phase 7 — VS Code sidebar / Context Composer  *(partially shipped: API + bridge + extension scaffold; Composer webview and one-click switch UI pending — docs/POST-1.0.md)*

Only after Phases 1–4 exist, so the UI is a client of the compiler.

- VS Code sidebar: project, thread, sessions, Continue / budget.
- Context Composer: checkbox sessions → live bundle + token estimate
  → Launch.
- Optional tiny local HTTP/stdio API wrapping the same Python
  functions. Not a second business-logic tree.
- **Not** a full web app. **Not** N vendor plugins.

---

## GitHub issues

Opened on `HarryHeYu/sessionFlow` (labels `enhancement` + `continuity`).
Treat the checkboxes as the implementation contract.

| Issue | Title | Phase | Blocked by |
|---|---|---|---|
| [#1](https://github.com/HarryHeYu/sessionFlow/issues/1) | Continuity Engine: tracking issue | 0 | — |
| [#2](https://github.com/HarryHeYu/sessionFlow/issues/2) | `voyager merge`: multi-session context synthesis | 1 | — *(shipped `ff13096`)* |
| [#9](https://github.com/HarryHeYu/sessionFlow/issues/9) | Index freshness / auto-sync (scan-before-compile) | 1b | — *(shipped `5e8271c`)* |
| [#3](https://github.com/HarryHeYu/sessionFlow/issues/3) | WorkThread: project → thread → sessions | 2 | #2 *(complete — see close-out under Deferred; auto-cluster re-scoped)* |
| [#11](https://github.com/HarryHeYu/sessionFlow/issues/11) | Single-writer lease on a WorkThread | 2 | #3 *(shipped `c41f99b`)* |
| [#4](https://github.com/HarryHeYu/sessionFlow/issues/4) | Goal-conditioned extraction (`--goal`) | 3 | #2 *(shipped `d931b02`)* |
| [#5](https://github.com/HarryHeYu/sessionFlow/issues/5) | Context Budget (`--budget auto\|Nk`) | 4 | #2 *(shipped Phase 4)* |
| [#6](https://github.com/HarryHeYu/sessionFlow/issues/6) | Voyager Skill + `voyager skill install` | 5 | #2 |
| [#7](https://github.com/HarryHeYu/sessionFlow/issues/7) | `voyager switch <agent>` | 6 | #3, #4, #5, **#9**, **#11** *(shipped — see CHANGELOG)* |
| [#10](https://github.com/HarryHeYu/sessionFlow/issues/10) | Optional transcript transplant (per-adapter writers) | later | #9, **#11** — **stays OPEN**: codex/grok writers shipped (opt-in, lease-gated); claude/dsh unsupported pending probe gates |
| [#8](https://github.com/HarryHeYu/sessionFlow/issues/8) | VS Code sidebar / Context Composer | 7 | #3, #4, #5 — **partially shipped** (API + bridge + extension scaffold ✅; Composer webview + one-click switch UI pending) |

#1 is the umbrella. Close it when #2–#7, #9 and #11 are done; #8 and
#10 are post-moat (#10 is now Grok/Codex-shaped, still lease-gated).

---

## Non-goals (this generation)

- Seamless **session** migration (system prompt / tool state / cached
  reasoning cannot move).
- Pairwise format converters (Claude JSONL → Codex JSONL, etc.).
- Two-way live mirroring of provider session stores.
- Two agents holding the same WorkThread at once.
- Rewriting a native session file while that agent is still running.
- Per-agent plugins as the first UI.
- Concatenating transcripts and calling it merge.
- LLM-in-core summarization, cloud APIs, new core dependencies.
- Replacing native resume for same-provider continue (D7).
- Writing into provider session directories **as the default switch
  path** (D8, D11). An opt-in writer is #10, after synthetic resume
  is proven per CLI, and even then: new ids only, text-only flatten,
  JSONL CLIs only.
- A standalone web app.
- Treating TUI / more adapters as the *strategic* next step. Adapters
  still matter when a format breaks; they are not the product jump.

---

## What success looks like

A user who split work across Claude, Codex, and Grok on one repo runs:

```
voyager continue --repo voyager --to codex --goal "finish adapter tests" --launch
```

Codex starts in a **new** session, reads a bundle that already knows
the goal, the surviving decision (Y, not X), the failing test, the
files to touch, and the git HEAD — and does not re-propose X.

That is the product.

---

## Open questions (do not block Phase 1b)

1. **Bundle location.** *Decided in Phase 1:* default
   `~/.voyager/bundles/`; `-o` always available.
2. **Thread identity.** Auto-cluster only, vs requiring `merge` to
   create a thread. Leaning: auto-cluster for `continue --repo`,
   persist when the user merges or switches.
3. **Optional LLM extra.** A later `[llm]` extra that turns quotes
   into resolved Decision objects. Must stay opt-in and off the core
   path. Not in Phases 1–6.
4. **Daemon.** Watcher already exists (`voyager watch`). A local API
   daemon is only justified when the VS Code client needs it (Phase 7).
5. **Transcript transplant.** Grok and Codex headless resume of a
   synthetic text-only JSONL is a HIT; Claude timed out. Writers
   stay behind the lease (#11) and new-id-only. Claude remains
   bundle-only until it HITs.

When these need a product call, record it in `docs/DECISIONS.md`.
