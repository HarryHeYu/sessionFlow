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
| `voyager handoff <id> --to <agent> [--launch]` | context package for another agent |
| `voyager continue [id] [--repo] [--to] [--launch]` | pick the newest session back up |
| `voyager brief [--hours N] [--repo] [--limit N]` | recent-activity digest |
| `voyager files <id>` / `voyager diff <id>` | file history (Claude version chain) |
| `voyager watch [--interval S]` | keep the index in sync automatically |
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

### Handoff (`voyager.handoff`)

```python
from voyager.handoff import build_context_package, handoff_command

package_md = build_context_package(store, session_row)
argv = handoff_command("codex", Path("handoff-codex-xxx.md"))
# argv = ["codex", "Read the file ... <handoff file> ..."]  (no shell)
```
