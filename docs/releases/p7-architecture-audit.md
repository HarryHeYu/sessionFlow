# P7 ARCHITECTURE & REFACTORING REPORT

**Baseline** `683831d`. **Nothing was released; no tag moved; no npm/PyPI/VSIX
publish.**

An audit first, then three low-risk changes, each its own commit. The most
useful finding is a negative one: the two "obvious" problems were not problems.
There are **no module-level import cycles**, and `Store` and `continuity.py`
have **no duplicated responsibility to collapse**. Both were checked properly
before being left alone.

Method note: the first cycle scan reported three cycles. It counted
function-local imports, and a function-local import is the *fix* for a cycle,
not a cycle. Re-run with module-level imports only, the count is zero. That
correction is the difference between a real finding and an invented one.

---

## Repository Health

| | |
|---|---|
| Current HEAD | `6785f7c` (core), `f991c54` (plugin) |
| Core files | `voyager/` 60 files / 23,403 lines; `tests/` 84 / 22,894; `scripts/` 9 / 2,387; `vscode-extension/` 3 TS-JS / 1,018 |
| Tests | 1123 passed, 4 skipped, 31 subtests (local); see Quality Gates |
| Largest modules | `cli.py` 2,437 · `continuity.py` 1,478 · `store.py` 1,422 · `doctor.py` 1,278 |
| Largest functions | `cli.main` 493 · `startup.startup_continuity` 392 · `continuity.build_continuation_bundle` 295 · `scanner.run_scan` 258 |
| Largest class | `Store` 1,073 lines / 53 methods |
| Dependency cycles | **none at module level.** Two function-local imports were masking a would-be cycle; one is now gone |
| Dead code | 16 module-level names referenced nowhere. `ZeroTouchCapability` (14 lines), `resolve_provider_budget` (30), `delete_backup` (17), `checkpoint_delete` (7), `sha256_of` (4), plus small `capability_matrix` helpers |
| Duplicate implementations | `parse_ts` ×3 (byte-identical) — **fixed**; `_log_dir` ×5 and `_read_stdin` ×5 — **deliberate**, see below; `dashboard._q` == `thread_brief._many`; `_is_voyager_hook` ×2; `_session_start_list` ×2; `hook_command` ×2 |

### The duplicates that were not fixed, and why

`_log_dir` and `_read_stdin` are byte-identical across five
`*_session_start.py` modules — and those modules have **zero intra-package
imports**. They are standalone hook entry points spawned as short-lived
processes by the agent hooks. Consolidating them would add the package's first
import to five hook scripts, which is a design change to save fifteen lines.
**KEEP AS IS.** Same reasoning for the remaining small pairs: each has a
different lifetime or a different caller, and merging them would create a worse
abstraction than the duplication.

## Architecture Findings

| Module | Finding | Evidence | Decision |
|---|---|---|---|
| **CLI** | Mixed ten concerns, but only one was a real defect: the scan pipeline lived here while `auto.py` reached into it | `auto.py:378` did `from .cli import run_scan` inside a function — a lower layer depending on the command layer for a core primitive | **REFACTOR NOW** — scan pipeline → `voyager/scanner.py` |
| **Store** | 53 methods across 9 responsibilities (schema/migration, ingest, source lifecycle, WorkThreads, leases, pending, retention, search, plumbing) | The last three rounds changed only the ingest area; the thread/lease area was untouched. But: no cycle, transaction boundaries already documented and covered by 20 tests, and 36 test files call `store.replace_session` | **KEEP AS IS** — splitting moves methods off a class that 36 test files and the scan path use, for a module the brief calls the core stable boundary |
| **Continuity** | No duplicated responsibility | `continuity.py` imports `rank_candidates` from `ranker`, `order_rows_for_l1`/`session_band` from `provenance`, and `budget` lazily. Exactly one definition of each concept; no second context compiler | **KEEP AS IS** |
| **Integrations** | Two capability models coexist (`capability_matrix` vs `integrations/capabilities`) | Both are read: the matrix by `budget`, `cli`, `doctor`, `verification_harness`; the detector by all 8 integration classes and by tests. `tests/test_capability_consistency.py` already guards agreement | **KEEP AS IS** — this is issue #16, a design decision, and nothing new is inconsistent |
| **Doctor / API / MCP** | No layering violation | `mcp_server.py` does **not** import `cli`; it goes to `store`/`continuity`/`handoff`/`auto`. `api.py` calls `store.*` methods, never raw `conn.execute`. `doctor.py` has an explicit repair-kind taxonomy and `--fix` runs only `SAFE_DERIVED_REPAIR` | **KEEP AS IS** |
| **Providers** | No second name/capability registry; strict and loose repo matching remain separate | `same_repo` / `same_repo_loose` both live in `util.py` and are pinned by `test_util.py`, which asserts the two genuinely differ | **KEEP AS IS** |
| **Root / docs** | 15 tracked phase reports at the root | README, CHANGELOG, DOGFOOD and ROADMAP all cite `*_continuity_verdict.md` from the root; 13 others cite only each other | **REFACTOR NOW** — archived, links rewritten |

## Refactoring

### Implemented

**1. Scan pipeline → `voyager/scanner.py`** (`6785f7c`)

Moved verbatim by line range: `MAX_WRITE_FAILURES`,
`WRITE_FAILURE_GRACE_SECONDS`, the two failure-reporting helpers,
`_pre_read_fingerprints`, `run_scan`. `cli.py` 2,757 → 2,437; `scanner.py` 346.

The measurable outcome: **nothing in `voyager/` imports `cli` any more**, and
the function-local imports that were masking a module-level cycle dropped from
two to one.

**2. `parse_ts` → `voyager/util.py`** (`45e9717`)

Three byte-identical copies (verified by normalised-AST hash before moving).
Also removed a dead `timezone` import from two of them. `tests/test_util.py`
already guards against re-duplication; `parse_ts` joined its `SHARED` set and
its former-duplicators map, and two behaviour tests pin the parser.

**3. September phase reports archived** (`9a59513`)

Fifteen files moved to `docs/archive/root-reports-2026-09/`, all recorded by git
as renames. The root now holds only README, README.zh-CN, CHANGELOG,
CONTRIBUTING, CODE_OF_CONDUCT and SECURITY.

### Deferred

**Materialisation / streaming** — a real bottleneck, quantified, with a design,
filed as **[issue #18](https://github.com/HarryHeYu/sessionFlow/issues/18)**. It
changes the adapter protocol, which the brief routes to an issue rather than a
drive-by implementation. See Performance.

### Kept unchanged

`Store`'s internal structure, `continuity.py` and its relationship to
ranker/budget/provenance, the two capability models, the hook scripts'
deliberate self-containment, and the dead-code list above (removing four
unused helpers is not worth a commit of its own, and `delete_backup` may be an
intended API surface — it is recorded, not removed).

### Compatibility evidence

| requirement | evidence |
|---|---|
| CLI stays compatible | `run_scan`, `MAX_WRITE_FAILURES` and `WRITE_FAILURE_GRACE_SECONDS` are re-exported from `voyager.cli`; 8 test modules and `auto.py` import `run_scan` from there |
| `voyager scan` unchanged | 7 characterization tests written and **passing before** the move: stats keys, fresh/unchanged/forced scans, provider scoping, the output the workflow docs quote, quiet-mode silence |
| D6/D7/D8 not regressed | `test_scan_error_isolation.py`, `test_scan_source_change.py`, `test_concurrent_open.py`, `test_scan_consistency.py`, `test_scan_crash.py` all pass unchanged |
| `--force` semantics | `test_force_reindexes_and_keeps_its_meaning` — re-ingests, does not duplicate or lose |
| no new scan lock | nothing added; `test_concurrent_open.py` still passes |
| provider registration untouched | no change to `adapters/base.py` or any `register()` call |
| Bridge schema v1 | `BRIDGE_SCHEMA_VERSION = 1`, `integration_info()` keys unchanged |
| six tool contracts | plugin still declares the same six `sessionflow_*` tools |
| DSH can still call core | plugin `npm run build` clean; `npm test` 40/40 on 4 of 5 runs (see Quality Gates) |

## Performance

### Materialisation investigation

Call chain `discover → parse → materialize → replace_session → stamping`. The
materialise step is the only one not bounded by a single session.

`codex.scan()` builds `out: List[dict]`; `zcode.scan()` and `cursor.scan()` do
the same. `run_scan` cannot write until `ad.scan()` returns.

**First-write latency** (`E:\sessionflow-scratch\p15\materialize_probe.py`):

| sessions | `scan()` s | events | RSS after | MB/event |
|---|---|---|---|---|
| 2,000 | 19.0 | 20,000 | 79.9 | 2,318 |
| 10,000 | 132.6 | 100,000 | 300.3 | 2,289 |

* **1.42 s per 1,000 events** → **~24 minutes before the first write** at
  1,000,000 events
* **~2.3 KB per event**, linear → **~2.3 GB** at 1M events, before SQLite's page
  cache and the database file

**Peak RSS**: 300 MB at 100k events; `docs/PERF_O1.md` recorded 2,971.7 MB at
1M. Nothing this round touched materialisation, so the ~3 GB risk is unchanged.

**Streaming feasibility**: yes, and it can be done **additively** —
`scan(source_changed, emit=None)`, where `emit` writes as it goes and the
default still returns a list. That keeps `len(bundles)` / `bundles[0]` in
`test_zcode.py` working. The D5/D6/D7 invariants survive with the stamp still
happening once at the end and the pre-read snapshot taken *before* iteration
(which makes D7 strictly safer). The part that needs real work is error
surfacing: a parse failure would move from around the `scan()` call into the
iteration, which needs a kill-part-way test.

**100k / 1M status**: **not passed.** In the previous round the run grew the
index by zero bytes in ~50 minutes and was stopped. It is filed as issue #18
with acceptance criteria and has **not** been re-run. It is not claimed as a
result.

## Repository Hygiene

* **Root files**: 21 tracked markdown files → **6**. The rest moved to
  `docs/archive/root-reports-2026-09/`.
* **Documentation links**: 86 relative links checked before and after. Fourteen
  were broken by the move and fixed; **one pre-existing breakage** was found and
  fixed with them (`docs/releases/v1.1.0-community-readiness.md` pointed at
  `QUICKSTART.md` in its own directory). The check is now clean.
* **Historical reports**: nothing deleted, nothing edited. `dogfood_report.md`
  still says it is superseded, in its own words. The archive has an index
  explaining what each file is and pointing at the live documents.
* **Removed dead code**: none removed. One stray `.p7_static.json` written by my
  own analysis script into the repo root was deleted; scratch stays under
  `E:\sessionflow-scratch\`.
* Untracked local cruft remains at the repo root (`dist/`, `exports/`,
  `.pytest_cache/`, `%SystemDrive%/`, and a nested `dsh-sessionflow/`) — all
  gitignored, none tracked, left alone.

## Quality Gates

| gate | result |
|---|---|
| Core full pytest (local) | **1123 passed, 4 skipped, 31 subtests** (10m27s). 1114 + the 9 new tests |
| **Core CI** | run [`38425304649`](https://github.com/HarryHeYu/sessionFlow/actions/runs/38425304649) on `8688bba` — **10/10 jobs success**, ubuntu-latest + windows-latest, py3.10–3.13, plus the core-only installs: **1123 passed, 4 skipped, 31 subtests** |
| the 4 skips | unchanged data/platform gates: scratch root unset, no CJK sessions, Windows `chmod` |
| New tests | `test_scan_characterization.py` 7 · `test_util.py` +2 |
| Targeted suites | scan + auto: 54 passed; adapters + util + hooks: 90 passed |
| Plugin build | `npm run build` clean |
| Plugin tests | **40/40 on 4 of 5 runs**; one run reported 39/40. The suite contains deadline/timeout tests, so at least one is timing-sensitive. Not attributable to this round — the bridge contract is unchanged and the plugin never imports core internals — but it is a real flake and is reported rather than averaged away |
| Linux / Windows CI | same run, 10/10 |
| core-only job | included in the CI matrix and green |

Local and CI agree at 1123, which is 1114 + the 9 new tests — the suite grew by
exactly what was added, so nothing was silently skipped.

## GitHub

| | |
|---|---|
| Core commits | `9a59513` docs archive · `45e9717` parse_ts · `6785f7c` scanner extraction |
| Plugin commits | none (no bridge-contract change) |
| Issues | **#18 created** (materialisation). #12 and #16 unchanged and still open |
| Worktree | clean |
| origin/main sync | core `6785f7c`, plugin `f991c54`, both in sync |

## Final Decision

**TARGETED REFACTORING COMPLETE.**

Three changes, each with evidence rather than a size argument: one real layering
inversion removed, one genuine three-way duplication collapsed, one repository
root made to read like a finished project. Four of the seven audited areas were
left exactly as they were, on the evidence, and the audit's most important
output is the negative result that there are no import cycles and no duplicated
engine.

### Remaining risk areas

* **Materialisation (issue #18)** — the largest open item. 1M events still needs
  ~24 minutes and ~2.3 GB before anything is written, and a first index at that
  size has never completed. Measured, designed, not implemented.
* **`cli.main` is 493 lines** and still holds the whole argparse tree plus
  dispatch. It is not coupled to anything and it was not worth the blast radius
  this round, but it is the next-largest readability problem after the scanner.
* **`Store` is 1,073 lines / 53 methods.** The responsibilities are real and
  mixed. Splitting was judged worse than the status quo *given 36 test files
  calling into it*, not because the boundary is right.
* **One flaky plugin test.** Timing-sensitive, unidentified by name, present
  before this round. Worth pinning down before it is used as a gate.
* **16 unused module-level names**, including `delete_backup` (17 lines) and
  `resolve_provider_budget` (30). Recorded, not removed — `delete_backup` may be
  intended as an API surface.
* **Issue #16** (two capability models) remains a design decision, not a defect.
