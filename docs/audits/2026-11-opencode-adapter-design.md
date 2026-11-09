# OpenCode adapter — design input for Phase 3

> **Design only. No adapter code exists.** Everything under "verified" was read
> from the upstream repository on 2026-11-09; everything under "to confirm" was
> **not** verified on a running install and must be checked before coding.

## 1. Recommendation

Read the **SQLite database directly, read-only**, and treat the CLI/API as a
fallback probe rather than the primary path.

Reasons, in order:

1. The database is the only source that carries `session_part.call_id`, which is
   what lets a tool result be joined to the command that produced it — the same
   property sessionFlow's bundles depend on for every other provider.
2. It is incremental by nature (`time_updated` per row), while an export command
   would re-materialise a whole session per scan.
3. A CLI/API path requires a running server and a matching CLI version; the
   database is a file, and its schema can be probed before parsing anything.

The cost is schema coupling. That is bounded by checking `PRAGMA user_version`
and the presence of `session_v2` before reading, and by failing closed with a
named reason when neither matches.

## 2. Verified facts (upstream `anomalyco/opencode`, release **v1.19.5**, 2026-10-28)

| Fact | Where |
|---|---|
| Database is SQLite, accessed through Drizzle | `packages/core/src/database/*` |
| Installed path: `~/.local/share/opencode/opencode.db` | `.opencode/skills/opencode-dev/SKILL.md`, `packages/cli/src/database-path.ts` |
| Dev checkout uses `opencode-local.db`; `OPENCODE_DB` overrides | same |
| Non-stable channels use `opencode-<channel>.db` unless `OPENCODE_DISABLE_CHANNEL_DB=1` | `packages/cli/src/database-path.ts` |
| Session table is `session_v2` (not `session`) | `packages/core/src/session/sql.ts` |
| `session_v2`: `id, project_id, workspace_id, parent_id, fork_session_id, slug, directory, path, title, version, cost, tokens_input/output/reasoning/cache_read/cache_write, agent, model, time_created/time_updated/time_archived/...` | same |
| `session_message`: `id, session_id, type, seq, data (JSON)`, unique on `(session_id, seq)` | same |
| `session_part`: `(message_id, ordinal)` PK, `type, call_id, data (JSON)`, indexed by session | same |
| `project`: `id, worktree (absolute), vcs, name` | `packages/core/src/project/sql.ts` |
| Session ids look like `ses_...` | `.opencode/skills/opencode-dev/SKILL.md` |
| Paths are stored with forward slashes on every platform and converted at the boundary | `packages/core/src/database/path.ts` |

## 3. To confirm before writing code

- The JSON shape inside `session_message.data` and `session_part.data` for
  `user`, `assistant`, `tool` and `reasoning` types (the type union exists in
  `session/message.ts`; field names must be read from a real row).
- Whether `session_part.call_id` is populated for every tool part, or only for
  some (this decides whether command↔result joining is reliable).
- Whether `time_created` is milliseconds (the `Timestamps` helper uses
  `Date.now()`, so almost certainly yes — verify one row).
- Which channels ship `session_v2` versus the older `session` table, and whether
  a migration keeps the old table readable.
- Whether an export command exists in the CLI (a search did not find one; the
  `opencode api` server may be the only structured surface).

## 4. Mapping to sessionFlow's canonical model

| sessionFlow | OpenCode | Note |
|---|---|---|
| `Session.native_session_id` | `session_v2.id` (`ses_...`) | stable |
| `Session.title` | `session_v2.title` | may be null → derive from first user message |
| `Session.cwd` / `repo_root` | `session_v2.directory`, `project.worktree` | two levels: session dir, project root |
| `Session.started_at` / `updated_at` | `time_created` / `time_updated` | ms → s |
| `Event.seq` | `session_message.seq` | already monotonic per session |
| `Event.kind` | `session_message.type` | `user` / `assistant` / `tool` / … |
| `Event.content` | `data` text fields, per type | to confirm |
| `Event.tool_name` / `tool_call_id` / `exit_code` | `session_part.type/call_id/data` | the reason to read SQLite |
| `Event.usage` | `session_v2.tokens_*` | session-level; per-message usage may exist in `data` |
| `Source path` | the `.db` file | one artifact → many sessions, like ZCode |

One artifact producing many sessions is already supported: ZCode does the same,
and `sources` is keyed by `(provider, path, sid)`.

## 5. Options considered

| Option | Reliability | Performance | Maintenance | Verdict |
|---|---|---|---|---|
| **A. Read-only SQLite** | high; schema is explicit and checkable | incremental by `time_updated`; no re-export | must track `session_v2` migrations | **chosen** |
| B. CLI export per session | medium; depends on a command that may not exist | O(all sessions) per scan | CLI output is not a contract | fallback only |
| C. `opencode api` server | medium; requires a healthy server | one round-trip per session | HTTP shape may change with the CLI | not for the scan path |
| D. Read `session_message` only, skip parts | high | fast | loses tool calls and exit codes | rejected: continuity needs them |

## 6. Adapter shape

Follow the existing contract, no special cases:

```python
class OpencodeAdapter(Adapter):
    provider = "opencode"
    can_resume = False          # until a resume command is verified, not before
    def discover(self) -> list[Path]:
        # 1. resolve the db path: OPENCODE_DB, else data dir + channel rules
        # 2. return [] when absent; never create the file
    def scan(self, source_changed, emit=None) -> list[dict]:
        # one sqlite3 connection (mode=ro), sessions ordered by time_updated
    def unusable_sources(self) -> list[Path]:
        # locked / unreadable db, so scan keeps the index instead of retiring it
    def unrecognised_sources(self) -> list[Path]:
        # an `opencode-*.db` we cannot attribute to a channel
```

Rules that already exist elsewhere and must be honoured here:

- open with `file:...?mode=ro` (never `immutable=1`: WAL means a live writer is
  normal, and `immutable` would hide committed pages);
- never write to the database, never run a migration;
- on a schema mismatch, return an error result for that source, not a crash and
  not an empty session;
- a vanished `.db` is `SOURCE_MISSING` (retained history), not a deletion.

## 7. Incremental scanning

Per session, compare `time_updated` with the fingerprint already recorded for
`(provider, path, sid)`. The database is one artifact, so the current per-file
fingerprint is too coarse; the scan should compute a per-session stamp
(`time_updated` + message count) and only re-read changed sessions. This is the
same shape as ZCode's whole-database rescan, which is already accepted — start
there, optimise only if a real index shows it matters.

## 8. Read-only and privacy boundary

- Open with `mode=ro`; SQLite will refuse writes even by accident.
- Never copy the database into the index; store only normalised events.
- `session_v2.metadata`, `permission`, `revert` and `model` are **not** needed for
  continuity; leave them out rather than storing opaque blobs.
- The test suite must point at a fixture database under the scratch root; the
  isolation guard already refuses real data paths.

## 9. Test fixtures

| Fixture | Purpose |
|---|---|
| `opencode-basic.db` | one session, user + assistant + one tool call |
| `opencode-tools.db` | tool call/result pairs with `call_id`, non-zero exit |
| `opencode-unicode.db` | CJK title and content, emoji |
| `opencode-legacy.db` | pre-`session_v2` shape (if still supported) |
| `opencode-corrupt.db` | truncated file → error result, no crash |
| `opencode-locked.db` | held open by a writer → `unusable_sources` |
| `opencode-large.db` | 5k messages, for a first-write timing sanity check |

Fixtures are generated by a script (like `make_demos.py`), not committed as
binaries, so the schema in the fixture is reviewable as code.

## 10. Risks and effort

| Risk | Mitigation |
|---|---|
| `session_v2` renamed again (it happened to DSH) | schema probe + `unrecognised_sources` warning |
| `call_id` not populated everywhere | fall back to ordinal pairing; record the limitation |
| Channel databases multiply | resolve by explicit `OPENCODE_DB` first; warn on unknown `opencode-*.db` |
| Live WAL writes during scan | read-only connection + one transaction per session |
| Tool JSON shape is large | cap the same way other adapters do (`FTS_BODY_CAP`, raw truncation) |

Rough effort: **2–3 days** — adapter + fixtures + tests (1.5), continuity
integration and edge cases (1), documentation and capability matrix (0.5).
No change to `Store` or the continuity engine is expected.

## 11. Acceptance criteria

1. `voyager scan --platform opencode` indexes sessions from the documented path
   and from `OPENCODE_DB`, and does not create a database when none exists.
2. A session appears in `list`, `search` (including a 2-character CJK query),
   `show`, and `repo`.
3. Tool calls carry `tool_name`, `call_id` and `exit_code` where upstream has
   them; where it does not, the bundle says so rather than inventing a value.
4. Re-scanning an unchanged database writes nothing; touching one session
   re-reads only that session.
5. A corrupt, locked or unknown-schema database produces a named error or a
   warning — never a silent "0 sessions indexed".
6. `verify --matrix` reports `session_discovery` / `session_indexing` as
   `UNIT_VERIFIED` until a real install is observed, then `LIVE_VERIFIED`.
   `native_resume` stays `NOT_FOUND` until a resume is actually observed.
7. Fixtures cover the six cases in §9, and the suite passes with the isolation
   guard active.
