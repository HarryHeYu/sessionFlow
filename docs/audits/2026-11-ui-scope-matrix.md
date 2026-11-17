# UI candidate scope matrix — all eight original candidates (2026-11-17)

Status: FROZEN audit. Written against Core `main` at `682d816`, Core release
`v1.5.0`, Plugin release `v1.3.0`, bridge `schema_version = 1`.

This note answers the exact question in Core issue #32 ("Candidate features"):
for **each of the eight original candidate rows**, name the exact surface, give
one of the three verdicts, state the gap, the compatibility risk, and the
verification evidence. It deliberately replaces the earlier four *re-grouped*
surfaces (Selector / Timeline / Composer / Handoff confirmation) — those were
an MVP narrowing, not the eight rows the issue asked for.

Governing rules (unchanged): no second data path, no UI-side continuity logic,
no Store / Bridge schema change for a UI feature, handoff execution stays a CLI
write (single engine `continuity.handoff_thread()`).

Named ops are from `voyager/api.py:_OPS` (10 ops) at this revision:
`overview`, `thread_detail`, `sessions`, `bundle_preview`, `thread_timeline`,
`search`, `current_work`, `continue_context`, `evidence_verify`,
`integration_info`. CLI commands are named literally.

| # | Feature | Exact Core API / CLI surface | Verdict | Gap | Compatibility risk | Verification evidence |
|---|---|---|---|---|---|---|
| 1 | VS Code sidebar (sessions / threads) | ops `overview` (+ optional `repo`), `sessions`, `thread_detail` | **BUILDABLE NOW** | none for the tree; `overview`'s `repo` filter is loose-matched (D19), so the UI must present it as display context, not an exact match | older Core lacking `sessions` must be gated by `integration_info().ops`; a client that assumes the op exists fails with `unknown op` (capability error, not empty data) | `selector.js` calls `overview` + `thread_detail`; real bridge transcript ids 2/4/5; `tests/test_selector_race.py` |
| 2 | Context Composer | ops `bundle_preview` (`session_refs`, `goal`, `budget`, `target`) and `continue_context` (read-only compile) | **BUILDABLE NOW** | no gap for preview; handoff execution deliberately stays CLI (`continue`/`handoff`, D13 lease + pending + file write) and is not exposed as an API write op | same as row 1; preview ≠ execution (index freshness, git snapshot at preview time, lease refusal is a state not an error) — wording must never read as "the agent completed the task" | `composer.js`; real bridge transcript id 7 (bundle + `estimated_tokens: 312` + dropped/trimmed), caveat text rendered |
| 3 | Cross-agent session timeline | op `thread_timeline` (thin pass-through to `build_thread_timeline`; `limit`, `kinds`, `provider`, `state`) | **BUILDABLE NOW** (first screen); deep paging **NEEDS A SMALL ADDITIVE OP** | `limit` keeps the newest N but there is **no cursor / offset / before** parameter, so there is no way to page past the first screen. Smallest change: an additive `before`/`offset` param on the same op — response shape unchanged, `schema_version` stays 1 | none for current use; a client must not build a second timeline aggregation (two aggregations drift invisibly) | `timeline.js` consumes `thread_timeline` verbatim; real bridge transcript id 6 (`THREAD_CREATED`, `SESSION_ATTACHED`, `SOURCE_MISSING`); `tests/test_timeline.py` |
| 4 | Multi-session drag-and-drop grouping | CLI `voyager merge <sessions...>` (`nargs="+"`, arbitrary set) for persistence; op `bundle_preview(session_refs=[...])` for a **read-only** preview of the same set | **BUILDABLE NOW** (read-only preview); in-app persistence **NEEDS A SMALL ADDITIVE OP** | there is **no `merge` op**: merge is a write (D13 lease + pending + bundle file), deliberately CLI-only. `bundle_preview` already accepts an arbitrary session set, so grouping/preview needs no new op; only creating the WorkThread from the UI would, and that moves a write surface into the API — out of scope here | a hypothetical future `merge` op must keep `BRIDGE_SCHEMA_VERSION = 1` (new op name, not a shape change) and must not change member identity or `ord` (Q1-C2 / #31) | `cli.py` `merge` parser `sessions nargs="+"` and `_thread_from_merge`; `bundle_preview` real transcript id 7 with 2 refs |
| 5 | Continuation Bundle preview | op `bundle_preview` → `bundle`, `estimated_tokens`, `budget`, `dropped`, `trimmed` | **BUILDABLE NOW** | returns fully rendered markdown, so the UI renders it **once**, verbatim — no second markdown renderer. Response does **not** carry the index timestamp / git snapshot used, so the earlier "as of …" label cannot be produced from op data; adding those fields changes the response shape (schema-version decision, not made here) | rendering provider caveats verbatim is mandatory: `SOURCE_MISSING`, `unverified`, *"MATCH means the reference matches, not that a claim is true"*; stripping them misrepresents provenance | real bridge transcript id 7; `composer.js` `messages` contract and hint text; caveat-preservation check in the headless webview run |
| 6 | Evidence / provenance visualisation | op `evidence_verify` (5 states in `result.status`); CLI `voyager provenance coverage --json` and `voyager provenance enrich` | **NEEDS A SMALL ADDITIVE OP** for coverage/origin view; `evidence_verify` itself is BUILDABLE NOW | `evidence_verify` is present and sufficient for per-reference checks, but there is **no bridge op for provenance coverage / origin counts** — only the CLI. Smallest change: a read-only `provenance_coverage` op wrapping `provenance.coverage()`; new op name, `schema_version` stays 1 | the five states must not be flattened (`INVALID_REFERENCE` / `UNVERIFIABLE` are capability/verdict distinctions); `MATCH ≠ claim true`; no digest ⇒ `UNVERIFIABLE`. Synthetic 100% coverage is not a real-coverage claim (Plugin #2) | `api.py:_OPS` has `evidence_verify` but no coverage op; `provenance.coverage`/`enrich`; `tests/test_bridge_ops.py`; real coverage run in the Plugin #2 evidence note |
| 7 | DSH plugin config / status display | op `integration_info` (version, `schema_version`, ops, capabilities); CLI `voyager integrate status` / `verify --json` | **NEEDS A SMALL ADDITIVE OP** | `integration_info` reports the Core version and op list only — it has **no per-provider installation or capability state**. Showing DSH installed-vs-capability needs a new read-only provider-status op (new name, `schema_version` stays 1) or a response-shape change (would require a version decision) | must keep `capability_state` distinct from `installation_state` (Core #14); `NOT_FOUND_IN_CURRENT_AUDIT` is a capability-scope value, not an installation verdict; older Core without the op ⇒ clear capability error | `api.py:integration_info` shape; `capability_matrix.py` `capability_state`/`capability_scope`/`installation_state`; `tests/test_bridge_ops.py:test_integration_info_shape` |
| 8 | Agent capability matrix view | CLI `voyager verify --matrix --json` (reads `capability_matrix.matrix()`); no bridge op | **NEEDS A SMALL ADDITIVE OP** | the CLI JSON is stable and machine-readable (provider → dimension → `{state, note}`) but is CLI-only; rendering it in a webview via the bridge needs a new read-only op wrapping `capability_matrix.matrix()`; new name, `schema_version` stays 1 | machine-dependent dimensions and `NOT_FOUND_IN_CURRENT_AUDIT` / `UNIT_VERIFIED` must render verbatim; never promote to `LIVE_VERIFIED` without evidence; older Core may report a smaller provider set | `verification_harness.py:cmd_verify` matrix branch; `capability_matrix.matrix`/`DIMENSIONS`/`PROVIDERS`; `tests/test_capability_matrix.py` |

## Verdict summary

- BUILDABLE NOW: rows 1, 2, 3 (first screen), 4 (read-only preview), 5.
- NEEDS A SMALL ADDITIVE OP (all keep `schema_version = 1`; none implemented here):
  row 3 (deep timeline paging), row 4 (in-app group persistence), row 6
  (provenance coverage), row 7 (provider status), row 8 (capability matrix).
- NEEDS A NEW PROTOCOL: **none**. Therefore the issue's rule "a new-protocol row
  links a separate design issue and is not implemented here" is N/A — no such
  row exists, and no design issue needs to be linked from this matrix.

## No schema / core-engine change

Rows implemented or assessed here touch no `voyager/store.py`, no
`voyager/continuity.py`, and no `BRIDGE_SCHEMA_VERSION` change. The add ops
proposed in rows 3/4/6/7/8 are *future*, additive, read-only, and explicitly
out of scope of issue #32.

## UI-level interaction evidence (Condition 2)

Recorded, reproducible, and NOT a mock:

- `build_ui_evidence.py` builds a synthetic index (two WorkThreads, a
  `SOURCE_MISSING` retained member, explicit origins) under
  `E:/sessionflow-scratch/r1718/` and drives the **real**
  `python -m voyager.api serve` subprocess: 8 ops, exit 0, 0 errors.
  Transcript: `ui-bridge-transcript.jsonl`.
- `drive_webviews.js` feeds those **actual** API results into the **actual**
  shipped inline scripts of `selector.js`, `timeline.js`, `composer.js` in a
  `node:vm` DOM stub. All 11 checks pass: 2 threads listed, selection change,
  stale-response isolation, `SOURCE_MISSING` tag, real not-found error, error
  vs empty-index state, timeline `THREAD_CREATED`/`SOURCE_MISSING`, bundle
  rendered with caveat text and 312-token estimate.
- **NOT VERIFIED:** the VS Code Extension Host shell (activation,
  `WebviewPanel` lifecycle, command palette, real host→webview message
  delivery). Launching it opens a GUI window, which is prohibited in this
  environment, and the repo has no `@vscode/test-electron` harness. A headless
  DOM-script run is not a substitute for an Extension Host run and is not
  claimed as one. Tracked separately (Extension Host E2E / VSIX issue).
