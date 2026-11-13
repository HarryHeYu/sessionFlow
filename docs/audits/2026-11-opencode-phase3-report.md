# OpenCode adapter — Phase 3 implementation report

> **Status: synthetic-verified only.** This round built and tested the adapter
> against **generated SQLite fixtures**. No real OpenCode install was read, no
> provider process was launched, and no native resume was attempted. Those
> claims stay `NOT_VERIFIED`; nothing here should be read as `LIVE_VERIFIED`.

## A. What was verified upstream, and from where

Checked on 2026-11-10 against `anomalyco/opencode` release **v2.2.6**
(2026-11-06). `packages/core/src/session/sql.ts` at that tag is
**byte-identical** to v1.19.5 (the version the Phase 2 design note used), so
the schema did not move between them.
Upstream sources were read from a scratch checkout of that tag, outside the
repository; they are not committed as an artifact.

| Fact | Source |
|---|---|
| Tables `session_v2`, `session_message`, `session_part`, `project` | `packages/core/src/session/sql.ts` |
| `session_part` has `session_id`, `ordinal`, `type`, `call_id` and `data` | same |
| Timestamps are `Date.now()` milliseconds | `packages/core/src/database/schema.sql.ts` (`Timestamps`) |
| Tool call + result live in ONE part's `state` (`status`, `input`, `content`, `error`) | `packages/schema/src/session-message.ts`, `packages/schema/src/tool.ts` |
| Message `type` values include `user`, `assistant`, `synthetic`, `system`, `skill`, `shell`, `compaction`, `idle`, `*-switched` | `packages/schema/src/session-message.ts` |
| Data root is `XDG_DATA_HOME/opencode` or `$HOME/.local/share/opencode` on all platforms | `packages/util/src/global-roots.ts` |
| `OPENCODE_DB` overrides the filename; channel builds use `opencode-<channel>.db` | `packages/cli/src/database-path.ts` |
| Pre-v2 schema uses `session` / `message` / `part` (different columns) | `packages/core/test/v1-migration.test.ts` (lines 834–885), `src/database/migration.ts` |

### Corrections to the Phase 2 design note

The design note was right about the recommended approach (read-only SQLite) but
wrong or incomplete on three points, all found by reading upstream code:

1. It listed `session_part` as `(message_id, ordinal) PRIMARY KEY, type,
   call_id, data`. The real table **also has `session_id`**, `time_created` and
   `time_updated`. Discovery of parts can be scoped by session.
2. It assumed `call_id` joins a tool *call* row to a separate *result* row.
   **There is no separate result row.** One part carries both: `state.input` is
   the call, `state.content` (array of `{type:"text",text}`) or `state.error`
   is the result. `call_id` is recorded as `tool_call_id` but is not the join
   key. The adapter emits `tool_call` always, and `tool_result` only when
   upstream actually recorded a completed or errored state.
3. It listed the message JSON shapes as "to confirm". They are now read from
   `packages/schema/src/session-message.ts`, including the distinction between
   a real `user` message and provider-generated `synthetic`/`system` messages.
   That distinction is what stops an agent's own claim from being indexed as a
   human instruction.

The design note also said to check `PRAGMA user_version`; upstream bootstrap
keys off the **presence of `session` / `session_v2` tables** instead
(`src/database/migration.ts`), so the adapter probes table/column shape and
treats `user_version` as non-authoritative.

## B. What was implemented

`voyager/adapters/opencode.py`, registered in `adapters/__init__.py`.

- **Read-only.** `file:...?mode=ro` plus `PRAGMA query_only=ON`. Never
  `immutable=1`: that would hide committed WAL pages and silently drop live
  turns. No migration, no writes, no deletion of provider files.
- **Schema probe.** `absent` / `ok` / `unreadable` / `unknown-schema`, with a
  real table read so a truncated file is not accepted as an empty index.
  Both `v2` and `legacy` shapes are detected by table + required columns.
- **WAL-aware incremental scanning.** `-wal` and `-shm` are returned by
  `discover()` deliberately: the scanner's `source_changed()` only stats
  discovered paths, so without this a running agent's new turns stay invisible
  until a checkpoint rewrites the main `.db`. Verified end to end through the
  real `Store` + `run_scan` (6 → 7 events, new turn searchable).
- **Mapping.** Session (title/cwd/repo/model/tokens/timestamps), messages,
  parts, tool calls and results, Unicode, reasoning, file parts, shell
  messages, inline assistant content fallback. `seq` is a stable per-session
  counter; `ts` is ms → s.
- **Provenance.** `origin` comes from the message `type` column, never from
  text. `synthetic`/`system`/`skill` messages become `meta` with
  `provider_system`, not `user`; a session with `parent_id` marks its user
  turns `provider_synthetic`.
- **Failure behaviour.** Unknown/corrupt/locked databases are named on stderr
  and reported via `unusable_sources()` / `unrecognised_sources()`, so a scan
  never quietly reports "0 sessions". A vanished database follows the existing
  O2 rule: history is retained as `SOURCE_MISSING`.
- **Streaming.** `emit=` produces the same bundles as the list form.
- **No native resume.** `can_resume = can_fork = False`, `resume_cmd = None`,
  and the capability matrix says `NOT_FOUND`.

### A real limitation, stated honestly

Opening a WAL database even with `mode=ro` can make **SQLite itself create a
`-shm` shared-memory index file** if one does not exist. That is a SQLite side
effect, not a write to session history; the adapter does not create it
deliberately and never touches session rows. The alternative (`immutable=1`)
would hide committed WAL pages, which is worse. The test asserts the main DB
bytes, WAL bytes and all row counts are unchanged, and documents the `-shm`
behaviour rather than claiming "no files are ever created".

## C. Tests (all synthetic)

`tests/opencode_fixtures.py` builds databases from reviewable DDL (no committed
binaries). `tests/test_opencode.py` — **27 passed**:

- both schemas, including the legacy `tool`/`callID`/`state.output` shape;
- tool call/result pairing, running tool with no invented result, error state
  without a fabricated exit code;
- synthetic/system messages not promoted to human turns; subagent sessions;
- Unicode (CJK/Japanese/Korean/emoji), malformed JSON rows, empty sessions;
- corrupt / unknown-schema / absent databases and the named warnings;
- read-only behaviour (DB bytes, WAL bytes and row counts unchanged);
- WAL discovery, WAL-driven rescan through the real scanner, idempotence,
  streaming equivalence, 2-character CJK search via `run_scan`, `SOURCE_MISSING`
  retention, and a 5000-message session.

Isolation: `tests/iso_guard.py` now covers `OPENCODE_DB`,
`VOYAGER_OPENCODE_DB` (explicit paths that bypass directory redirection) and
the `opencode/` directory derived from `XDG_DATA_HOME`.
`tests/test_isolation_guard.py` gained 4 regressions; a **mutation check**
confirmed the derived-path test actually fails when `_DERIVED_DATA_DIRS` loses
its `XDG_DATA_HOME` entry — the guard is load-bearing, not decorative.
Related suites: 62 passed / 2 skipped (isolation + adapter), 50 passed
(capability/doctor/adapters), 66 passed / 2 skipped (streaming/docs).

### Verification-level ledger

| Capability | Level | Evidence |
|---|---|---|
| Session discovery / indexing (synthetic DB) | `UNIT_VERIFIED` | `tests/test_opencode.py` |
| Message / part / tool mapping | `UNIT_VERIFIED` | same |
| WAL incremental rescan, idempotence, SOURCE_MISSING | `UNIT_VERIFIED` | real `Store` + `run_scan` in-test |
| Search / show / repo / merge / continue consume the index | `INTEGRATION_TESTED` via shared Store path; **not separately exercised for OpenCode** | existing CLI tests + `run_scan` search test |
| Real OpenCode install read | `NOT_VERIFIED` | no install observed |
| Native resume / startup hook / transcript writer | `NOT_FOUND` | not implemented; adapter denies resume |
| Cross-agent continuation from an OpenCode session | `NOT_VERIFIED` | no live agent run |

## D. Known gaps / next round

1. **Real-install verification is the top gap.** The message/part JSON shapes
   were read from upstream schema code, not from a row produced by a running
   OpenCode. A real install (any channel) should be pointed at with
   `VOYAGER_OPENCODE_DB`, scanned, and its rows compared with the fixtures.
   Only then may any cell move to `LIVE_VERIFIED`.
2. `session_v2` has more columns (`fork_*`, `summary_*`, `revert`, `permission`,
   `time_archived`, …) that are intentionally not indexed. Archived sessions
   are not filtered — a policy decision, not a bug.
3. `session_part.call_id` may be NULL on some builds. The adapter falls back to
   the part's `id`/legacy `callID`; where none exists, `tool_call_id` stays NULL
   rather than being invented.
4. Channel databases (`opencode-<channel>.db`) are all candidates and all get
   probed; a machine with several channels will index each. This is deliberate
   but untested against a real multi-channel install.
5. The static diagram (`scripts/make_diagram.py`) still shows eight platforms;
   it is a generated illustration, not the capability source of truth. Updating
   it is a separate asset change.
6. `OPENCODE_DB` can be a filename relative to the data dir or `:memory:`.
   Relative names are resolved against the data dir; `:memory:` is skipped
   because there is no file to index. Both are synthetic-tested, not real-tested.

## E. Compatibility impact

- Additive: one new provider in `enabled_adapters` and `PROVIDERS`. No change
  to `Store`, the continuity engine, the bridge schema, or any existing CLI
  contract. `voyager doctor` / `verify --matrix` gain one row whose states are
  `UNIT_VERIFIED` at best.
- No database migration. Existing indexes are untouched.
- No new runtime dependency (stdlib `sqlite3` only).
- Not released: this work lives on the `phase3-opencode` branch only.
