# OpenCode adapter — v2.0.x inline-schema compatibility and real-install gate

**Status**: real-install evidence recorded; **synthetic results remain
`UNIT_VERIFIED`**. This document is the gate record for the change that added
support for the intermediate v2.0.x schema. It contains **no user message text,
no database path, and no file hash** — only structural facts and counts.

Related: [`2026-11-opencode-adapter-design.md`](2026-11-opencode-adapter-design.md)
(design input), [`2026-11-opencode-phase3-report.md`](2026-11-opencode-phase3-report.md)
(Phase 3 implementation, frozen; its "no real install" statement applies to that
round only).

## 1. Why this change exists

Phase 3 was validated against upstream `anomalyco/opencode` v2.2.x, where a
message's parts live in a separate `session_part` table. A real install on the
maintainer machine runs **v2.0.29**, an intermediate shape:

- Tables include `session_v2` (36 columns), `session_message` (7 columns) and
  `project`, among 20 tables in total.
- **There is no `session_part` table.**
- `session_message.data` carries the content **inline**: a `user` row has a
  `text` string; an `assistant` row has a `content` array of
  `text` / `reasoning` / `tool` items; a tool's exit code is in
  `state.metadata.exit`, not `state.exit`.

Because `SCHEMAS` required `session_part` for the `v2` shape, `detect_schema()`
matched nothing, `probe()` returned `unknown-schema`, `discover()` returned an
empty set, and **no session from that install was indexed**. The 27 synthetic
tests could not catch this: their fixtures model v2.2.x. That is the concrete
reason a synthetic pass must never be reported as `LIVE_VERIFIED`.

## 2. What changed

`voyager/adapters/opencode.py`:

- **Third shape `v2-inline`** in `SCHEMAS`, detected **structurally**: it
  requires `session_v2` + `session_message` and, via `SCHEMA_FORBID`, the
  **absence** of `session_part`. No product version string is consulted, so a
  v2.2.x database (which also has `session_message`) still resolves to `v2`.
- **Inline parts**: `_inline_parts()` reads `data.content`; `text` → assistant
  event, `reasoning` → reasoning event, `tool` → tool call/result through the
  existing `_tool_events()`.
- **Tool exit code**: `_tool_exit()` reads `state.exit`, `state.exit_code` or
  `state.metadata.exit`, and treats `0` as a real code (not "missing").
- **Provider-generated rows** (`system`, `synthetic`, `compaction`, `idle`,
  `model-switched`, …) stay `meta` with `origin=provider_system`; a `compaction`
  row's `summary` is used when present. Nothing is promoted to a human turn.
- **Honest degradation**: a running tool gets no fabricated result; empty user
  text emits no user event; unknown types are skipped rather than guessed.
- **Discovery priority**: an explicit `VOYAGER_OPENCODE_DB` / `OPENCODE_DB` is
  now **authoritative** — the default data-directory database and channel
  builds are not probed when an override is set. This prevents an
  operator-named (or test-named) database from silently causing a read of a
  real HOME database.

Unchanged: read-only access (`mode=ro` + `PRAGMA query_only=ON`, never
`immutable=1`), `Store`, continuity, the bridge schema, every other adapter,
`can_resume = can_fork = False`, and the five-state evidence semantics.

## 3. Real-install gate

Method: the real database and its `-wal` / `-shm` were copied byte-for-byte
into an isolated scratch directory; **every SQLite connection targeted the
copy**. `HOME`, `USERPROFILE` and `XDG_DATA_HOME` were all redirected into
scratch, and the run asserts that no candidate path lies outside it. The real
directory was only hashed before and after.

### Gate A — schema and query-result comparison: PASS

- `probe()` → `('ok', 'v2-inline')`.
- `discover()` → the database plus its `-wal` and `-shm` companions.
- **8 of 8** real sessions indexed; **2,539** real message rows parsed.
- Independent read-only `sqlite3` oracle (no adapter code) agreed on: 20 tables,
  `session_v2` = 8 rows, `session_message` = 2,539 rows, `user_version` = 0.
- Per-session event kinds observed: `user`, `assistant`, `reasoning`,
  `tool_call`, `tool_result`, `meta`; tool `exit_code` captured (including `0`).
- `session_message.type` values seen: `assistant`, `user`, `idle`, `compaction`,
  `system`, `synthetic`, `model-switched`; only the first two map to human /
  assistant content, the rest stay meta or are skipped.

### Gate B — no modification of user data: PASS

| Check | Result |
|---|---|
| Real `.db` SHA-256 before/after | identical |
| Real `-wal` SHA-256 before/after | identical |
| Real `-shm` SHA-256 before/after | identical (final isolated run) |
| Row counts, all 20 tables, copy vs oracle | identical |
| `PRAGMA user_version` | unchanged |
| New files in the isolated dir | none |

An earlier ad-hoc probe that set an override **without** isolating `HOME`
caused SQLite to rewrite the real `-shm` (a reconstructible shared-memory
index; `.db`, `-wal` and all row counts were unchanged). That is what motivated
the override-priority change in §2; after it, the run is fully hermetic.

### Gate C — capability ledger: HELD

| Capability | Level |
|---|---|
| Synthetic v2.2.x / legacy / v2.0.x mapping | `UNIT_VERIFIED` |
| Real v2.0.x discovery, message/role mapping, inline tool calls, timestamps | **real-install observed** (Gate A) |
| Native resume / startup hook / transcript writer | `NOT_FOUND` (adapter denies resume) |
| Cross-agent continuation from an OpenCode session | `NOT_VERIFIED` |
| File attachments in `user.files` on the inline shape | `NOT_VERIFIED` (empty in the observed data) |
| Multi-channel real install | `NOT_VERIFIED` |
| `LIVE_VERIFIED` | **none claimed** |

## 4. Limits of this evidence

- One install, one platform (Windows), one version (v2.0.29). Other v2.0.x
  builds may differ; the detection is structural so a shape change fails
  loudly as `unknown-schema` rather than being misparsed.
- `message_count` is the count of `user` + `assistant` **content** events, not
  raw `session_message` rows (the existing contract, unchanged).
- No agent was launched, no native resume attempted, no release made.
- A synthetic v2.0.x fixture now covers this shape so the gap cannot silently
  return; it remains synthetic and is labelled as such.
