# P9 STREAMING SCAN REPORT

## CI Skill dogfood (Phase 9)

`.agents/skills/sessionflow-ci/SKILL.md` was followed for the whole round, and it
was mostly right: it predicted both the `git commit` non-zero exit (which
happened, and checking `git log -1` instead of retrying was correct) and the
"push can fail and still have succeeded" case (which also happened, and the
fetch-and-compare check resolved it). Its "watch CI to completion, never claim
green while queued" rule mattered — the first run on `08517ed` **failed** on four
jobs, and it was CI that found both a Windows/Ubuntu split my local runs could
not see and the D9 defect.

Four concrete errors were found and fixed, each against evidence rather than
preference:

1. **The suite size was stale** ("roughly 1,100 tests"). It is 1,177 collected.
2. **The skip guidance was wrong in a way that would cause a false alarm.** The
   skill said "four skips are expected", which is the *Windows-with-extras*
   number. The CI log shows the real spread: Windows ~5, Ubuntu **~22**,
   `core install (no extras)` **~46** skips, because extras-dependent tests skip
   themselves. The rule is now "compare a job to the same job previously";
   an apparent 17-skip jump on Ubuntu is the optional dependencies being absent,
   not a finding.
3. **The scope table was missing the primary scan suite.** The
   `voyager/scanner.py` row did not list `tests/test_scan_streaming.py`, which
   is now the suite the scan pipeline is protected by.
4. **The `VOYAGER_NO_SYNC` warning was outdated.** The skill said the plugin
   suite must run with it set "or a `merge` path will perform a real scan of the
   user's session directories". Since P8's isolation work that is no longer true:
   verified by running `npm test` with no other environment set — 46/46, 0
   skipped, and `~/.voyager/bundles/` unchanged at 189 files. The skill now says
   the suite is isolated by `tests/isolate.ts`, and that an isolation regression
   must be fixed there rather than papered over with the variable.

The skill still validates clean against the official `skills_ref` validator
(zero errors), and `tests/test_docs.py` still guards its structure.

## Architecture

**Previous materialisation path.** `ad.scan(source_changed)` returned a `list`
of every bundle, and `run_scan` only began calling `replace_session()` once the
whole provider had been parsed. On the 100k set that meant every event of every
session was resident before the first byte was durable, and — because the whole
round is one `scan()` call — an interrupted scan lost *all* of its progress.

**New streaming path.** `scan(source_changed, emit=None)`: an adapter that takes
`emit` hands its bundles over one at a time. `Adapter.scan_streams()` answers
from the signature whether an adapter takes it, so the scanner asks before it
calls. Both styles funnel through one local `_consume()` in `scanner.py`, so a
streaming adapter and a list-returning adapter cannot drift apart.

**Compatibility.** Additive. ZCode and Cursor still use the list path and are
proved to still work (`test_an_adapter_without_emit_still_uses_the_list_path`
drives the real ZCode adapter, two sessions, and asserts `emit` is not in its
signature). `api.py` is untouched; the bridge schema version is still **1** and
the six `sessionflow_*` tools are unchanged, so **no plugin change is needed**.
The only code touched: `voyager/adapters/base.py`, `voyager/adapters/codex.py`
(rollouts and its app store), `voyager/scanner.py`.

## Correctness

| | |
|---|---|
| **D5** | **Holds, and is now provable at scale.** Stamping stays at the end of the round: the scanner cannot know whether a later bundle will still claim a path. In the 100k kill test, 20,000 sessions were on disk and **0 source rows were stamped** — then a plain re-scan converged. |
| **D6** | **Unchanged.** A failed write is isolated, and the failed path is withheld from `stamp_sources()` — including a shared path, where one failure withholds the stamp for the whole artifact. |
| **D7** | **Unchanged and slightly safer.** The pre-read fingerprint snapshot is still taken before any reading, and streaming moves the first write *earlier*, shortening the window rather than lengthening it. |
| **D8** | **Unchanged.** No new lock; the adapter calls back into the store from inside its own read of the source, and §14's concurrency findings still hold. |
| **D9 (new)** | Two processes creating the same fresh index could make one fail with `attempt to write a readonly database`. Fixed with a bounded setup retry; see the section below. |
| **Crash recovery** | The kill test above. Partial writes are durable and the artifact is not falsely marked complete. |
| **FTS consistency** | 1,000,000 `event_fts` rows = 1,000,000 events, asserted in every scan test and in the acceptance run. |
| **Duplicate prevention** | Three repeated scans give 0 duplicate event groups; two concurrent child processes converge to 3 sessions, 0 duplicates. |

All ten Phase-4 scenarios have a test: interrupted scan, session 2/3 write
failure, appended-during-read, source disappears and returns, one artifact with
many sessions, repeated scans, FTS agreement, `SOURCE_MISSING` not mislabelled,
concurrent convergence, and `--force`.

## Performance

Strict A/B, back-to-back, same datasets and same machine, "before" = the package
archived from `b65dbc9`:

| scale | before RSS | after RSS | before scan | after scan | before first write | after first write |
|---|---|---|---|---|---|---|
| 1,020 sessions | 104.6 MB | 76.0 MB (**−27%**) | 7.46 s | 6.00 s (**−20%**) | 2.43 s | 0.71 s (**3.4×**) |
| 2,020 sessions | 159.0 MB | 102.2 MB (**−36%**) | 18.67 s | 15.27 s (**−18%**) | 5.61 s | 0.86 s (**6.5×**) |
| 10,020 sessions | 445.0 MB | 153.7 MB (**−65%**) | 120.29 s | 100.70 s (**−16%**) | 52.84 s | 1.24 s (**43×**) |

The saving grows with scale, which is the signature of fixing the actual
bottleneck rather than a constant cost. No-op incremental scans are unchanged
(0.14 s / 0.31 s at 1k/2k), and forced scans are slightly *faster*.

## 100k / 1M

**Status: COMPLETE** (not ENVIRONMENT_LIMITED, not extrapolated).

* Peak RSS **484.9 MB** — against the ~3.8 GB projection, about an 8× reduction
  and ~4.8 KB per session instead of ~38 KB.
* First durable write **0.034 s** — against the previous behaviour, where the
  first write could only happen after ~24 minutes of parsing.
* Duration 834.6 s; DB 1.90 GB; integrity ok; 0 duplicates; FTS consistent.
* Budget was fixed before running (45 min wall, 2.5 GB RSS cap) and respected.

## Tests

| | |
|---|---|
| Targeted scan suites | 83 passed |
| New `test_scan_streaming.py` | **14 tests**, all failing first |
| Core full pytest, local | **1173 passed, 4 skipped, 31 subtests** |
| Plugin `npm test` | 46/46, 0 skipped (also with no `VOYAGER_NO_SYNC` set — see §CI Skill) |
| Plugin `npm run build` | clean |
| `npm pack --dry-run` | 24 files, 27.0 kB |
| Bridge contract | schema v1, same keys, six tools unchanged; `api.py` untouched |
| CI | **10/10 jobs success** — Windows 1172 passed / 5 skipped, Ubuntu 1155 / 22, core-only 1131 / 46. Runs [38720973922](https://github.com/HarryHeYu/sessionFlow/actions/runs/38720973922) (`8203658`) and [38721769542](https://github.com/HarryHeYu/sessionFlow/actions/runs/38721769542) (`b2f41c4`, final HEAD) |

**Mutation evidence.** Three mutations were run. Setting `scan_streams()` to
always return `False` (reverting to materialising) **escaped** the first eight
tests — because both paths are semantically correct and the tests compared
*results*. That is exactly the blind spot that would let the regression return
unnoticed, so two more tests now pin the mechanism: `run_scan` must pass an
`emit` callback, and the first bundle must be written before the last is
parsed. Re-running the mutation now fails exactly those two. Setting the setup
retry count to 1 fails `test_the_setup_retry_actually_retries`.

**A CI failure that was not a defect in the change.** The first push
(`08517ed`) failed on four jobs — all Windows and the py3.11/3.13 Ubuntu ones —
because `test_two_concurrent_streaming_scans_converge` was spawning two children
that were *creating* the index rather than scanning it, so they raced on
`PRAGMA journal_mode=WAL`. That test was wrong, not the scanner; but diagnosing
it exposed D9, which is a real defect and is fixed. Both are in the suite now,
and the final run is green.

## Git

Core commits: `558a94a` (streaming scanner + adapters + tests + §15), and the
report/workflow commits below. Plugin: none — no contract change. Both trees
clean and pushed.

## Issue #18

Recommended **close**, with this report, the before/after table, the crash-
recovery evidence and the CI run URL as the acceptance record.

**Remaining limitations.**
* Only `codex` streams. ZCode and Cursor still materialise; they are smaller in
  practice, and migrating them is now a mechanical change with the same test
  suite to prove it.
* An interrupted scan re-parses the un-stamped remainder rather than resuming
  from a checkpoint. That is the documented D5 cost, unchanged by this round.
* `SCAN_MATERIALISE_ALL_BEFORE_WRITE`'s *pre-read fingerprint snapshot* is still
  one dict entry per source; at 100k sources that is tens of MB and is now the
  largest remaining per-source structure.

## A second defect found on the way: D9

Pushing the cursor scanner meant spawning two real processes against one index
for the concurrency test. On CI both children happened to be *creating* the index
rather than scanning it, and one died — at `PRAGMA journal_mode=WAL` on Ubuntu
and `executescript(SCHEMA)` on Windows, with the message `attempt to write a
readonly database`. The file is writable; what SQLite is actually reporting is
that WAL mode has to initialise the shared-memory file and it refused one of two
processes doing that to the same new database at the same instant. The busy
timeout does not cover it, because SQLite does not classify it as a lock.
Measured: 1 failure in 20 pairs, so it is rare — but the trigger is ordinary
(two terminals' first `voyager scan`), and the error is unhelpful.

The setup was already idempotent by construction (IF NOT EXISTS, guarded ALTERs,
writes that first check there is anything to do), so nothing actually conflicted;
there was simply no tolerance for the loser of the race. `__init__` now retries a
bounded number of times around the setup body, which moved to `_setup()`
unchanged. A failure that still persists raises on the final attempt rather than
hanging or leaving a half-open connection.

The test evidence is deliberately not "the probe passes now" — a 1-in-20 race
proves nothing in one run. `test_the_setup_retry_actually_retries` drives the
mechanism directly by making the first attempts raise exactly that SQLite error
and checking both that a losing-then-winning setup comes up with a real schema
and that a permanently failing one still raises; setting the retry count to 1
fails it.

## Final

**COMPLETE.** The bottleneck is removed with a compatible, additive interface;
the 100k/1M acceptance passed within a pre-declared budget; D5/D6/D7/D8 all
still hold and are now tested at that scale.
