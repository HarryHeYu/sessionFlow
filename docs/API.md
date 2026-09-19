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
| `voyager thread list\|show\|create\|attach\|close\|unlock` | manage WorkThreads (unlock releases a lease) |
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

### Switch (`voyager.cli.cmd_switch`)

`voyager switch <agent>` composes: freshness scan → WorkThread
resolution → lease acquire (D13) → native resume XOR bundle → launch.
The lease is consumed via `store.thread_lease_acquire`; launch failure
releases it.

### Handoff (`voyager.handoff`)

```python
from voyager.handoff import build_context_package, handoff_command

package_md = build_context_package(store, session_row)
argv = handoff_command("codex", Path("handoff-codex-xxx.md"))
# argv = ["codex", "Read the file ... <handoff file> ..."]  (no shell)
```
