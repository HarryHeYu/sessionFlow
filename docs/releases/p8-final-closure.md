# P8 — Governance, test isolation and reliability closure

This round was mostly negative results, and they are the useful part: several
suspected problems were measured and found not to exist, two real defects were
found somewhere other than where they were expected, and one class of flakiness
turned out to be a property of the stress harness rather than of the code.

Nothing was tagged, published to PyPI or npm, or released as a VSIX. No existing
tag or Release was moved.

## 1. Documentation

**Structure.** `docs/` holds the maintained documents; `docs/releases/` holds
per-release notes and per-round reports; `docs/archive/` holds history.
[`docs/INDEX.md`](../INDEX.md) classifies every tracked markdown file by kind,
audience and whether it is **maintained** or **frozen**, and states where a new
document belongs. The root holds only README, README.zh-CN, CHANGELOG,
CONTRIBUTING, CODE_OF_CONDUCT, SECURITY — plus `AGENTS.md`, which is a standard
agent-instruction file rather than a report.

**Archived.** `O5_INVENTORY.md` (self-declared historical snapshot) and
`O6_AUDIT.md` (status "Complete") moved to `docs/archive/audits-2026-10/`. Both
had zero inbound references and no markdown link pointing at them, so the move
rewrote nothing; the durable outcomes are D18/D19 in `DECISIONS.md`, which were
not touched. `docs/archive/README.md` became an index of the three archive
groups instead of describing only one.

**Terminology / taxonomy / ontology — three documents became two.** The first
draft split them into `GLOSSARY.md`, `TAXONOMY.md` and `CONCEPT_MODEL.md`.
Taxonomy and ontology are the two halves of one description of the domain — what
things *are*, and how they *relate* — so they were folded into
[`CONCEPT_MODEL.md`](../CONCEPT_MODEL.md), which now carries the classifications
as sections, then the relationships, then eight invariants with the test that
pins each. [`GLOSSARY.md`](../GLOSSARY.md) stays separate because it is a lookup
reference for a single term, with a different reader. No content was dropped.

**Links.** `tests/test_docs.py` checks every relative link in every markdown file
(and that none uses a backslash), that the index lists every document under
`docs/` and the root, that no phase report has reappeared at the top level, that
the glossary defines every required term and states the four distinctions, that
the lifecycle states, resolver statuses, error codes and capability levels named
in the docs still exist in the code, and that no maintained document contains a
bare `pip install voyager`. All pass.

That last check found a real one: `docs/distribution.md` told a future reader to
run `pip install voyager`, which installs an **unrelated PyPI project**. Fixed.
The same pattern in a frozen 1.0.0 release note was left alone — the check scopes
itself to maintained documents rather than rewriting history.

**Not checked, deliberately:** prose wording. A linter that guesses at natural
language reports false positives, and a check people learn to ignore is worse
than no check.

## 2. CI skill

**Path.** [`.agents/skills/sessionflow-ci/SKILL.md`](../../.agents/skills/sessionflow-ci/SKILL.md),
referenced from [`AGENTS.md`](../../AGENTS.md) and listed in the index.

**Discovery — verified against the reference implementation, not by eye.**
`.agents/skills/<name>/SKILL.md` is the vendor-neutral Agent Skills layout:
an agent looks for a directory named after the skill containing a file named
exactly `SKILL.md`, with `name` and `description` in YAML frontmatter. The
official `skills-ref` validator was installed and run against the directory:

```
skills_ref.validate(Path(".agents/skills/sessionflow-ci"))  ->  []
```

Zero errors. Because that package is not a test dependency, `tests/test_docs.py`
re-implements the structural part (file name, directory name, frontmatter, and
that `AGENTS.md` and the index reference it) so the check runs everywhere, and
calls the real validator when it happens to be installed.

**Content.** It covers the ten required steps and names the commands that
actually run: scoping a change by which files moved, targeted suites, the full
suite, the plugin build and tests, `npm pack --dry-run`, reading GitHub Actions
with `gh run watch`, platform differences, flaky diagnosis, bridge-contract
verification, and safe commit and push. It states the prohibitions — no
skip/xfail to go green, no weakened assertions, no claiming CI before the run
finishes, no force-push — and it does not restate the workflow files: where a
command duplicates one, it says the workflow is the source of truth.

**It cannot bypass CI.** It tells the agent to read the real run result and to
treat a local pass as insufficient. Nothing in it creates a second gate.

## 3. Test isolation

**Before.** The core suite already redirected HOME, USERPROFILE, APPDATA, the
default index path, the working directory and every adapter's storage globals —
but nothing proved it, and the **plugin** suite had no such redirection at all
while spawning the real core as a child. Consequence, measured: **186 files** in
a real `~/.voyager/bundles/`, all `bundle-merged-2sessions-*` scratch from the
plugin's `merge` tests, accumulated since 2026-10-11.

**After.** The plugin suite imports `tests/isolate.ts` first, which points HOME
and USERPROFILE at a throwaway directory before anything spawns. The core suite
gains `tests/test_isolation.py`, which pins each redirection by name and runs a
real `handoff_thread` through its **default** output path to check the
developer's `~/.voyager/bundles/` afterwards.

Writing those tests found a **real gap**: every adapter captures `Path.home()`
at import time, and the redirect list covered the derived names
(`SESSIONS_DIR`, `THREAD_HISTORY_DB`, …) but not `HOME` itself — while
`codex._thread_history_dbs()` globs `HOME / ".codex"` at **runtime**. That path
is reached only when `THREAD_HISTORY_DB` looks unpatched, so the exposure
depended on a sentinel comparison. `HOME` is now redirected too, and the
sentinel resolves the same way (checked).

**APPDATA is deliberately not redirected**, and that is the interesting finding.
On Windows, Python resolves its **user site-packages** from `APPDATA`, and that
is where an editable install of the core lives — so redirecting it makes every
spawned core fail with `ModuleNotFoundError: No module named 'voyager'`.
Measured: **nineteen integration tests then skipped silently**, which reads as
"host gap" rather than as a defect and is worse than the leak it was meant to
prevent. `%APPDATA%` is read by exactly one place in the product
(`kiro.py`, session discovery), never written, and these tests never scan, so
the residual exposure is small and known. A test records the decision so it does
not read as an oversight.

**User files left untouched.** No existing file was deleted, moved or
overwritten. The suspected test artefacts in `~/.voyager/bundles/` are listed
read-only in §9 below; nothing was removed.

**Remaining risk.** A test that spawns a child inherits the redirection, but a
test that resolves a path from `Path.home()` *at import time* would not — the
adapter globals were exactly that, and the guard now covers them.

## 4. Flaky tests

Two root causes, both reproduced under load, both fixed at the cause.

**Root cause A — a discovery budget that depended on machine speed.** 8 of 80
runs failed, always on the first iteration of each worker, always
`VOYAGER_NOT_FOUND` after 27.6 s, for a core that was installed and working.
`resolveVoyager` proves each candidate by spawning it and importing the package,
and the probe shared a 15 s ceiling; with twenty-four CPU burners and ten
concurrent suites, starting Python and importing `voyager.cli` exceeds it. The
probe now has a 60 s budget of its own — it runs **once per process** and the
result is cached, so patience costs one startup, while failing it tells the user
their core is missing when it is on `PATH`.

**Root cause B — a shared output path, and a torn read.** 1 of 80 runs failed
`merge output is stable for the same input` on a determinism assertion, and it
was not a determinism bug. The bundle name was the member list plus
`int(time.time())`, so parallel suites computed the **same** path, and
`Path.write_text` truncates first — a read-back landing in that window got `''`.

**Product fixes.** `default_bundle_name` now adds a random suffix, so two
invocations cannot choose the same path (the readable `bundle-merged-*.md`
prefix is unchanged, and a single session keeps its deterministic name because
re-exporting one session is meant to land on the same file). `_write_atomic`
writes to a sibling temp file and `os.replace`s it, so a reader sees either the
previous complete file or the new one; the staging name carries the pid and a
per-process sequence number, so threads of one process cannot collide; it is
removed on every path including failure; and the replace is retried up to five
times on Windows, where another process holding the destination briefly makes
`os.replace` raise `PermissionError` — observed on a CI runner.

**Test fixes.** The plugin's merge tests pass an explicit `-o` under the suite's
temp dir; the suite's HOME is redirected; integration calls use the interpreter
the seeding step already proved works instead of re-scanning `PATH` per call;
concurrent resolutions share one in-flight probe; and two stub-based tests lost
their arbitrary 3 s and 5 s per-call deadlines, which measured machine load
rather than the argument forwarding they exist to check.

**Repeated results.** Four 80-run campaigns under identical conditions
(24 CPU burners + 10 concurrent suites on a 32-core machine, three test files
per run):

| Campaign | Runs | Failing runs | Failing tests |
|---|---|---|---|
| before | 80 | **8** | `recent returns the index overview` ×4, `recent on a repo with no history` ×4 |
| after the two root-cause fixes | 80 | **9** | `each tool forwards the documented core op` ×8, `search forwards its query` ×5 |
| after removing the stub deadlines | 80 | **5** | `BAD_JSON` ×2, `the deadline kills the child` ×1, `each tool forwards` ×1, `merge … DEFAULT output path` ×1 |
| after removing the remaining arbitrary deadlines | **80** | **0** | — |

**0 failures in 80 runs** under the exact conditions that produced 8 before, with
**0 skips in every run** — so nothing was passing by being skipped, and the
isolation work did not silently disable the integration tests.

Every intermediate failure was diagnosed before it was fixed: the original two
by root cause, and the later ones as a test-spawned process exceeding a deadline
that the test itself had set and that was never its subject. The three campaigns
in between are reported rather than collapsed into the final number, because a
result that only appears after four attempts is worth being able to audit.

## 5. Production bundle safety

**Collision risk: real, and not only in tests.** The default multi-session name
was time-keyed at one-second granularity, so `merge`, `switch`, or any two
processes doing the same thing at once shared a window in which the second
silently replaced the first. Atomic replacement does **not** address this —
uniqueness and atomicity are independent properties, and both were needed.

**Atomic write behaviour.** Write to a sibling temp file, then `os.replace`,
which is atomic on POSIX and Windows alike. A failure part-way through leaves
the destination untouched rather than truncated. Temp files are removed on every
path. A bounded retry absorbs the Windows transient where another process holds
the destination open.

**The `--output` contract is unchanged.** An explicit path still overwrites it,
which is what the caller asked for; only the *default* name became unique.

**Regression tests** (`tests/test_bundle_write.py`, 10 tests, each
mutation-checked): the swap happens while the old file is still intact; the name
differs between two calls in the same second; the destination survives a failed
write; concurrent writers of one target do not share a staging file; two threads
writing one target always leave one complete payload; the name keeps its
documented prefix and stays filesystem-safe; a single-session name stays
deterministic; and `handoff_thread` actually goes through the atomic path. That
last one matters — with only the helper tested, reverting the call site leaves
every test green.

## 6. Quality gates

| Gate | Result |
|---|---|
| Core full pytest, local | **1154 passed, 4 skipped, 31 subtests** (before the five skill checks were added) |
| Core full pytest, CI | **1158 passed, 5 skipped, 31 subtests** on `71cd22a` |
| the 5 skips | 4 unchanged data/platform gates (scratch root unset, no CJK sessions, Windows `chmod`) + the `skills-ref` validator test, which skips where the package is absent |
| New tests this round | `test_isolation.py` 6, `test_bundle_write.py` 10, `test_docs.py` +5 |
| Plugin `npm test` | **46/46, 0 skipped** |
| Plugin `npm pack --dry-run` | 24 files, 27.0 kB |
| Plugin build | `tsc` clean |
| Bridge contract | `schema_version` still 1, same keys, six `sessionflow_*` tools unchanged; `api.py` untouched |
| Linux CI | 10/10 core jobs include all four Ubuntu Pythons; plugin cold-start green |
| Windows CI | 4/4 Windows jobs green on `71cd22a` |

## 7. Git and CI

**Core commits:** `4c5f878` (unique names + atomic write), `a992cd9` (isolation
guards), `8ce9489` (taxonomy folded in), `4d0b9e9` (isolation guards corrected
for CI, Windows replace retry), `71cd22a` (this report).

**Plugin commits:** `2c2d134` (probe budget, hermetic merge tests), `beae5d0`
(in-flight probe sharing, suite HOME isolation), `6d2adef` (the suite's own
deadlines no longer measure machine load).

Both worktrees are clean and both `origin/main` refs equal their HEADs; no tag
was moved and nothing was published to PyPI, npm or the VS Code marketplace.

**CI caught what local runs could not.** The first core push failed on **all four
Windows jobs** while every Ubuntu job passed: pytest's temp root lives *under*
the home on a GitHub runner, so my "not under the home" assertion failed for the
wrong reason — locally it passed only because I had been passing `--basetemp` on
another drive. The predicate is now "not inside a real provider data directory",
and the condition was reproduced locally by pointing `--basetemp` under the home.
The same run also exposed the Windows `os.replace` `PermissionError`.

| Run | Commit | Result |
|---|---|---|
| [38615860180](https://github.com/HarryHeYu/sessionFlow/actions/runs/38615860180) | core `71cd22a` | **10/10 jobs success** — py3.10–3.13 on Ubuntu *and* Windows, plus the core-only jobs; **1158 passed, 5 skipped** |
| [38609644136](https://github.com/HarryHeYu/sessionFlow/actions/runs/38609644136) | core `4d0b9e9` | **10/10 jobs success** |
| [38604197003](https://github.com/HarryHeYu/sessionFlow/actions/runs/38604197003) | core `8ce9489` | **failure** — 4 of 4 Windows jobs, 0 Ubuntu jobs (superseded) |
| [38615671191](https://github.com/HarryHeYu/sessionFlow/actions/runs/38615671191) | plugin `6d2adef` | **3/3 jobs success** (cold-start on Linux) |
| [38609700931](https://github.com/HarryHeYu/sessionFlow/actions/runs/38609700931) | plugin `beae5d0` | **success** |

The failing run is kept in the table rather than only the successes, because it
is the one that found two defects — and because "local is not remote" is exactly
what it demonstrated. The fifth skip on CI is the reference `skills-ref`
validator test skipping itself where the package is not installed; the other four
are the unchanged data and platform gates.

## 8. The residual condition, and how it was closed

After the two root causes were fixed, three 80-run campaigns still showed a
handful of failures — a different class each time, all with the same shape: a
**test-spawned process not answering within a deadline**. Three instances were
diagnosed from their logs, none of them a product defect:

| Failure | Log | Why |
|---|---|---|
| `non-JSON output becomes a BAD_JSON error` | `exceeded 20000ms` | a Node stub under 34× oversubscription; the subject is error mapping |
| `the deadline kills the child` | `the child never ran, so this test proved nothing` | the 6 s deadline fired before the stub had started |
| `merge using the DEFAULT output path` | `exceeded 20000ms` on `py -m voyager.cli merge` | Python cold start plus the merge, in a test whose subject is the output path |

None is fixable by choosing a bigger number: a fixed deadline cannot be both
"long enough that the process definitely started" and "short enough to be a
deadline" when a cold start varies by an order of magnitude with load. So the
deadlines that were never the subject were removed:

* `stub()` and the integration call path use `TEST_CALL_TIMEOUT_MS` (120 s).
  These tests assert what was returned, not how fast; the product's own deadline
  has dedicated tests, which pass their own `timeoutMs` and win;
* the marker test derives its deadline from a **measured cold start** (20×, floor
  6 s) rather than a magic constant.

Result: **0 failures in 80 runs** (§4).

What remains true and is not claimed otherwise: a machine running far more
concurrent processes than it has cores can still make a spawn-and-wait test
exceed any fixed deadline. GitHub's runners are 2-4 cores and `node --test` runs
the three files with a concurrency of `cores - 1`, which is not that regime —
and the CI matrix, not a local run, is the check that matters (§7).

## 9. Read-only inventory: suspected test artefacts in `~/.voyager/bundles/`

**Nothing was deleted, moved or overwritten.** For the maintainer to decide.

| | |
|---|---|
| Files present | **189** |
| Matching `bundle-merged-2sessions-claude-codex-*.md` | **186** |
| Other | 3 (`bundle-codex-br1.md` and two others, older) |

**Basis for calling them test artefacts, not user bundles:** the name pattern
matches the plugin's `merge` tests exactly (two members, providers `claude` and
`codex`, which is the plugin's fixture pair); the files span 2026-10-11 to
2026-10-14, which is the period the plugin suite ran with an unredirected HOME;
each is ~718 bytes, the size of that fixture's compiled bundle; and no `merge`
invocation by a human is recorded in the repository's own history. The three
others are older and do not match, so they are **not** attributed to the suite.

Deleting them is a one-line command, and deliberately not run here.

## 10. Issues

`#12` (performance debt) and `#16` (two capability models) remain open as before,
and `#18` (multi-session materialisation) is unchanged. No issue was reopened and
none was duplicated. **No new issue was filed**: the flaky test this round was
root-caused and fixed with regression tests, so it does not need one.

## Final status

**COMPLETE.** The two root causes are fixed with regression tests that were
mutation-checked, the isolation gap the guards found is closed, the bundle write
is both unique and atomic, and 80 repeated runs under the load that produced the
original failures now produce zero failures and zero skips. Core and plugin CI
are green, and the one failure along the way is reported with what it found.

One item is left deliberately for the maintainer rather than done automatically:
the read-only inventory in §9.
