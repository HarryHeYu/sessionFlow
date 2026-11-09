# Next-phase baseline audit — 2026-11-09

> **One round's record, not a description of the product.** This file records
> what was measured on 2026-11-09 and is frozen once the round closes. If it
> disagrees with the code, the code is right and this file is the bug. It exists
> so the next attempt starts from evidence instead of from memory.

## 1. How this was measured

| | |
|---|---|
| Host | Windows 11 (x64), Git Bash |
| Python | 3.13.12 (`voyager-dev` venv), `PYTHONPATH=` (delete-guard shim removed), `PYTHONDONTWRITEBYTECODE=1` |
| Node | 22.22.2 |
| Core suite | `pytest tests/ -q --basetemp=E:/sessionflow-scratch/audit/bt` |
| Plugin suite | `node --experimental-strip-types --test "tests/**/*.test.ts"` after `tsc --outDir` |
| GitHub state | `gh issue/pr/release/discussion` at the timestamps below |

No real provider session was read, no index was written, no release was cut.
Scratch output lives in `E:/sessionflow-scratch/audit/` (outside both repos).

## 2. Repository state

| | Core (`sessionFlow`) | Plugin (`dsh-sessionflow`) |
|---|---|---|
| Branch / worktree | `main`, clean | `main`, clean |
| HEAD | `b55a941` | `6fbed16` |
| Latest tag | `v1.3.0` (2026-10-20) | `v1.2.0` (2026-10-30) |
| Commits ahead of tag | **10** | **4** |
| Diff since tag | 24 files, +2509/−233 | 7 files, +154/−1 |
| Open issues | 1 (#12) | 2 (#1, #2) |
| PRs / Discussions | 0 / 0 | 0 / 2 |

The 10 unreleased core commits are: four Grok/Windows process fixes
(`1dc0a3d`…`85a3d36`), three documentation commits (`5995e9f`, `ca57244`,
`ff781ab`), and the three correctness fixes in `b55a941`. The plugin's four
unreleased commits are documentation only.

## 3. Test baseline

| Suite | Result | Notes |
|---|---|---|
| Core | **1338 passed, 6 skipped**, 31 subtests, **762 s** | 6 skips are environment-declared: case-insensitive Windows path test, symlink unavailable, `VOYAGER_SCRATCH_ROOT` unset, no Chinese-content DB, no sessions for bundle test, `chmod` cannot deny reads on Windows |
| Plugin | **54 pass, 0 fail, 0 skip**, 63.8 s | see build caveat below |

**Plugin build caveat (environment, not code).** `npm test` runs
`tsc -p tsconfig.json` first, and `tsc` cannot write `lib/` in this sandbox
(`EPERM` on every output file). `tsc --noEmit` exits 0, and
`tsc --outDir <scratch>` succeeds; its output is **byte-identical** to the
checked-out `lib/` (`diff -rq` empty). The suite was therefore run against a
correct build, but `npm test` as a single command cannot be used here. This is
recorded so it is not mistaken for a product failure.

## 4. Current Implementation Matrix

States are used strictly: `IMPLEMENTED` (code exists) ≠ `TESTED` ≠
`LIVE_VERIFIED` (observed against a real provider) ≠ `RELEASED` (in a tag).

| Area | State | Evidence |
|---|---|---|
| Session indexing, 8 providers | IMPLEMENTED · TESTED · RELEASED | `voyager/adapters/`, 96 test files |
| Unified index + FTS search | IMPLEMENTED · TESTED · RELEASED | `store.py`; short-phrase fallback in `b55a941` |
| WorkThread | IMPLEMENTED · TESTED · RELEASED | `auto.resolve_work_thread`, D13 lease |
| Continuity Engine (merge/switch/continue/handoff) | IMPLEMENTED · TESTED · RELEASED | `continuity.handoff_thread()` is the single engine (D14) |
| Evidence references + `evidence verify` | IMPLEMENTED · TESTED · RELEASED | CLI only; no API/MCP wrapper (MAINTENANCE #6) |
| Provenance classification | IMPLEMENTED for 3 of 8 providers | codex/claude/zcode only (MAINTENANCE #13) |
| MCP server | IMPLEMENTED · TESTED · RELEASED | `voyager-mcp` entry point |
| Zero-touch startup continuity | LIVE_VERIFIED for codex/claude/grok | `ZERO_TOUCH_LIVE_VERIFIED` in `capability_matrix`; ROADMAP records the 2026-09-25 live leg |
| Native resume | IMPLEMENTED · **NOT_VERIFIED** | commands are built; no resume observed succeeding (MAINTENANCE #10) |
| VS Code Context Composer + timeline | IMPLEMENTED · TESTED · **NOT_DISTRIBUTED** | `vscode-extension/` at 0.1.0, **no `.vsix`**, not on Marketplace |
| OpenCode | **NOT_SUPPORTED** | absent from `PROVIDERS` |
| DSH plugin, six tools | IMPLEMENTED · TESTED · RELEASED (v1.2.0) | `src/tools/`: search, recent, session, current-work, continue, merge |
| Plugin model-driven tool selection | OBSERVED, NOT REPEATABLE | MAINTENANCE #11; do not quote a success rate |

## 5. Published Release Matrix

| Release | Date | Contents | Assets |
|---|---|---|---|
| Core `v1.3.0` | 2026-10-20 | Evidence fingerprints + references, first-run docs, Grok fixes **not** included | wheel + sdist |
| Plugin `v1.2.0` | 2026-10-30 | DSH `0.2.1-rc.5` (next) and `0.2.2-alpha.2` (alpha) admitted | **no assets** (not published to npm) |

**The tag is not the branch.** `v1.3.0` predates the DSH v4 / UTF-8 / short-query
fixes; a user installing `@v1.3.0` today does not have them. Nothing in the
release notes claims otherwise, but the gap is the reason Phase 1 exists.

`docs/releases/` holds notes only through `v1.1.1`. There is **no
`v1.2.0.md` and no `v1.3.0.md`**, although `release.yml` reads
`v<version>.md` for the tag it publishes. Those two releases were created
without a notes file in that directory (their GitHub release bodies exist).

## 6. Provider Capability Matrix

From `capability_matrix.DECLARED` at `b55a941` (`LIVE` = LIVE_VERIFIED,
`UNIT` = UNIT_VERIFIED, `SUP` = SUPPORTED, `NF` = NOT_FOUND_IN_CURRENT_AUDIT).

| provider | discovery | indexing | native resume | startup hook | dynamic inject | native id at start | auto attach | zero-touch |
|---|---|---|---|---|---|---|---|---|
| antigravity | LIVE | LIVE | NF | UNIT | UNIT | UNIT | UNIT | NF |
| claude | LIVE | LIVE | SUP | LIVE | LIVE | LIVE | LIVE | **ZERO_TOUCH_LIVE_VERIFIED** |
| codex | LIVE | LIVE | SUP | LIVE | LIVE | LIVE | LIVE | **ZERO_TOUCH_LIVE_VERIFIED** |
| cursor | LIVE | LIVE | NF | UNIT | UNIT | UNIT | UNIT | NF |
| dsh | LIVE | LIVE | NF | NF | NF | NF | NF | NF |
| grok | LIVE | LIVE | SUP | LIVE | LIVE | LIVE | LIVE | **ZERO_TOUCH_LIVE_VERIFIED** |
| kiro | LIVE | LIVE | NF | UNIT | UNIT | UNIT | UNIT | NF |
| zcode | LIVE | LIVE | SUP | UNIT | UNIT | UNIT | UNIT | NF |

`SUP` on native resume means "a command can be constructed", **not** that a
resume has been observed working (MAINTENANCE #10). `NF` on DSH is the
`NOT_FOUND_IN_CURRENT_AUDIT` naming issue recorded as MAINTENANCE #14 — it does
not mean "DSH is not installed".

## 7. Known Issues Matrix

| Source | Item | Status on 2026-11-09 |
|---|---|---|
| MAINTENANCE #1–#9 | no fact-checking, fingerprint ≠ signature, mtime/size only, seq shifts, Unicode not normalised, verify is CLI-only, dashboard snapshot search, browser CI, SQLite flake | unchanged |
| MAINTENANCE #10 | native resume unverified | unchanged |
| MAINTENANCE #11 | model tool selection not repeatable | unchanged |
| MAINTENANCE #12 | `evidence verify --help` omits states/codes | **open, fix scheduled in Phase 1.3** |
| MAINTENANCE #13 | provenance covers 3/8 providers | documented 2026-11-09 |
| MAINTENANCE #14 | `NOT_FOUND_IN_CURRENT_AUDIT` reads as "not installed" | documented 2026-11-09 |
| Core #12 | performance debt (C1 reverted, C2 deferred) | **open; do not re-implement without statement counts at 100k+ sessions** |
| Core #18 | multi-session materialisation before first write | **closed**; streaming is in code (`Adapter.scan_streams`, `emit`, `replace_session(stamp_sources=False)` + `stamp_sources()`) |
| Plugin #1 | three DSH adapter defects | **fixed on main `b55a941`, not released**; issue still open |
| Plugin #2 | audit/provenance observations | documented (#13/#14); no code change by design |

## 8. Documentation Drift Matrix

| # | Where | Drift | Severity |
|---|---|---|---|
| 1 | Plugin `README.zh-CN.md:18` | header still says **v1.1.0** (English says v1.2.0) | high — user-visible version |
| 2 | Plugin `README.md:118` / `README.zh-CN.md:193` | compatibility table lists only **1.1.0**; 1.2.0 row missing | high — the table is the contract |
| 3 | Plugin `README.md:133-135` | "tested against plugin v1.1.0 … no plugin upgrade is needed" — stale since 1.2.0 | medium |
| 4 | Core `docs/releases/` | no `v1.2.0.md` / `v1.3.0.md`, while `release.yml` reads `v<version>.md` | medium — release-notes source of truth |
| 5 | Core `README.md` / `README.zh-CN.md` | correctly say v1.3.0; **no statement that main carries unreleased fixes** | low — Phase 1 must make released-vs-main explicit |

No case was found of a shipped feature being described as pending, or of a
pending feature being described as shipped.

## 9. Issue and PR audit

- **Core**: 18 issues, 17 closed, 1 open (#12). 0 PRs, 0 discussions.
  Closed ≠ fully done: #18's streaming is real in code (verified above), while
  #8's VS Code work is shipped as a feature but **not distributed**.
- **Plugin**: 2 issues, both open, both answered on 2026-11-09. 0 PRs,
  2 discussions (v1.2.0 announcement).
- No duplicate issue exists for OpenCode, native-resume verification, VSIX
  distribution, evidence-in-plugin, provenance coverage or version drift.

## 10. Corrections to the briefing's assumptions

| Assumption | Finding |
|---|---|
| "main 已修复三个问题，但尚未发布" | **Correct.** Fixed in `b55a941`, CI green, not in any tag. |
| "已支持 … Context Composer" | Implemented and tested, but **not distributed** (no VSIX, not on Marketplace). "Supported" would overstate it. |
| "dsh-sessionflow 已有六个核心工具及真实 DSH 集成测试" | Six tools confirmed. The suite is contract-level (54 tests); *live* DSH runs are recorded in `docs/p8/p9-live-verification.md`, not in CI. |
| CI matrix includes macOS | **It does not.** `test.yml` runs ubuntu 3.10–3.13 + windows 3.10–3.13. Phase 8.2's "add macOS" is a real gap, not a review. |
| Plugin released with assets | v1.2.0 GitHub release has **0 assets**; it is not on npm. |

## 11. Inputs this audit hands to Phase 1

1. Three fixes exist and are tested; the release gate is packaging, not code.
2. Five documentation drifts, all mechanical, none requiring design.
3. `evidence verify --help` (#12) is the one code-adjacent change: help text
   plus a regression test, exit codes unchanged.
4. `docs/releases/v1.3.1.md` must be created for `release.yml` to publish notes.
5. Upgrade path v1.3.0 → v1.3.1 needs no migration: no schema change since the
   tag except the additive `idx_events_ts` index (created by `CREATE INDEX IF
   NOT EXISTS` on open).

## 12. What this audit did not do

- No OpenCode research (Phase 3), no native-resume experiment (Phase 4), no
  VSIX build (Phase 5), no new plugin tool (Phase 6).
- No performance re-measurement beyond the 469k-event short-query benchmark
  recorded in `CHANGELOG` — Core #12 stays untouched.
- No release, no tag, no PyPI/npm publish.
