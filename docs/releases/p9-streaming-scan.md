# P9 STREAMING SCAN REPORT

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
| New `test_scan_streaming.py` | **12 tests**, all failing first |
| Core full pytest, local | **1169 passed, 4 skipped, 31 subtests** |
| Plugin `npm test` | 46/46, 0 skipped |
| Plugin `npm run build` | clean |
| `npm pack --dry-run` | 24 files, 27.0 kB |
| Bridge contract | schema v1, same keys, six tools unchanged; `api.py` untouched |

**Mutation evidence.** Two mutations were run. Setting `scan_streams()` to
always return `False` (reverting to materialising) **escaped** the first eight
tests — because both paths are semantically correct and the tests compared
*results*. That is exactly the blind spot that would let the regression return
unnoticed, so two more tests now pin the mechanism: `run_scan` must pass an
`emit` callback, and the first bundle must be written before the last is
parsed. Re-running the mutation now fails exactly those two.

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

## Final

**COMPLETE.** The bottleneck is removed with a compatible, additive interface;
the 100k/1M acceptance passed within a pre-declared budget; D5/D6/D7/D8 all
still hold and are now tested at that scale.
