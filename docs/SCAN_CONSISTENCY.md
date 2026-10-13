# Scan write path — crash consistency contract

**Status**: design document for Issue #12 (`SCAN_MATERIALISE_ALL_BEFORE_WRITE`,
`SCAN_BATCH_TRANSACTION_OPTIMIZATION`). Written before any code change, as the
issue requires.

**Baseline**: `7541a93` (main). Everything below was read out of that tree; the
one defect it names was reproduced, not inferred.

---

## 1. The write path as it actually is

Per provider, `run_scan()` (`voyager/cli.py`) does:

| # | step | transaction |
|---|---|---|
| 1 | `ad.discover()` → source paths | — |
| 2 | `ad.unusable_sources()` → present-but-unreadable paths | — |
| 3 | no sources at all → `prune_missing_sessions(provider, set())` | one, mark-only |
| 4 | per source: `store.source_changed(provider, src)`; any `True` ⇒ `rescan = True` | read only |
| 5 | `disk_paths = discovered ∪ unusable` | — |
| 6 | `rescan` false ⇒ `skip` everything, never call `scan()` | — |
| 7 | multi-session adapter: `ad.scan(force ? always : store.source_changed)`, then **one `replace_session()` per bundle** | **one per session** |
| 7' | per-file adapter: per source, `ad.parse(src)` then `replace_session()` | **one per session** |
| 8 | `prune_missing_sessions(provider, disk_paths)` | one per provider, mark-only |
| 9 | `store.stats()`, print | read only |

`replace_session()` (`voyager/store.py`) is the atomic unit. Inside a single
`BEGIN … COMMIT` it:

* deletes the session's old `event_fts`, `events` and `files` rows,
* `INSERT OR REPLACE`s the `sessions` row,
* inserts every event **and** its FTS row,
* inserts the `files` rows,
* **`INSERT OR REPLACE`s the `sources` row(s) with the fingerprint taken now**,
* records `SOURCE_RETURNED` on the owning threads if it was retained.

So **the session row, its events, its FTS rows, its files and its source
fingerprint all become visible in the same commit.** That is the property the
whole recovery story rests on, and it is the property a naive "batch the writes"
change would destroy.

`prune_missing_sessions()` runs in the implicit transaction SQLite opens on the
first DML statement (`isolation_level=''`) and commits once at the end, so all
marking for one provider is atomic. It **marks**, never deletes (O2 / D15).

Connection settings: `journal_mode=WAL`, `synchronous=NORMAL`, 64 MB page
cache, busy timeout 5 000 ms (the `sqlite3.connect` default).

Durability consequence, stated precisely:

* **process killed** → committed transactions survive (they are in the WAL);
  uncommitted ones are discarded on reopen. Nothing is half-written.
* **power loss / OS crash** with `synchronous=NORMAL` → the last commits may be
  lost, but the database is never left inconsistent.

## 2. Invariants that must not be broken

1. **I1 — atomic session ingest.** (session, events, FTS, files, fingerprint)
   are one commit. No state where the fingerprint is fresh but the events are
   from an older read of that file.
2. **I2 — no duplicate events.** Re-ingesting a session yields the same rows,
   because the old rows are deleted in the same transaction.
3. **I3 — marking is non-destructive and reversible.** `SOURCE_MISSING` keeps
   events/files/FTS and is cleared by the next scan that sees the source.
4. **I4 — re-scan converges.** After any interruption, a plain re-scan (no
   `--force`) reaches the same index as an uninterrupted one.
5. **I5 — `--force` is an accelerator, never the recovery path.**

I1–I3 hold today. **I4 does not** — see §5.

## 3. Scenario-by-scenario

| # | interruption | what happens | verdict |
|---|---|---|---|
| 1 | killed mid-parse | nothing written for that session; its source row is unchanged or absent ⇒ `source_changed` true ⇒ re-parsed | **safe** (per-file) / **not safe** for multi-session artifacts (§5) |
| 2 | killed mid-write | cannot happen: one session is one transaction; SQLite discards uncommitted frames | **safe** |
| 3 | killed before COMMIT | rollback; the session's previous content is intact (its DELETE was in the same transaction) | **safe** |
| 4 | killed after COMMIT, before fingerprint update | **cannot happen** — the fingerprint is written inside the same transaction (I1) | **safe** |
| 5 | `SQLITE_BUSY` | after the 5 s timeout `OperationalError` propagates out of `replace_session` (post-ROLLBACK) **and out of `run_scan`** — there is no handler, so `voyager scan` dies with a traceback. Committed sessions stay committed; the rest retry next scan | **consistent, but abrupt** (§5, D6) |
| 6 | source mutated during the scan | `replace_session` re-stats the file at write time and stores **that** fingerprint, while the events came from the earlier read ⇒ **fresh fingerprint + stale events**. The appended bytes are not indexed until the file changes again | **real, narrow race** (§5, D7) |
| 7 | source corrupt / temporarily unreadable | `parse` yields `__error__` or raises ⇒ session skipped, old fingerprint kept ⇒ retried next scan; `unusable_sources()` keeps the path in `disk_paths` so prune does **not** mark it missing | **safe** |
| 8 | repeated scan of identical data | fingerprint matches ⇒ skip ⇒ zero writes. Forced re-scan re-runs `replace_session`, which is idempotent | **safe, idempotent** |

## 4. What the single-writer lease guarantees here

**Nothing.** D13's lease is per **WorkThread** and answers "which agent may write
this thread's canonical log". `run_scan()` takes no lease, and no index-level
lock exists. Two concurrent `voyager scan` processes are serialized only by
SQLite's own WAL locking and the 5 s busy timeout.

That is defensible — the lease exists for a different race (two agents writing
one conversation) — but it means "there is a single writer" must not be assumed
when reasoning about the scan. The concurrency guarantees on this path are
SQLite's, not Voyager's.

## 5. Findings

### D5 — a partial write of a multi-session artifact is not recoverable by re-scan *(confirmed, data-visible)*

`source_changed(provider, path)` is satisfied by **any single row** for that path
carrying the current fingerprint. That is correct for a per-file adapter, where
one file is one session. It is wrong for `cursor`, `zcode` and Codex's app
store, where **one SQLite artifact yields many sessions** and the pipeline writes
them one at a time.

If a scan writes k of n sessions and then dies, the artifact already has rows
with the current fingerprint, `source_changed` returns **false**, `rescan` stays
false, and the remaining n−k sessions are never indexed — until the artifact
happens to change, or someone runs `--force`.

Reproduced on a two-session ZCode database
(`E:\sessionflow-scratch\p13\probe_partial_write.py`):

```
adapter.scan()      -> 2 bundles
complete scan       -> 2 sessions indexed;  source_changed -> False   (correct)
partial write (1/2) -> 1 session indexed;   source_changed -> False   (WRONG)
plain re-scan       -> 1 session indexed
VERDICT: NOT RECOVERABLE — 1 session permanently missing
```

This is a **pre-existing** defect, not a consequence of batching. It matters
here because it is the sharpest counter-example to "the per-session transaction
boundary is what makes interruption safe": the boundary is fine, but the
**freshness marker's granularity does not match the write unit**, and that is
what breaks I4.

The control isolates the mechanism. Same partial-write shape, but with two
independent session files (DSH, one `.jsonl.zstd` per session) instead of one
two-session artifact (`E:\sessionflow-scratch\p13\probe_perfile_control.py`):

| case | after partial write | plain re-scan | verdict |
|---|---|---|---|
| per-file (DSH, 2 files) | 1/2 | **2/2** | self-healing |
| one artifact, many sessions (ZCode, 2 sessions) | 1/2 | **1/2** | not recoverable |

Both use the same `replace_session()`, the same transaction boundary and the
same pipeline. The only difference is whether one source path maps to one
session or to many — so that, and not the transaction boundary, is the cause.

### D6 — a write error aborts the whole scan *(robustness gap — reported, deliberately not changed)*

The per-file path guards `ad.parse(src)` with try/except but calls
`store.replace_session()` unguarded. One `SQLITE_BUSY`, one disk-full, one I/O
error on one session ends the entire scan with a traceback, and the user gets no
summary of what did and did not land. Consistency is preserved; usability is not.

**Not changed in this round, on purpose.** The obvious fix — catch and continue —
is wrong as written: in the multi-session path it would let the artifact be
stamped while a session is missing, which is D5 all over again. A correct fix has
to tolerate the error *and* withhold the stamp so the next scan retries, and that
is a change to the failure semantics of the write path. It deserves its own
round and its own tests rather than riding along with a correctness fix.

### D7 — source mutated between parse and stat *(narrow race)*

See scenario 6. Real, but the window is one parse and the cost is delayed
indexing, not loss. Worth a note; not worth a risky fix.

## 6. Constraints for any change (binding on Phase 3/4)

* **C1** Keep I1: (session, events, FTS, files, fingerprint) in one commit.
* **C2** Never widen the "fresh fingerprint, stale events" window.
* **C3** Keep prune mark-only, per-provider atomic and idempotent.
* **C4** Any batching must preserve I4 — re-scan must converge **without**
  `--force`. A design that only converges under `--force` is not a fix.
* **C5** Do not relax `synchronous`, journal mode, or remove the transaction
  protection to gain throughput.
* **C6** No crash injection against a real user database; crash tests use a
  throwaway index and a real child process.

## 7. What this means for Issue #12

* `SCAN_BATCH_TRANSACTION_OPTIMIZATION` (C2 in the issue) remains **deferred by
  design**: any batching must still satisfy I1 and I4. §6 is the checklist it
  has to pass.
* `SCAN_MATERIALISE_ALL_BEFORE_WRITE` is a **memory** claim, not a correctness
  claim — but materialising is what lets `replace_session` write the whole
  session in one transaction. Stream-writing would break I1 unless the write
  unit changes with it. Measure before touching.
* **D5 is a separate, confirmed correctness defect** and is the only thing in
  this document that warrants code in this round. It is fixed in Phase 4 with a
  change that keeps every invariant above.

---

## 8. Where the time actually goes

Measured with the harness's own counters on `codex_src10k` (10,000 sessions /
100,000 events), `scripts/benchmark.py`, Windows, Python 3.13.12
(`E:\sessionflow-scratch\p13\decompose_scan.py`):

| stage | wall | SQLite statements | transactions |
|---|---|---|---|
| `discover()` | 0.04 s | 0 | 0 |
| **parse only (no DB)** | **179.7 s** | 0 | 0 |
| **write only (pre-parsed bundles)** | **71.9 s** | 280,011 | 10,000 |
| `source_changed()` × 10,000 | 0.64 s | 10,000 | 0 |
| **`prune_missing_sessions()`** | **21.1 s** | 30,001 | 1 |
| `stats()` | 0.01 s | 2 | 0 |
| whole no-op `run_scan()` | 22.8 s | 40,007 | 0 |

Two things fall out of this, and neither was visible from the totals alone:

1. **A no-op scan is 93% `prune_missing_sessions`.** Fingerprint checking is
   0.64 s; prune is 21.1 s, for 30,001 statements — three per session. The
   lookup `SELECT path FROM sources WHERE provider=? AND sid=?` could only use
   the `provider` prefix of the primary key `(provider, path, sid)`, so it
   scanned every source row of the provider once per session.
2. **A full re-index is 71% parsing.** Writing is 28.6% of it. Anything that
   only touches the write path can at best move a third of the total.

### Shape matters more than size

Same 100,000 events, two distributions (`E:\sessionflow-scratch\p13\phase2_shape.py`):

| shape | task | wall | CPU | peak RSS | statements | transactions |
|---|---|---|---|---|---|---|
| 10,000 sessions × 10 events | `scan_initial` | 68.79 s | 61.05 s | 406.9 MB | 320,020 | 10,001 |
| 10,000 sessions × 10 events | `scan_force` | 70.10 s | 62.06 s | 407.0 MB | 320,020 | 10,001 |
| 50 sessions × 2,000 events | `scan_initial` | 25.10 s | 22.61 s | 358.7 MB | 200,620 | 51 |
| 50 sessions × 2,000 events | `scan_force` | 22.42 s | 19.86 s | 359.1 MB | 200,620 | 51 |

Three times the cost for the same number of events, and 60% more statements —
because a session's fixed overhead (one transaction, one session row, its FTS
rows, one prune lookup) is paid per **session**, not per event. `scan_inc` is
also stable across repeats (1.765 / 1.937 / 1.823 s, spread 0.17 s), so the
numbers above are reproducible rather than single samples.

## 9. Candidate designs

| | A — per-session txn (as-is) | B — bounded batch txn | C — stream the materialised bundles |
|---|---|---|---|
| expected gain | — | fewer COMMITs | lower peak RSS |
| measured gain | — | **≤ 3.5% of the write path** (see below) | none on this dataset (parse is CPU-bound) |
| integrity | unchanged | batch becomes the atomic unit | unchanged *if* per-session materialisation stays |
| max rollback | one session | N sessions | one session |
| retry / idempotency | re-scan converges | must keep the freshness marker in step with the batch, or it inherits D5 | re-scan converges |
| memory | O(all sessions) | O(all sessions) | O(one session) |
| complexity | none | medium — new transaction boundary **and** a new freshness rule | medium — `scan() -> List[dict]` becomes an iterator; `len(bundles)` / `bundles[0]` in tests; parse errors move from around `scan()` into the write loop, which interacts with D5 |
| fallback | — | revert one call site | revert one call site |
| **decision** | **keep** | **NO_CHANGE** | **defer, with a number** |

### B — why NO_CHANGE

Batching only removes COMMITs; the DML is identical, so the entire benefit is
`(transactions avoided) × (cost of one commit)`. That cost was measured directly
rather than assumed — 280,000 rows inserted either as 10,000 single-statement
transactions or as one transaction
(`E:\sessionflow-scratch\p13\measure_fixes.py`):

```
280000 rows in  10000 transactions :    6.228 s
280000 rows in      1 transaction  :    3.738 s
commit overhead total              :    2.490 s   (0.25 ms per commit)
```

For 10,000 sessions that is **2.49 s against a 71.9 s write path — 3.5%** — and
the write path is 28.6% of a full re-index, so batching a full re-index is worth
**about 1%**. That is an *upper* bound: it assumes every commit disappears and
adds no complexity. Against that, the batch becomes the atomic unit, which is
exactly the property `docs/PERF_O1.md` said the current design exists to
protect, and §5 shows how easy it is to get the freshness marker out of step
with the write unit.

Issue #12 deferred this for the same reason. It now has a number.

### C — why deferred rather than done

Peak RSS during a full scan is ~414 MB for 100,000 events here, and
`docs/PERF_O1.md` recorded 2,971.7 MB for 1,000,000 events — about 2.9 KB per
event, linear. At the sizes where that hurts, the fix is real. But it changes
the adapter contract in a way that is not contained to one call site (tests use
`len()` and indexing), and — more importantly — moving parse errors inside the
write loop is precisely the situation D5 is about. Changing both in one round
would make it impossible to tell which change made an interruption safe. The
measured design is recorded here so the next round starts from it.

### D and E — the two changes this round does make

Both are inside design A, so no crash semantics move.

| change | kind | evidence |
|---|---|---|
| **D — index `sources(provider, sid, path)`** | additive DDL | prune 13.17 s → **0.548 s (24×)**; whole unchanged scan 21.41 s → **1.62 s (13.2×)** with an **identical** statement count (40,019), so the gain is the index and not the machine |
| **E — defer the freshness stamp for multi-session artifacts** | correctness (D5) | the artifact stops looking unchanged only once it is really fully written; costs one extra commit per artifact |

E is why `run_scan` writes the multi-session path with
`stamp_sources=False` and then calls `Store.stamp_sources()` once. A scan that
dies part-way leaves the artifact unstamped, so the next plain scan re-reads it
and rewrites every session. The writes are idempotent, so redoing is always
safe — and the alternative (stamping per session) is exactly what made a partial
write look complete.

## 10. End-to-end result

`scripts/benchmark.py run`, `codex_src10k` (10,000 sessions / 100,000 events),
Windows, Python 3.13.12. "before" is the same harness on the same dataset
immediately before this change.

| task | before | after | speed-up | statements before | statements after |
|---|---|---|---|---|---|
| `scan_inc` (nothing changed) | 21.41 s | **1.61 s** | **13.3×** | 40,019 | 40,019 |
| `scan_touch` (20 files touched) | 23.35 s | **3.41 s** | **6.9×** | 50,579 | 50,581 |
| `scan_initial` (cold) | 104.32 s | 67.88 s | 1.5× | 320,018 | 320,020 |
| `scan_force` (full re-index) | 130.58 s | 75.68 s | 1.7× | 320,018 | 320,020 |

The statement counts are what make this trustworthy: `scan_inc` does **exactly
the same work** before and after (40,019 statements, 0 transactions), so the
13× is the index and not a faster machine or a warmer cache. `scan_touch` and
the two cold tasks gain by the same mechanism — prune runs on every scan — plus
the one extra transaction per artifact that E costs (`txn` 20 → 21, 10,000 →
10,001; `execute` +2). The cold-task figures carry this sandbox's usual
run-to-run variance (~25% between two identical runs), so they are reported as
observed rather than as a precise multiplier.

Peak RSS is unchanged (~407 MB on this dataset): neither change touches
materialisation, which §9 C leaves for its own round.

