# DSH provenance structure reconnaissance

Date: 2026-11-16. Scope: a read-only structural sample of 12 local
`session*.jsonl.zstd` files (19,556 parseable rows). The probe recorded field
names, value types and a small allowlist of structural enum values. It never
printed or stored message text, titles, commands, tool arguments or repository
content. No sampled value from a free-text field appears in this report.

## Observed row types

The dominant types were `tool/result` (3,603), `tool/call` (3,595),
`step/start` / `step/end` (3,410 each), `assistant/message` (3,403) and
`user/message` (197). Lower-volume lifecycle/control rows included
`agent/inbox/spliced`, `turn/start`, `turn/end`, `session/title`,
`request/context`, `compaction/*` and `system/message`.

## User rows

All 197 sampled `user/message` rows carried `data.content`, `data.source`,
`data.role` and `data.id`; `data.role` was always the literal `user`.

`data.source.kind` was the decisive structural field:

| source.kind | count |
|---|---:|
| `user` | 115 |
| `goal` | 35 |
| `skill-catalog` | 14 |
| `tool-jobs` | 10 |
| `plugin` | 7 |
| `runtime-context` | 7 |
| `agent-instructions` | 2 |
| `compact-checkpoint` | 2 |
| `repeat-tool-reminder` | 2 |
| `model-selection` / `tool-goal` / `user-approval` | 1 each |

A `role=user` row is therefore not by itself a human turn. The existing
synthetic fixture uses the older `client` value; that alias is supported for the
fixture only and is not counted as real-world coverage.

## Assistant and tool rows

`assistant/message` content blocks were `tool-call` (3,595), `reasoning`
(1,735) and `text` (1,034). These are provider-produced records; their
provenance is `provider_system`, while `kind` continues to distinguish
assistant text, reasoning, tool calls and tool results.

## Session flags

All 12 sampled `session` rows had `delegationDepth = 0`. There is no observed
sample for a delegated/subagent DSH session, so the `delegationDepth > 0`
branch is pinned by synthesis and reported as synthetic-only evidence.

## Classifier decision

- `user/message` + `data.role == "user"` + `source.kind == "user"` -> `human`.
- `source.kind == "client"` (synthetic fixture compatibility) -> `human`.
- `source.kind` in `agent-instructions`, `skill-catalog`, `runtime-context`,
  `plugin` -> `provider_bootstrap`.
- Other observed provider-generated source kinds -> `provider_system`.
- `delegationDepth > 0` -> `provider_synthetic` for user rows (synthetic-only).
- `assistant/message`, `tool/call`, `tool/result`, `error*` -> `provider_system`.
- Missing or novel `source.kind`, or an unsupported row type -> `UNKNOWN`.

No text heuristics, no model call and no mutation of provider files are used.
Coverage is reported as classified and `UNKNOWN` counts, not as a single
percentage.
