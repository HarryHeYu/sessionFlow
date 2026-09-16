# Roadmap: Continuity Engine

> Voyager compiles scattered agent histories into the context the next agent actually needs.

**Status:** planning (no implementation in this document).
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

find the active thread, extract state, check git, write a Continuation
Bundle, launch Codex, and have Codex read the bundle and keep working.

---

## What already shipped (the foundation)

Do not rebuild this. The continuity layer sits on top.

| Layer | Today |
|---|---|
| Adapters | 8 providers → normalized `Session` / `Event` (`voyager/model.py`) |
| Index | SQLite + FTS5 trigram (`voyager/store.py`), idempotent scan |
| Per-session handoff V1 | `voyager/handoff.py`: one session → Markdown Context Package → launch `claude` / `codex` / `grok` |
| Continue V1 | newest session; native resume when `can_resume`, else handoff |
| MCP | `brief` / `search` / `list` / `show` / `handoff` |
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

---

## The compiler pipeline

```
Raw history (many sessions, many providers)
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
| `voyager_thread` | new, after WorkThread exists |

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

### Phase 1 — Multi-session Context Synthesis  **(do this first)**

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

### Phase 2 — WorkThread

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
- Auto-cluster on `continue --repo` using the signal table above.
- `voyager merge A B C` creates (or updates) a thread.
- `continue` with no args: cwd repo → active thread → native resume if
  the latest member can resume **and** `--to` is absent; otherwise
  compile + handoff.
- MCP `voyager_thread` once the CLI is stable.

**Out of scope:** fancy topic modelling, renaming UX polish, UI.

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

### Phase 4 — Context Budget / adaptive packing

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

1. Resolve active thread (cwd / `--repo` / explicit `--thread`).
2. Same provider + `can_resume` → native resume (D7).
3. Else compile bundle (goal + budget defaults) → launch target (D8).
4. Re-check git working tree; warn if dirty in a surprising way.

User-visible: one command. Internally: select → compile → launch.

### Phase 7 — VS Code sidebar / Context Composer  *(last)*

Only after Phases 1–4 exist, so the UI is a client of the compiler.

- VS Code sidebar: project, thread, sessions, Continue / budget.
- Context Composer: checkbox sessions → live bundle + token estimate
  → Launch.
- Optional tiny local HTTP/stdio API wrapping the same Python
  functions. Not a second business-logic tree.
- **Not** a full web app. **Not** N vendor plugins.

---

## Suggested GitHub issues

Open these as issues (labels `enhancement` + `continuity`) and treat
the checkboxes as the implementation contract.

| # | Title | Phase | Blocked by |
|---|---|---|---|
| 1 | Continuity Engine: tracking issue | 0 | — |
| 2 | `voyager merge`: multi-session context synthesis | 1 | — |
| 3 | WorkThread: project → thread → sessions | 2 | 2 |
| 4 | Goal-conditioned extraction (`--goal`) | 3 | 2 |
| 5 | Context Budget (`--budget auto\|Nk`) | 4 | 2 |
| 6 | Voyager Skill + `voyager skill install` | 5 | 2 |
| 7 | `voyager switch <agent>` | 6 | 3, 4, 5 |
| 8 | VS Code sidebar / Context Composer | 7 | 3, 4, 5 |

Issue 1 is the umbrella. Close it when 2–7 are done; 8 is explicitly
post-moat.

---

## Non-goals (this generation)

- Seamless **session** migration (system prompt / tool state / cached
  reasoning cannot move).
- Per-agent plugins as the first UI.
- Concatenating transcripts and calling it merge.
- LLM-in-core summarization, cloud APIs, new core dependencies.
- Replacing native resume for same-provider continue (D7).
- Writing into provider session directories.
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

## Open questions (do not block Phase 1)

1. **Bundle location.** Default `~/.voyager/bundles/` vs repo-local
   `.voyager/` (gitignored). Leaning global so provider cwd mess and
   accidental commits stay unlikely; `-o` always available.
2. **Thread identity.** Auto-cluster only, vs requiring `merge` to
   create a thread. Leaning: auto-cluster for `continue --repo`,
   persist when the user merges or switches.
3. **Optional LLM extra.** A later `[llm]` extra that turns quotes
   into resolved Decision objects. Must stay opt-in and off the core
   path. Not in Phases 1–6.
4. **Daemon.** Watcher already exists (`voyager watch`). A local API
   daemon is only justified when the VS Code client needs it (Phase 7).

When these need a product call, record it in `docs/DECISIONS.md`.
