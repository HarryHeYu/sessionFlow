# WorkBuddy provider reconnaissance (2026-11-16)

Scope: decide whether Tencent WorkBuddy desktop has a stable, read-only session
format that sessionFlow can index without guessing. This is **structural
reconnaissance**, not a live continuity test and not a privacy audit of any
conversation. No real message text, tool output, cwd, title or session id was
copied into this report or into tests.

## Install facts

| Fact | Observed value | Source |
|---|---|---|
| Desktop product version | `5.9.4` | `~/.workbuddy-ai/last-launch.json` |
| Build | `fa0a0901906addaa1a4259f6df347388a5563ded` | same file |
| Bundled CLI | `@genie/agent-cli` `2.165.0` | `resources/app.asar.unpacked/cli/package.json` |
| CLI description | "Use CodeBuddy, Tencent's AI assistant, right from your terminal" | same file |
| Local data root | `~/.workbuddy-ai/` | directory listing |

The installed desktop agent is therefore present and identifiable. The
`codebuddy.ai/hostKind` key inside session records is the provider's own marker
for the host surface; the adapter records it when present but does not use it
as a version gate.

## Two different stores

`~/.workbuddy-ai/workbuddy.db` is **application metadata**, not a transcript
store. A read-only structural query found 11 tables: migrations, automations,
automation runs/delivery/runtime state, `buddy_snapshots`, `session_usage`,
`sessions`, `workspace_trust_decisions` and `workspaces`. The `sessions` table
has identity/title/status/time fields and no message body table; `workspaces`
has only `path` and `last_opened_at`. Migration files explain the schema: the
baseline defines `sessions`/`workspaces`, a 0010/0011 pair briefly adds and then
drops `cloud_conversations`, and 0012 folds cloud transport fields into
`sessions`. The adapter does not read this database.

Transcripts are JSON Lines files:

```text
~/.workbuddy-ai/projects/<project-slug>/<sessionId>.jsonl
```

Each file carries one `sessionId`; the first record's `sessionId` matched the
file stem in every checked file. Sibling `*.meta.json` and
`*.file-rollback.ndjson` files are freshness companions only. The similarly
named `~/.workbuddy-ai/workspace/sessions/<uuid>/` directories are file-backup
and snapshot workspaces (`snapfile`, `modify_backup`, permission files), not
conversation logs — one of them also contained a checked-out source tree, so
recursive enumeration would have been both noisy and wrong.

## JSONL shape

Observation was limited to JSON keys, value types, event-type counts and
timestamp magnitude. Eight event types were seen:

| `type` | Structural fields used by the adapter |
|---|---|
| `session-meta` | `meta["codebuddy.ai/hostKind"]` |
| `ai-title` | `aiTitle` |
| `message` | `role` (`user`/`assistant`), `content[]`, `cwd`, `providerData` |
| `reasoning` | `content[]`, `rawContent[]`, `providerData` |
| `function_call` | `callId`, `name`, `arguments` (JSON string), `cwd` |
| `function_call_result` | `callId`, `name`, `output`, `status`, `providerData.toolResult` |
| `resend-fork-notice` | ignored (no turn) |
| `file-history-snapshot` | ignored (no turn) |

Message text is a list of blocks with `type` `input_text` or `output_text` and
a string `text`. Tool arguments are JSON strings; tool output is an object or
list. Provider data carries model identifiers, request ids and usage fields.
`providerData.isMeta` / `clientMeta` are treated as provider-generated records,
not as human turns. Timestamps are 13-digit epoch milliseconds.

## Adapter decision

Implemented `voyager/adapters/workbuddy.py`:

- provider id `workbuddy`, registered as the tenth adapter;
- `VOYAGER_WORKBUDDY_PROJECTS` is an authoritative override, so a test cannot
  silently read the real profile;
- read-only `open()`, malformed JSON lines skipped, unreadable/unknown files
  reported through `unusable_sources()` so the scanner retains history (O2);
- no writes to WorkBuddy files, no `Store()` in the diagnosis path, no native
  resume, no startup hook, no transcript writer;
- capability cells stay `UNIT_VERIFIED`; the local install proves only that
  the shape exists, not that live continuation works.

## Boundaries

- No real conversation text was read to design or test the field mapping.
  Tests use generated fixtures whose strings are synthetic.
- The adapter does not reconstruct `cwd` from an absent field, does not invent
  shell exit codes, and does not turn a result-less running tool into a
  completed tool result.
- Evidence references from this provider follow the normal `(sid, seq)` +
  digest contract; `MATCH` is still reference consistency, not truth.
- Native resume and cross-agent continuation remain `NOT_VERIFIED`.
