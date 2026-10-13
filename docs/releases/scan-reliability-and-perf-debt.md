# Scan reliability and performance debt — Issue #12

**Baseline**: `7541a93` (main). **No tag moved, nothing published.**

This round was audit-first. Two defects were confirmed and fixed; two of Issue
#12's three open items were closed with measurements rather than code, and the
one that was fixed was fixed by an index rather than by touching a transaction.

Design document for the crash-recovery contract:
[`docs/SCAN_CONSISTENCY.md`](../SCAN_CONSISTENCY.md).

---

## 1. Baseline

| | |
|---|---|
| `main` HEAD | `7541a93` = `origin/main`, working tree clean |
| tags | `v1.0.0`, `v1.0.0-rc1`, `v1.0.0-rc2`, `v1.1.0` — untouched |
| CI at baseline | 10/10 jobs green (`38336008611`) |
| Issue #12 | OPEN. C1 *reverted* (gain too small, measurement untrustworthy), C2 *deferred* ("changes crash-recovery semantics … needs a crash-consistency design before any code"), C3 *kept, performance unverified*, 100k/1M not re-run since the revert |

The benchmark harness was fixed in the previous round (file-backed scan jobs so
`scan_inc` is not secretly a cold scan; the `overview` window pinned so the task
does not drift with the calendar). **Nothing this round reintroduced either
defect** — every scan number below comes from a job that starts from a fresh
database on disk, and the repeat runs are identical to ±5%.

What incremental and force actually mean, confirmed in code:

* **incremental** — for each discovered source, `Store.source_changed()` compares
  `(mtime, size)`. If nothing changed, the adapter is never called and the round
  is a no-op plus prune.
* **force** — bypasses that check *and* the adapter's own per-file filter, so
  every session is re-parsed and re-written.

## 2. Crash-consistency contract

The write path, its transaction boundaries, the five invariants it must keep and
a scenario-by-scenario recovery table are in
[`docs/SCAN_CONSISTENCY.md`](../SCAN_CONSISTENCY.md) §1–§4. The short version:

* One session is **one transaction**, and it covers the session row, its events,
  its FTS rows, its files **and its source fingerprint**. There is therefore no
  state where the fingerprint is fresh and the events are stale (invariant I1),
  and no half-written session.
* `prune_missing_sessions()` **marks**, never deletes, and is idempotent.
* The **single-writer lease (D13) provides no guarantee here.** It is
  WorkThread-scoped — "which agent may write this thread's log" — and `run_scan`
  takes no lease at all. Two concurrent scans are serialized only by SQLite's WAL
  and the default 5 s busy timeout.

### Confirmed defect — D5: a partial write of a multi-session artifact was unrecoverable

`source_changed(provider, path)` is satisfied by **any single row** for that
path. Correct for a per-file adapter (one file, one session). Wrong for
`cursor`, `zcode` and Codex's app store, where **one SQLite artifact yields many
sessions** and the pipeline writes them one at a time. Write k of n and die, and
the artifact already looks unchanged: the remaining n−k sessions are never
indexed again without `--force`.

Reproduced on a two-session ZCode database, with a per-file control so the cause
is isolated (`E:\sessionflow-scratch\p13\probe_*.py`):

| case | after partial write | plain re-scan | verdict |
|---|---|---|---|
| per-file (DSH, 2 files) | 1/2 | **2/2** | self-healing |
| one artifact, many sessions (ZCode, 2 sessions) | 1/2 | **1/2** | **not recoverable** |

Both use the same `replace_session()` and the same transaction boundary. The
only difference is whether one source path maps to one session or to many — so
the transaction boundary is not the cause; the **granularity of the freshness
marker** is.

### Reported, deliberately not changed

* **D6** — a write error (`SQLITE_BUSY`, disk full, I/O) propagates out of
  `replace_session()` and kills the whole scan with a traceback; the user gets no
  summary of what landed. Consistency is fine; usability is not. The obvious fix
  is wrong as written (catching the error in the multi-session path would stamp
  the artifact with a session missing — D5 again), so it needs its own round.
* **D7** — a source mutated between parse and stat yields a fresh fingerprint
  with stale events; the appended bytes wait for the next change. Narrow, and
  the cost is delayed indexing, not loss.

## 3. Benchmark

Same machine, same datasets, `scripts/benchmark.py`, Python 3.13.12, Windows.
Sandbox process start-up is ~1.2–1.4 s, so every conclusion below is anchored to
an environment-independent counter (statements, transactions, spawns).

**Where the time goes** — `codex_src10k`, 10,000 sessions / 100,000 events:

| stage | wall | statements | transactions |
|---|---|---|---|
| `discover()` | 0.04 s | 0 | 0 |
| **parse only (no DB)** | **179.7 s** | 0 | 0 |
| **write only (pre-parsed bundles)** | **71.9 s** | 280,011 | 10,000 |
| `source_changed()` × 10,000 | 0.64 s | 10,000 | 0 |
| **`prune_missing_sessions()`** | **21.1 s** | 30,001 | 1 |
| whole no-op `run_scan()` | 22.8 s | 40,007 | 0 |

Two things the totals hid:

* **A no-op scan is 93% prune.** Fingerprint checking costs 0.64 s; prune costs
  21.1 s for 30,001 statements. `SELECT path FROM sources WHERE provider=? AND
  sid=?` could only use the `provider` prefix of the primary key
  `(provider, path, sid)`, so it scanned every source row of the provider once
  per session. `EXPLAIN QUERY PLAN` says so verbatim:
  `SEARCH sources USING COVERING INDEX sqlite_autoindex_sources_1 (provider=?)`.
* **A full re-index is 71% parsing.** The write path is 28.6%, so no
  write-path-only change can move more than about a third of the total.

**Shape beats size** — same 100,000 events, two distributions:

| shape | task | wall | peak RSS | statements | transactions |
|---|---|---|---|---|---|
| 10,000 × 10 | `scan_force` | 70.10 s | 407.0 MB | 320,020 | 10,001 |
| 50 × 2,000 | `scan_force` | 22.42 s | 359.1 MB | 200,620 | 51 |

Three times the cost for the same events, because a session's fixed overhead is
paid per **session**. `scan_inc` repeats at 1.765 / 1.937 / 1.823 s (median
1.823, spread 0.17 s) — the measurements are stable, not single samples.

## 4. Design comparison

| | A — per-session txn | B — bounded batch txn | C — stream the materialised bundles |
|---|---|---|---|
| expected gain | — | fewer COMMITs | lower peak RSS |
| measured | — | **≤ 3.5% of the write path** | none here (parse is CPU-bound) |
| integrity | unchanged | the batch becomes the atomic unit | unchanged *if* per-session materialisation stays |
| max rollback | one session | N sessions | one session |
| complexity | none | new transaction boundary **and** a new freshness rule | `scan() -> List[dict]` becomes an iterator; tests use `len()`/indexing; parse errors move inside the write loop |
| decision | **keep** | **NO_CHANGE** | **defer, with a number** |

**B — why NO_CHANGE.** Batching removes COMMITs and nothing else; the DML is
identical. So the entire benefit is `(transactions avoided) × (cost of one
commit)`, and that cost was measured rather than assumed — 280,000 rows inserted
as 10,000 single-statement transactions versus one transaction:

```
280000 rows in  10000 transactions :    6.228 s
280000 rows in      1 transaction  :    3.738 s
commit overhead total              :    2.490 s   (0.25 ms per commit)
```

**2.49 s against a 71.9 s write path — 3.5%** — and the write path is 28.6% of a
full re-index, so batching a full re-index is worth about **1%**. That is an
upper bound. Against it: the batch becomes the atomic unit, which is exactly what
`docs/PERF_O1.md` says the current design exists to protect, and §2 shows how
easy it is to get the freshness marker out of step with the write unit.
Issue #12 deferred this for the same reason; it now has a number.

**C — why deferred rather than done.** Peak RSS is ~2.9 KB per event (414 MB at
100,000 events here; `docs/PERF_O1.md` recorded 2,971.7 MB at 1,000,000). At the
sizes where that hurts the fix is real — but it changes the adapter contract
beyond one call site, and moving parse errors inside the write loop is precisely
the situation D5 is about. Doing both in one round would make it impossible to
tell which change made an interruption safe. The measured design is recorded so
the next round starts from it.

## 5. Implementation

Two changes, both inside design A — **no crash semantics move**.

**D — index `sources(provider, sid, path)`** (additive DDL, applied on open by
the existing `CREATE INDEX IF NOT EXISTS` in `SCHEMA`):

* `prune_missing_sessions`: **13.17 s → 0.548 s (24×)** on the same database.
* Whole unchanged scan: **21.41 s → 1.61 s (13.3×)**, with an **identical**
  statement count (40,019) — the gain is the index, not the machine.

**E — defer the freshness stamp for multi-session artifacts** (D5).
`Store.replace_session(..., stamp_sources=False)` writes the session but not its
`sources` row; `run_scan` calls the new `Store.stamp_sources()` once, after the
whole artifact has been written. A scan that dies part-way leaves the artifact
unstamped, so the next plain scan re-reads it and rewrites every session. The
writes are idempotent, so redoing is always safe. Cost: one extra commit per
artifact (`txn` 20 → 21 on `scan_touch`).

`replace_session`'s default is unchanged, so every other caller — including the
entire per-file path — behaves exactly as before.

End-to-end, `codex_src10k`:

| task | before | after | speed-up | statements before | statements after |
|---|---|---|---|---|---|
| `scan_inc` | 21.41 s | **1.61 s** | **13.3×** | 40,019 | 40,019 |
| `scan_touch` | 23.35 s | **3.41 s** | **6.9×** | 50,579 | 50,581 |
| `scan_initial` | 104.32 s | 67.88 s | 1.5× | 320,018 | 320,020 |
| `scan_force` | 130.58 s | 75.68 s | 1.7× | 320,018 | 320,020 |

Peak RSS is unchanged (~407 MB): neither change touches materialisation.

### Tests

Every change has a test that failed first, and the failure was re-confirmed by
temporarily restoring the old behaviour:

| file | tests | what it pins |
|---|---|---|
| `tests/test_scan_consistency.py` | 4 | D5 via an interrupted `run_scan`; the per-file control; prune's plan must use the index; prune's marking semantics unchanged |
| `tests/test_scan_crash.py` | 2 | **a real child process terminated with `os._exit`** — one killed mid-artifact (re-scan must converge), one killed inside a session transaction (the transaction must be rolled back whole: 0 sessions, 0 events, 0 source rows) |

The crash tests do not simulate a kill with an exception: the child dies with no
unwinding, no `finally`, no flush, so SQLite's own recovery is what is being
tested. They run only against a throwaway index in `tmp_path`.

Mutation check — flipping `stamp_sources=False` back to `True`:

```
FAILED tests/test_scan_crash.py::test_a_kill_mid_artifact_write_is_recovered_by_a_plain_rescan
FAILED tests/test_scan_consistency.py::test_partial_write_of_a_multi_session_artifact_is_recovered_by_rescan
2 failed, 4 passed
```

**CI is the full-suite gate** — run [`38363927135`](https://github.com/HarryHeYu/sessionFlow/actions/runs/38363927135)
on `c5bfeda`, **10/10 jobs success** (ubuntu-latest and windows-latest, py3.10–3.13, plus the
core-only installs):

```
1100 passed, 4 skipped, 31 subtests passed
```

1094 before this round, 1100 after — the six new tests, and the same four
data/platform-gated skips as before.

One caveat, stated plainly: **the full suite was not completed locally this
round.** It was started, and after ~29 minutes it had produced no output and an
empty `--basetemp`, with `tasklist` itself starting to fail. That is this
sandbox's known delete-guard failure mode, and it was self-inflicted — the
measurement scripts in this round ran many `rm -f` loops in the same turn, which
is the documented way to exhaust the guard. The targeted files were re-run green
afterwards, and CI ran the whole suite on two platforms, so the gate is real;
but the local full run is not evidence, and is not presented as such.

## 6. Issue #12

| item | state |
|---|---|
| C1 — batch source fingerprint checking | still reverted; unchanged. The measurement caveats it lists (no statement counting, no RSS) are now satisfied by the harness, but the change itself was not worth re-attempting |
| C2 — `SCAN_BATCH_TRANSACTION_OPTIMIZATION` | **NO_CHANGE, with evidence.** Ceiling measured at 0.25 ms/commit → ~1% of a full re-index, against a redesign of the atomic unit |
| C3 — `SCAN_MATERIALISE_ALL_BEFORE_WRITE` | **still open, now quantified.** ~2.9 KB/event; a concrete design is recorded in `docs/SCAN_CONSISTENCY.md` §9 C, deliberately not implemented in this round |
| large-scale benchmark (100k/1M) | **still not re-run.** Out of scope for the sandbox; the shape results above are the substitute evidence |
| **D5 (new)** | **fixed** — a partial write of a multi-session artifact is now recoverable by a plain re-scan |
| **missing index (new)** | **fixed** — the no-op scan path was 13× slower than it needed to be |

**Issue #12 should stay open**, narrowed: C2 is decided, C3 and the large-scale
run remain, and the honest summary is "the write path is not the bottleneck".

## 7. Release recommendation

**Yes — v1.1.1 is worth cutting, and this round did not cut it.**

Contents that are ready:

1. the DSH `native_resume` correction (previous round) — a user-visible
   capability that the matrix advertised and the CLI refused;
2. the `overview` N+1 fix (previous round) — 1.724 s → 0.278 s, 10,016 → 28
   statements;
3. **the prune index** — 13.3× on the operation every scan, every handoff
   pre-scan and every `_ensure_fresh` performs;
4. **the D5 fix** — a silently-missing-history bug for Cursor/ZCode/Codex-store
   users who have ever interrupted a scan.

Suggested gates, because **the extended `release.yml` has never run against a
real tag**:

* cut `v1.1.1` from a commit that is green on both CI platforms;
* treat the Release step as unverified until it completes — it is a draft until
  every asset is attached, so a failure announces nothing;
* if it fails, fix the workflow and re-run against the same tag; **do not move
  the tag**. The workflow never overwrites an existing asset, so a re-run is
  safe;
* one extra thing to watch: the new index is built the first time an existing
  index is opened after upgrade. It is a one-off cost proportional to the
  `sources` table, and it happens on the first `voyager scan`, not at import.

Excluded on purpose: batching (NO_CHANGE), materialisation streaming (deferred),
and the D6/D7 robustness gaps.

## 8. Final status

**COMPLETE.**

Two confirmed defects fixed with tests that failed first and were
mutation-verified; two of Issue #12's open items closed by measurement rather
than by code; one deferred with a number and a design. No production code was
changed where the evidence did not support it — batching and streaming were both
investigated and both deliberately left alone. No tag moved, no release
published, no PyPI/npm.
