# Phase 2 report — 2026-11-09 (dual-repo consistency)

> Scope: Phase 2 only — plugin docs, compatibility contract, cross-repo
> verification, isolation hardening, release readiness, issue triage, OpenCode
> design input. Phase 3–10 remain **not started**. Numbers marked *(pending)* are
> filled from the run that this commit belongs to.

## A. Heads and changes

| | Core | Plugin |
|---|---|---|
| HEAD before this round | `25bae9e` | `6fbed16` |
| HEAD after this round | `c027adf` + report commit | `6940d9b` |
| Commits added | 3 (`dabb52c`, `c027adf`, report) | 1 (`6940d9b`) |

**Core** — `scripts/check_consistency.py` + 8 tests; four isolation regressions;
CI step with `fetch-tags: true`; OpenCode design document; this report.

**Plugin** — both READMEs corrected (v1.1.0 → v1.2.0 in Chinese, compatibility
tables, the "tested against v1.1.0" note), plus an explicit statement that core
v1.3.1 is a release candidate rather than a released version.

## B. What was actually wrong, and what fixed it

**The plugin README lock was not a lock.** Diagnosis in order:

| Check | Result |
|---|---|
| Directory writable | yes |
| ACL of the two files | identical to a writable neighbour (`FullControl`) |
| Attributes | `Archive` only, no `ReadOnly` |
| `File.Open(..., Write, None)` | `UnauthorizedAccessException` |
| `File.Replace` (atomic rename over target) | **succeeds** |

The two files are **pnpm hardlinks** (14 links each, pointing into
`scratch/*/node_modules`), and at least one linked copy is held open by a
resident process, so open-for-write is refused while rename over the directory
entry is allowed. Written with `File.Replace`, backups kept outside the repo;
the hardlink is now broken, which is the correct state for a tracked source file.

**Documentation drift** (all five items from the baseline are now closed):
Chinese header, both compatibility tables, the English "no plugin upgrade is
needed" note, and the released-vs-RC distinction. Core-side release notes for
v1.2.0/v1.3.0/v1.3.1 and the "main is ahead of the tag" README note were closed
in Phase 1.

## C. One place to check release metadata

`scripts/check_consistency.py` compares, and **fails CI** on disagreement:

- `pyproject.toml` version == newest `CHANGELOG` heading
- `docs/releases/v<version>.md` exists (the file `release.yml` reads)
- README (en + zh) names the newest **git tag**, and both languages agree
- every install pin `@vX.Y.Z` names a tag that exists
- core `integration-info` `schema_version` == plugin `REQUIRED_SCHEMA_VERSION`

Deliberate choices:

- **Unknown is not a pass.** Without git tags the tag comparison prints
  `NEEDS_ONLINE`; it never prints OK.
- **The version being built may be ahead of the tag.** README must name the
  newest *released* tag, so a prepared-but-unreleased 1.3.1 is legal while README
  still says v1.3.0.
- Historical documents are exempt: `docs/archive/` and older release notes are
  records and may name old versions.

Current output on this tree (8 checks, all OK):

```
OK   pyproject version                              1.3.1
OK   CHANGELOG newest heading                       1.3.1
OK   release notes for 1.3.1                        docs\releases\v1.3.1.md
OK   README en/zh agree                             1.3.0
OK   README names the newest tag                    v1.3.0
OK   install pins in README.md are released         1.3.0
OK   install pins in README.zh-CN.md are released   1.3.0
OK   bridge schema agreement                        1
```

## D. Cross-repository contract

**No new contract test was written, because the existing suite already covers the
matrix.** 54 plugin tests already assert: missing core, configured-but-broken
core (never silently replaced), PATH fallback, non-JSON output, timeout, non-zero
exit, schema gate, concurrent probe sharing, CJK round-trip, repo filter with
spaces, unknown ids, and the default-output-path isolation.

What was missing was **evidence against the candidate**, so the whole suite was
run with `VOYAGER_PYTHON` pointed at the installed **1.3.1 wheel**:

| Pairing | Result |
|---|---|
| Plugin v1.2.0 + core v1.3.0 (installed) | 54 pass / 0 fail / 0 skip |
| Plugin v1.2.0 + **core v1.3.1 RC** | **54 pass / 0 fail / 0 skip** (55 s) |

Boundaries, stated plainly: this is a **real-core integration test**, not a real
DSH host run and not a real-LLM E2E. The two later categories remain out of CI.

## E. Isolation hardening

The guard already covered `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`,
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` and judged each on the data
directories it derives. The gap was not coverage but **regression evidence for
the exact mistake made on 2026-11-09**:

```sh
export HOME=<scratch> USERPROFILE=$HOME   # $HOME here is the OLD value
```

`USERPROFILE` therefore stayed at the real profile and `voyager demo` wrote into
the real `~/.voyager`. Four tests now pin the behaviour:

| Test | Asserts |
|---|---|
| redirected HOME + real USERPROFILE | refused, names `USERPROFILE` |
| real `XDG_DATA_HOME` (a data dir, not the profile root) | refused |
| Windows case variant of USERPROFILE | refused |
| fully redirected environment | **passes** — the guard must not refuse correct setup |

## F. Tests and CI

| Gate | Result |
|---|---|
| Core suite | **1356 passed / 6 skipped / 31 subtests / 773 s** |
| Plugin suite | 54 pass (against 1.3.0 and against 1.3.1 RC) |
| Consistency checker | 8 tests, 8 checks OK on this tree |
| Isolation guard | 21 passed / 2 skipped (same two environment skips as baseline) |
| Docs | 20 passed |
| Core CI | **10/10 success** (run 42595938476, `66194e7`) — the consistency step passed in CI |
| Plugin CI | **success** (run 42595007899, `6940d9b`) — `cold-start.yml` runs on push |

### The checker failed its own first CI run — and that was useful

Run 42594999041 failed all eight matrix jobs on the new step, with
`v1.3.0 not in []`: `actions/checkout` with `fetch-tags: true` produced no tags,
and the checker read an empty list as "no release exists". Two fixes: fetch with
`fetch-depth: 0`, and treat an empty list as **unknown** (`NEEDS_ONLINE`) with a
regression test. The next run passed 10/10 and printed the eight checks,
including `README names the newest tag v1.3.0` — which is the proof that the tag
list is now real rather than assumed.

## G. Release readiness (v1.3.1)

Artifacts rebuilt from **this** HEAD (`c027adf`), because the sdist includes
`docs/` and `scripts/`:

| Artifact | Size | SHA-256 |
|---|---|---|
| `voyager-1.3.1-py3-none-any.whl` | 373,521 B | `9d3f2d4beb82de1db60e89011b08f936f6aa5149cda616fd0ef63f7994e6495b` |
| `voyager-1.3.1.tar.gz` | 661,148 B | `a71df619261b8333342e2ad98c13e445388b5556cbbad8e0015d7b8e9ff8a52c` |

`twine check` PASSED on both. Fresh venv install → `voyager 1.3.1`; `demo`,
`search` (exit 0) and `evidence verify` (MATCH, exit 0) all work. Upgrade from a
pure v1.3.0 index still adds only `idx_events_ts` (verified in Phase 1, unchanged
by this round).

**Rollback**: `v1.3.0` reads a v1.3.1 index unchanged and ignores the extra
index. No migration ran, so rolling back is a tag change, not a data operation.

**Not done**: no tag, no GitHub Release, no PyPI. Steps are in
`docs/audits/2026-11-v1.3.1-rc-report.md`.

## H. Issues

| Issue | State after this round | Action |
|---|---|---|
| core #12 | open, performance debt | untouched; C1/C2 must not be re-implemented without statement counts |
| plugin #1 | open | comment: fixed on `b55a941`, tests named, **first release v1.3.1 (pending)** — not closed before the release exists |
| plugin #2 | open | comment: split into three items (naming → MAINTENANCE #14, provenance → #13, OpenCode → design doc). No duplicate issues created |

No issue was closed, because the fix is not in any tag yet. Closing on "main"
would repeat exactly the confusion this phase exists to remove.

## I. OpenCode design input

`docs/audits/2026-11-opencode-adapter-design.md`. Verified against upstream
**v1.19.5** (2026-10-28): SQLite at `~/.local/share/opencode/opencode.db` with
channel variants and `OPENCODE_DB` override; tables `session_v2`,
`session_message`, `session_part` (carrying `call_id`), `project`.

Recommendation: read SQLite read-only, because `call_id` is what joins a tool
result to its command. Four options compared; CLI/API kept as fallbacks. The
document separates verified facts from items that must be confirmed on a real
install, and writes acceptance criteria a later round can test.

## J. Remaining backlog

| Priority | Item | State |
|---|---|---|
| P0 | Human decision on tagging v1.3.1 | ready, waiting |
| P1 | OpenCode adapter | design done, code not started |
| P1 | Native resume live verification | still `NOT_VERIFIED` |
| P1 | VSIX distribution | still 0.1.0, no artifact |
| P2 | Evidence tool in the plugin | not started |
| P2 | Provenance for dsh/grok | not started |
| P2 | macOS CI leg, DSH canary | not started |
| Deferred | auto-clustering, scan transaction batching, LLM summaries | decision records stand |
