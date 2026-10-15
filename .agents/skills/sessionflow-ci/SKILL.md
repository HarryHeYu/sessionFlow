---
name: sessionflow-ci
description: >
  Run and read the quality gates for the sessionFlow core repository and the
  dsh-sessionflow plugin: scope a change, run targeted tests, run the full
  pytest suite and the plugin build/tests, read GitHub Actions results, diagnose
  a flaky test, verify the DSH bridge contract, and commit and push safely.
  Use when changing code in this repository, before claiming a change works.
---

# sessionFlow CI

How to take a change in this repository from "I edited something" to "CI is
green and I can say so". The commands below are the ones this repository
actually runs; where a command duplicates a workflow file, the workflow is the
source of truth and this page tells you how to run the same thing locally.

This is a procedure, not a second CI configuration. Do not add gates here that
`.github/workflows/` does not run, and do not treat a local pass as a remote pass.

## 0. Rules that are not negotiable

- **Never claim CI is green before the run has finished.** A queued run is not a
  passing run. Watch it to completion and read the conclusion.
- **A local pass is not a remote pass.** Different Python versions, different
  platforms and different optional-dependency sets are installed there.
- **Do not skip or xfail a test to make a suite green.** If a test is wrong, fix
  the test and say why; if the code is wrong, fix the code.
- **Do not weaken an assertion to make it pass.** A test that passes because it
  checks less is worse than a failing one.
- **Do not blame the sandbox without evidence.** If a command fails, capture the
  error and reproduce it before attributing it to the environment.
- **Never force-push a public branch.** `main` on either repository is public.
- **Never move a tag, publish to PyPI/npm, or create a GitHub Release** unless
  the task explicitly says to.

## 1. Know what you are working with

```bash
cd /path/to/voyager            # the core repo (package `voyager`)
git rev-parse --short HEAD
git rev-parse --short origin/main    # compare: are you ahead, behind, or diverged?
git status --short                   # must be empty before you claim "clean"
git tag -l | tr '\n' ' '             # which releases exist
```

The plugin is a **separate repository** (`dsh-sessionflow`). Check both; they
version independently and only the bridge schema binds them.

Two repository-specific facts that look like failures and are not:

- `git commit` in this repository often exits non-zero (a post-commit hook shells
  out to a Windows binary; a repack step reports a pre-existing missing object).
  **The commit usually landed.** Check `git log -1 --oneline`, do not read the
  exit code, and do not retry — a retry creates a duplicate commit.
- `git push` can report an error and still have succeeded. Verify with
  `git fetch -q origin && git rev-parse --short origin/main`.

## 2. Decide what the change can break

Do not run everything for a typo, and do not run one file for a schema change.
Classify first:

| Touched | Run at minimum |
|---|---|
| `voyager/store.py`, schema, migrations | `tests/test_store.py tests/test_retention.py tests/test_scan_consistency.py tests/test_scan_crash.py tests/test_concurrent_open.py` |
| `voyager/scanner.py`, `voyager/adapters/**` | `tests/test_scan_streaming.py` **first** — it is the suite for the scan pipeline — then `tests/test_scan_characterization.py tests/test_scan_consistency.py tests/test_scan_crash.py tests/test_scan_error_isolation.py tests/test_scan_source_change.py tests/test_scan_scope.py` |
| `voyager/adapters/**` (specifically) | `tests/test_adapters.py` + the per-provider suite (`test_codex_app_store.py`, `test_zcode.py`, `test_grok_native.py`, …) |
| `voyager/continuity.py`, `ranker`, `budget`, `provenance` | `tests/test_continuity.py tests/test_auto.py tests/test_budget.py tests/test_provenance.py` |
| `voyager/api.py`, `mcp_server.py` | `tests/test_api.py tests/test_mcp.py tests/test_capability_truth.py` |
| `voyager/capability_matrix.py`, `integrations/capabilities.py` | `tests/test_capability_truth.py tests/test_capability_consistency.py tests/test_doctor.py` |
| `voyager/cli.py` | the scan suites plus `tests/test_workflows.py` |
| `docs/**`, `*.md` | `tests/test_docs.py` |
| `vscode-extension/**` | `tests/test_vscode_extension.py` |
| `dsh-sessionflow/**` | the plugin suite (step 5) and the bridge check (step 8) |

`docs/SCAN_CONSISTENCY.md` states the invariants the scan suites protect. If a
change touches the write path, read it first and say in the commit which
invariant you checked.

## 3. Run targeted tests first

```bash
python -m pytest tests/test_scan_consistency.py -q --tb=short -rf
```

Find the interpreter that has the package and pytest. The core install must work
with **zero optional dependencies**, so `pip install -e .` plus pytest is a valid
environment; some tests skip themselves when an extra is missing, and that is
expected rather than a failure.

When adding a test for a bug, **confirm it fails first** — revert the fix, watch
it fail for the stated reason, restore. A test that never failed has not been
shown to test anything.

If a temporary directory is needed, put it on a scratch path outside the
repository, and **do not let it be named anything containing `.voyager`** — the
product's own directory guard treats that as a live install.

## 4. Run the core suite

The exact command CI uses:

```bash
python -m pytest tests/ -q --tb=short -rf
```

The suite is roughly **1,170 tests** (1,177 collected as of P9); it takes
several minutes on Windows and under a minute on Ubuntu. Run it in the
background and read the tail when it finishes rather than polling.

There is no `.agents`-style second interpreter to find: `python` on `PATH` in a
Git Bash shell here already resolves to an interpreter with pytest installed
(9.x), and the full isolated venv used for pinned runs is
`C:\Users\He_Yu_Hao\.workbuddy-ai\binaries\python\envs\voyager-dev\Scripts\python.exe`.
Both work; do not conclude "pytest is missing" from the base interpreter alone
without checking which one you got.

`PYTHONPATH=` (empty) matters when measuring or stress-running: WorkBuddy's
`sitecustomize` shim can turn bulk deletions into `SystemExit`, which fails
unrelated tests. Use it for full-suite and benchmark runs.

`pyproject.toml` pins `testpaths = ["tests"]`, so a scratch file in the
repository root is not collected. Do not rely on that — keep scratch out of the
tree.

## 5. Run the plugin

In `dsh-sessionflow`:

```bash
npm run build      # tsc
npm test           # builds, then `node --test` over tests/**/*.test.ts
npm pack --dry-run # confirm the tarball contains what it should
```

Two things to know:

- The suite is **isolated by itself**: `tests/isolate.ts` redirects
  `HOME`/`USERPROFILE` to a throwaway directory before anything spawns a child,
  and the `merge` tests pass an explicit `-o`. Verified: `npm test` with no other
  environment set leaves `~/.voyager/bundles/` at exactly the size it started
  (189 files) and reports 46/46, 0 skipped. Setting `VOYAGER_NO_SYNC=1` is still
  the belt-and-braces option — it stops a real index scan from happening at all,
  which also makes the run faster — but it is no longer what stands between the
  suite and the developer's data. Do not "fix" an isolation regression by
  setting the variable here; fix `tests/isolate.ts`.
- If you change `src/`, rebuild. A suite run against a stale `lib/` is testing
  code that is not in the commit.
- Use `SESSIONFLOW_TEST_TMPDIR` to put scratch on a fast disk, since the suite
  otherwise defaults to the OS temp directory.

## 6. Read the CI result

```bash
gh run list --branch main --limit 5
gh run watch <run-id> --exit-status --interval 20
gh run view <run-id> --json conclusion,headSha,jobs \
  --jq '".\(.conclusion) on \(.headSha[0:7])  jobs: \([.jobs[]|.conclusion]|unique|join(",")) (\(.jobs|length))"'
gh run view <run-id> --log > /tmp/ci.log      # then grep the counts
```

`test.yml` runs a matrix of Python 3.10–3.13 on Ubuntu plus 3.10–3.13 on Windows,
and a `core-only` job that installs with no extras. **10 jobs total.** A run is
green when every job is `success`; "most jobs passed" is not a result.

Pull the pass count out of the log and compare it to the local count. If remote
is lower by exactly the number of tests you added, something is being skipped
silently — investigate rather than accepting it.

## 7. Windows and Linux are genuinely different here

The matrix is not decoration: the adapters read Windows paths (`%APPDATA%`, drive
letters, backslashes), so both ends of the range run on Windows.

- A path test that passes locally on Windows can fail on Linux and vice versa.
  Run the suite on both when you touch path handling, or say that you only
  checked one.
- `chmod`-based tests skip on Windows by design.
- **The expected skip count is not one number.** It depends on the job, because
  extras-dependent tests skip themselves and the platform gates differ:

  | job | skips |
  |---|---|
  | Windows, extras installed | ~5 |
  | Ubuntu, extras installed | ~22 |
  | `core install (no extras)` | ~46 |

  So compare a job to **the same job** on the previous run, not to your local
  count. A jump of 17 skips on Ubuntu is the optional dependencies being absent,
  not a new finding; one extra skip in the *same* job you were comparing is.

## 8. Verify the DSH bridge contract

Only needed when `voyager/api.py` changes, but cheap enough to run anyway:

```bash
python -m voyager.cli integration-info --json
```

Check that `schema_version` is still `1` and that the keys the plugin reads are
unchanged. The plugin's six tools are `sessionflow_*`; if the contract is
unchanged, the plugin needs **no** code change — and you should say so rather
than "syncing" it for symmetry.

The plugin must be able to reach core end to end: run a real DSH turn, or the
plugin's own integration tests. A configured `voyagerBin` that fails its probe
raises rather than silently falling back to another binary on `PATH`, so a
misconfigured path shows up as an error, not as wrong answers.

## 9. Diagnose a flaky test

A test that passes on re-run has not been fixed.

1. **Reproduce it.** Run the suite (or the suspect file) repeatedly, capturing
   each run's output to a file. One failure in N runs is the evidence; a single
   green re-run is not.
2. **Name the failing test**, not just the count. `node --test` prints
   `not ok <n> - <name>`; pytest prints `FAILED <nodeid>`.
3. **Classify the cause** before changing anything:
   - wall-clock or deadline assertion → the test depends on machine speed
   - fixed `sleep` used as synchronisation → the test depends on scheduling
   - shared global mutated by another test → ordering dependence
   - subprocess or temp file not cleaned up → resource leak across tests
   - port or path collision when tests run concurrently → isolation failure
4. **Fix the cause, not the symptom.** Replace a sleep with a real synchronisation
   condition; replace a wall-clock deadline with an injected clock or a generous
   but *bounded* wait; capture globals at module load instead of reading them
   after another test may have changed them.
5. **Do not raise a timeout or add a sleep to make it pass**, and do not
   quarantine the test. If it cannot be fixed now, open an issue with the
   reproduction, the failure log, the environment and an acceptance criterion.
6. **Re-run enough times to mean something** — a fix confirmed by one run is not
   confirmed.

## 10. Commit and push

```bash
git add <explicit paths>          # never `git add -A` on a dirty tree you did not inspect
git diff --cached --stat
git commit -m "<type>(<scope>): <what changed and why>"
git log -1 --oneline              # confirm it landed despite the exit code
```

Then push and verify:

```bash
GCM_INTERACTIVE=never GIT_TERMINAL_PROMPT=0 \
  git -c credential.helper= -c credential.helper=manager push origin main
git fetch -q origin
git rev-parse --short origin/main   # must equal HEAD
```

Then watch CI (step 6) before reporting the work as done.

**Never** use `git stash` in this repository: an interrupted stash has been
observed to lose recently created objects from `.git/objects`, leaving a
repository that cannot read `HEAD`. To test old code, use
`git show HEAD:<path>` into a separate file instead.

## Report format

When you finish, state: what changed, the targeted suites you ran, the local
full-suite count, the plugin result, the CI run URL and its job conclusions, and
anything you did **not** verify. An unverified item reported as unverified is
useful; the same item reported as verified is a defect.
