# API Reference

Voyager is primarily a CLI tool, but everything the CLI does is available
as a small Python API. This page documents both.

## CLI commands

| Command | Purpose |
|---|---|
| `voyager scan [--platform A,B] [--force]` | discover and index agent sessions (incremental) |
| `voyager list [--platform] [--repo] [--since N] [--json]` | list sessions, newest first |
| `voyager show <id> [--json]` | full event timeline of one session |
| `voyager search "query" [--limit N]` | FTS5 full-text search across all events |
| `voyager repo <pattern>` | cross-agent timeline grouped by repository |
| `voyager export <id> [--format md\|json] [-o FILE]` | export one session |
| `voyager resume <id> [--print]` | launch the native agent on that session |
| `voyager handoff <id> --to <agent> [--goal G] [--budget B] [--launch]` | context package for another agent |
| `voyager merge <ids...> [--goal G] [--budget B] [--to <agent>]` | multi-session continuation bundle + WorkThread |
| `voyager continue [id] [--repo] [--thread T] [--from ids] [--goal G] [--budget B] [--to] [--launch] [--no-launch]` | pick work back up (native resume or bundle) |
| `voyager switch <agent> [--thread T] [--repo] [--goal G] [--budget B] [--steal] [--no-launch]` | switch the active WorkThread to another agent (D13 lease) |
| `voyager doctor [--json] [--fix] [--dry-run]` | installation health check; `--fix` runs only SAFE_DERIVED_REPAIR (O4, D17) |
| `voyager verify [provider] [--all] [-v] [--json] [--matrix]` | read-only verification status: declared vs observed vs effective (O5, D18) |
| `voyager thread list\|show\|create\|attach\|close\|unlock` | manage WorkThreads (unlock releases a lease) |
| `voyager thread timeline <id> [--json] [--limit N] [--kind …] [--provider P] [--live-only\|--retained-only]` | lifecycle/milestone timeline (O3, D16) |
| `voyager brief [--hours N] [--repo] [--limit N]` | recent-activity digest |
| `voyager files <id>` / `voyager diff <id>` | file history (Claude version chain) |
| `voyager watch [--interval S]` | keep the index in sync automatically |
| `voyager api serve [--db PATH]` | stdio JSON-lines bridge (VS Code client) |
| `voyager stats` | index statistics |

Global option: `--db PATH` overrides the index location
(default `~/.voyager/index.db`).

Session ids accept prefixes; ambiguous prefixes list candidates and exit
with code 2.

## Python API

### Store (`voyager.store`)

```python
from voyager.store import Store

store = Store()                    # opens/creates ~/.voyager/index.db
store.scan_stats()                 # not needed — see stats()
store.sessions(provider=None)      # list[sqlite3.Row]
store.session("019f")              # (row, ambiguous_candidates)
store.events("codex:019f...")      # list[sqlite3.Row], ordered by (seq, id)
store.search("query", limit=50)    # FTS5 rows joined with sessions
store.stats()                      # {"sessions": .., "events": .., "by_provider": {..}}
store.close()
```

`Store` is a context manager: `with Store() as store: ...`.

### Writing (used by adapters / tools)

```python
store.replace_session(session_dict, events, provider,
                      source_path, extra_sources=[])
```

Atomically rewrites one session (events + FTS rows) and records the source
fingerprint — this is what makes scans idempotent. `prune_missing_sessions`
removes sessions whose sources vanished from disk.

### Adapter protocol (`voyager.adapters.base`)

```python
class Adapter:
    provider: str          # "codex", "claude", ...
    can_resume: bool
    can_fork: bool

    def discover(self) -> list[Path]: ...
    def parse(self, source: Path) -> dict | None:
        """Returns {"session": dict, "events": [dict], "extra_sources": [Path]}"""

    # multi-session sources (one artifact -> N sessions, e.g. a SQLite DB):
    def scan(self, source_changed) -> list[dict]: ...
```

Register with `register(MyAdapter())` in your module and add the module
name to `_MODULES` in `voyager/adapters/__init__.py`. Sessions/events are
plain dicts validated against `voyager.model.SESSION_FIELDS` /
`EVENT_FIELDS`; every event carries the raw provider record in
`raw_event`, and nothing from the provider is dropped.

### Continuity / ranker / budget (`voyager.continuity`, `voyager.ranker`, `voyager.budget`)

```python
from voyager.continuity import build_continuation_bundle
bundle = build_continuation_bundle(store, rows, goal="fix CI")   # rows = session rows

from voyager.ranker import extract_candidate_facts, rank_candidates
facts = extract_candidate_facts(store, rows)          # CandidateFact list
ranked = rank_candidates(facts, goal="fix CI")        # [(fact, score)] desc;
                                                      # goal=None keeps order
from voyager.budget import apply_budget, parse_budget
packed, info = apply_budget(bundle, parse_budget("compact"))
```

`rank_candidates` with no goal returns the facts in input order with
`score=None`. `apply_budget` packs section-by-section under a token
ceiling (chars/4 estimate), keeping the Evidence & Provenance header.

### Skill installer (`voyager.skill`)

```python
from voyager.skill import install_skills
results = install_skills(agent=None, force=False)  # codex/claude/grok
```

### Handoff engine (`voyager.continuity.handoff_thread`)

The **one** engine behind `voyager switch` / `continue` / `handoff` / `merge`
and the MCP `voyager_switch` / `voyager_handoff` / `voyager_merge` tools
(see [DECISIONS.md](DECISIONS.md) D14).

```python
from voyager.continuity import handoff_thread, resolve_handoff_source

res = handoff_thread(store, thread=tid, target="codex",
                     goal="fix CI", budget="compact", style="continuation")
# or: handoff_thread(store, sessions=rows, target="claude")
# or: handoff_thread(store, source="11111111-2222", target="claude")

res["action"]        # refused | invalid | error | native-resume | transcript | bundle
res["argv"]          # None when the target has no direct launch path
res["context_path"]  # the file that was written (None before the bundle step)
res["lease_token"]   # held only when work actually changed hands
res["pending_recorded"]
res["style"]         # "package" or "continuation" — what was ACTUALLY built
res["warnings"]      # dirty tree, refused lease, released lease, …
```

`resolve_handoff_source(store, thread=…, sessions=…, source=…)` normalises any
entry point to `{thread, thread_id, members, repo_root, source_provider,
source_session, scope}`. A session-scope call adopts the WorkThread that
already **contains** those sessions (`store.thread_find_containing`), so
handing off one member of a thread still leases that thread. A WorkThread is
never created here.

The engine never prints (MCP's JSON-RPC rides stdout), never launches, never
raises for expected paths, never writes a provider file and never creates a
WorkThread. It returns `argv`; the caller launches and owns the
release-the-lease-on-failure policy (`voyager.cli._handoff_launch`).

### Timeline (`voyager.timeline`)

The **one** WorkThread timeline (O3, DECISIONS D16). The CLI
(`voyager thread timeline`), the dashboard, the VS Code webview and the stdio
API op `thread_timeline` all consume this — no surface aggregates its own.

```python
from voyager.timeline import build_thread_timeline, render_text

tl = build_thread_timeline(store, "thr_abc123", limit=40,
                           kinds=["HANDOFF", "SOURCE_MISSING"],
                           provider="codex", state="retained")
tl["thread"]    # {id, title, goal, status, repo_root}
tl["events"]    # oldest first: id, timestamp, event_type, thread_id,
                # provider, session_id, title, summary, source_state, metadata
tl["total"], tl["shown"], tl["counts"], tl["filters"]

print(render_text(tl))       # the CLI view
```

Event types, and the single piece of canonical evidence behind each:

| type | evidence |
|---|---|
| `THREAD_CREATED` | `threads.created_at` |
| `SESSION_ATTACHED` | `thread_sessions.attached_at` |
| `HANDOFF` / `PROVIDER_SWITCHED` | `thread_pending.created_at` (+ source/target provider) |
| `CHECKPOINT_CREATED` / `BLOCKER_ADDED` / `BLOCKER_RESOLVED` / `TEST_GATE` / `COMMIT_OBSERVED` | `checkpoints` — explicit records only |
| `SOURCE_MISSING` | the append-only `thread_events` log (or `sessions.source_missing_since` for rows that predate it) |
| `SOURCE_RETURNED` | the `thread_events` log |
| `THREAD_CLOSED` / `THREAD_REOPENED` / `THREAD_ARCHIVED` | the `thread_events` log — `threads.status` has no timestamp and `updated_at` is also written by `thread_touch` |

Assistant prose is never scanned. `state="live"` / `"retained"` filters on
`source_state`; timestamps order the display and nothing else (they never settle
an ambiguity or establish authority — see D13/D14).

### Doctor / safe self-healing (`voyager.doctor`, O4 / D17)

```python
from voyager.doctor import (
    Issue, collect_issues, apply_fix, run, render,
    CRITICAL, WARNING, INFO,
    READ_ONLY_DIAGNOSIS, SAFE_DERIVED_REPAIR,
    USER_DECISION_REQUIRED, EXTERNAL_PROVIDER_ISSUE,
)

# The canonical issue model — every finding has this shape:
issues = collect_issues(db_path=Path("~/.voyager/index.db"))
for i in issues:
    i.code            # "STORE_UNAVAILABLE", "CACHE_STALE", ...
    i.severity        # "info" | "warning" | "critical"
    i.category        # "store" | "continuity" | "retention" | "lease" | ...
    i.message         # human-readable one-liner
    i.evidence        # what was observed
    i.suggested_action  # what to do
    i.auto_fixable    # can --fix handle this?
    i.repair_kind     # READ_ONLY_DIAGNOSIS | SAFE_DERIVED_REPAIR | ...

# The full report (backward-compatible dict + new keys):
report = run(db_path=Path("~/.voyager/index.db"))
report["issues"]       # list[dict] — each has code+severity+repair_kind
                        # AND legacy id/detail/kind for backward compat
report["blocking"]      # subset where severity == "critical"
report["warnings"]      # subset where severity == "warning"
report["leases"]        # O4.5: lease health
report["pending"]       # O4.4: pending-attach health
report["verification"]  # O4.6/O5.7: declared vs observed vs effective
                       #          + chains, evidence_count, best_chain,
                       #            last_live_event, blocked_reason

# Safe fix — only SAFE_DERIVED_REPAIR, never touches:
#   leases, ambiguity, retained history, pending, provider files
result = apply_fix(db_path=Path("~/.voyager/index.db"), dry_run=True)
result["fixable"]    # count of auto-fixable issues
result["executed"]   # list of {code, category, ok/action}
```

`apply_fix` currently clears stale cache entries (`CACHE_STALE`) whose
canonical source (sessions table) is intact. The FTS rebuild lives in
`voyager.db_health.apply_safe_repairs` and is reached via
`voyager db repair --apply`. `VACUUM` is maintenance (`voyager db compact`),
never a fix.

### Switch (`voyager.cli.cmd_switch`)

`voyager switch <agent>` composes: freshness scan → WorkThread
resolution → the handoff engine → render. The lease is consumed via
`store.thread_lease_acquire`; launch failure releases it. `--bundle`
forces the Continuation Bundle even for a same-provider member, and
`--steal` takes over a live lease explicitly.

### Handoff (`voyager.handoff`)

`voyager handoff <id> --to <agent>` is the *export* spelling of the same
engine (`style="package"`): it always compiles a Context Package, because
`--to` names the agent that will read it. The two functions below are the
package compiler and argv builder the engine calls.

```python
from voyager.handoff import build_context_package, handoff_command

package_md = build_context_package(store, session_row)
argv = handoff_command("codex", Path("handoff-codex-xxx.md"))
# argv = ["codex", "Read the file ... <handoff file> ..."]  (no shell)
```
