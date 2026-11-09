# Development report — 2026-11-09 (Phase 0–1)

> **Scope of this report: Phase 0 and Phase 1 only.** Phases 2–10 of the
> next-phase plan are **not started**; nothing below should be read as a claim
> that they were done. The report is written to the structure the plan asks for
> so the next round can append to it rather than rewrite it.

## A. Executive summary

Two rounds of work, both on `main`:

1. **Phase 0 — baseline audit** (`c4d8e6e`). Measured both repositories instead
   of trusting old documents: HEADs, tags, test counts, provider matrix, issue
   state and documentation drift. Three assumptions in the plan were corrected
   (Context Composer is implemented but **not distributed**; CI has **no macOS**
   leg; plugin v1.2.0's GitHub release has **no assets**).
2. **Phase 1 — correctness release** (`b88797a`). Closed the gap between "fixed
   on main" and "installable": version 1.3.1, changelog, release notes, help
   text, boundary tests, and a built-and-verified candidate.

Remaining risk is unchanged in kind: native resume is still unverified, no macOS
run exists, and the plugin's documentation still advertises v1.1.0 in one
language (blocked by a file lock in this environment, see D).

## B. Implementation matrix

| Item | State | Evidence |
|---|---|---|
| Baseline audit document | **DONE** | `docs/audits/2026-11-next-phase-baseline.md` |
| DSH v4 discovery + unknown-name warning | **DONE, TESTED, ON MAIN** (`b55a941`) | `tests/test_dsh.py` (7 tests) |
| UTF-8 stdio at both entry points | **DONE, TESTED, ON MAIN** (`b55a941`) | `tests/test_utf8_stdio.py` (4 tests, byte-level) |
| Short-query fallback + `idx_events_ts` | **DONE, TESTED, ON MAIN** (`b55a941`) | `tests/test_search_short_query.py` (11 tests) |
| `evidence verify --help` lists outcomes/codes | **DONE, TESTED** | `tests/test_evidence_help.py` (3 tests) |
| Release notes v1.2.0 / v1.3.0 / v1.3.1 | **DONE** (two backfilled, marked) | `docs/releases/` |
| README states main ≠ tag | **DONE** (both languages) | `README.md`, `README.zh-CN.md` |
| v1.3.1 candidate artifacts | **BUILT, VERIFIED, NOT PUBLISHED** | `docs/audits/2026-11-v1.3.1-rc-report.md` |
| Plugin README v1.1.0 → v1.2.0, compat table | **BLOCKED** | both files held open by another process; exact edits in §D |
| Phases 2–10 | **NOT STARTED** | — |

## C. Quality gates

| Gate | Result |
|---|---|
| Core suite before | 1338 passed / 6 skipped / 31 subtests / 762 s |
| Core suite after | **1345 passed / 6 skipped / 31 subtests / 735 s** |
| Plugin suite | 54 pass / 0 fail / 0 skip / 63.8 s (build caveat in baseline §3) |
| `twine check` | wheel **PASSED**, sdist **PASSED** |
| Fresh install | `voyager 1.3.1` in a new venv |
| Smoke | demo → search → `evidence verify` **MATCH (0)** |
| Upgrade from pure v1.3.0 index | 11 → 12 indexes (`idx_events_ts` only); 16 tables / 12 events / 4 sessions unchanged |
| Plugin v1.2.0 against candidate | `integration-info` → 1.3.1 / schema 1; `search` tool returned a result |
| macOS | **not run** — no macOS CI leg exists |
| Real provider session | **not touched** |

The six skips are the same environment-declared ones as the baseline; no new
skip was introduced.

## D. Release readiness

**Ready to tag, waiting for a human decision.** The candidate passes every gate
that can be checked here. Steps are in
`docs/audits/2026-11-v1.3.1-rc-report.md`; none were executed.

**Blocked item — plugin README drift.** `README.md` and `README.zh-CN.md` in
`dsh-sessionflow` are held open by another process (append and rename both fail
with `PermissionError`). Three edits are needed:

1. `README.zh-CN.md` header: `v1.1.0` → `v1.2.0`, and the DSH channel list
   `0.1.5-rc.3 / 0.2.0-rc.2 / 0.2.1-alpha.4` → `… / 0.2.1-rc.5 / 0.2.2-alpha.2`.
2. Both compatibility tables: add `| **1.2.0** | v1.3.0 | 1 |` above the
   existing `1.1.0` row.
3. `README.md` "Core v1.3.0 is released…" paragraph: say the current pairing is
   plugin **1.2.0** against core **1.3.0**, and that 1.2.0 adds the two new DSH
   channels while the six tools and schema 1 are unchanged.

## E. Remaining backlog

| Priority | Item | Why |
|---|---|---|
| P0 | Plugin README drift (§D) | user-visible version is wrong in one language |
| P0 | Human decision on tagging v1.3.1 | release is otherwise ready |
| P1 | Compatibility contract as machine-readable metadata (Phase 2) | the drift above happened because the pairing lives in prose |
| P1 | OpenCode adapter (Phase 3) | the only large provider gap |
| P1 | Native resume live verification (Phase 4) | longest-standing `NOT_VERIFIED` |
| P1 | VSIX distribution (Phase 5) | feature exists, users cannot install it |
| P2 | Evidence tool in the plugin (Phase 6) | core has it; agents cannot reach it |
| P2 | Provenance for dsh/grok (Phase 7) | 0.0% classified on DSH-only indexes |
| P2 | macOS CI + DSH canary (Phase 8) | risk is real, evidence is absent |
| Deferred | Auto-clustering, scan transaction batching, LLM summaries | decision records say why |

## F. Suggested roadmap

- **v1.3.1** — correctness (this candidate). Patch.
- **v1.4.0** — provider coverage (OpenCode), VSIX distribution, and either a
  verified native resume or an explicit decision to keep it unverified. Minor.
- **dsh-sessionflow v1.3.0** — evidence tool, compatibility metadata, README
  drift closed. Minor.

These are suggestions; SemVer follows the actual scope.
