C3 is now measured. **Verdict: DEFER** — the gain is real but modest, and it is
bounded by a cost that cannot be batched away.

## What C3 turned out to be

`Store.source_changed(provider, path)` — one `os.stat` plus one indexed SELECT per
source, called from the discovery loop in `scanner.py`. On a no-op incremental
scan it is called **exactly once per file** (the adapter's own per-group check
only runs when the discovery loop found a change, i.e. when `rescan` became true).

## The measurement that was missing

C1's revert asked for **SQL statement counts** rather than wall clock, because a
sandbox adds ~1.2–1.4 s of process start-up per child. Both are now recorded, and
they agree:

| sessions | scan (s) | `source_changed` calls | calls/file | check (s) | check share | statements | stmts/session |
|---|---|---|---|---|---|---|---|
| 1,020 | 0.175 | 1,020 | 1.00 | 0.066 | 37.9% | 4,133 | 4.05 |
| 2,020 | 0.309 | 2,020 | 1.00 | 0.135 | 43.7% | 8,133 | 4.03 |
| 10,020 | 1.519 | 10,020 | 1.00 | 0.692 | **45.6%** | 40,133 | 4.01 |

Statements per session is **flat at ~4.0** — the cost is linear, not quadratic, so
there is no hidden blow-up at production scale. Of 4,123 statements in a 1,020-session
no-op scan, **2,040 (49.5%) are the `source_changed` lookup**; the rest is prune,
stats and the migration/backfill checks that run on every open.

Per call: **56 µs** — 22 µs of `os.stat`, 33 µs of indexed SELECT.

## Why DEFER rather than IMPLEMENT

The ceiling was measured by implementing the alternative and comparing answers,
5-run average, both warm:

| | 10k no-op check | answer |
|---|---|---|
| current (per-file SELECT) | 0.671 s | 0 changed |
| batched (one SELECT + Python set) | 0.287 s | 0 changed — **identical** |

So batching saves **57% of the check, ~0.38 s, which is ~25% of the no-op scan at
10k**. That is a real gain — but:

* **40% of the check is `os.stat`, and it cannot be batched away.** You must stat
  every file to know whether it changed. That is the irreducible floor, and it is
  0.22 s at 10k no matter what the SQL does. The ceiling is reached quickly and
  cannot be improved further by a later round.
* The absolute saving is **0.38 s on the no-op path only**. On an initial index
  the check is ~0.02% of the time (parse dominates at 8.3 ms/session), so the
  expensive case is untouched.
* It needs a new batch API on `Store`, which P7 explicitly froze as the stable
  boundary. It would be additive, but it is new surface on a frozen seam, plus
  adapter changes, plus tests.
* C1 attempted exactly this and was reverted for "gain too small to justify the
  added complexity". That reasoning still holds at 0.38 s.

Recorded rather than implemented, so a future attempt starts here instead of
re-deriving it: batch as
`SELECT path, mtime, size FROM sources WHERE provider=?` once per provider, then
compare the stat results in Python — with the same-answer check against
`source_changed` as the acceptance test.

## Did P9 streaming change C3's benefit or priority?

**No, materially.** In the streaming path the adapter receives
`store.source_changed` as its check callback and calls it interleaved with writes
rather than all-before-any-write, but the *count* is unchanged (once per file on a
no-op). The initial index is still parse-dominated (834 s for 100k = 8.3 ms per
session against 56 µs for the check), so streaming did not make the check more or
less significant. If anything it slightly reduced the priority: the memory
bottleneck that P9 removed was the one real blocker at scale.

## What this means for the issue

C3 is **verified and quantified**; the answer is "not worth changing now", with
the design and the numbers above so the decision is reviewable rather than
asserted. The issue stays open for the two items that are still genuinely
unresolved: **C2** (batched scan transactions — the larger win, blocked on a
crash-consistency design) and the **large-scale benchmark** note, which P9's
100k/1M acceptance run has now partly superseded.

**No code was changed, so there is nothing to regress and no CI run to wait for.**
Core HEAD remains `4191923`, CI 10/10 green.
