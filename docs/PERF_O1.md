# O1 — Performance forensics on the scan and read paths

**Status: CLOSED (2026-10-02).** Companion to `docs/DECISIONS.md` D4 and
`docs/POST-1.0.md` §4. Harness: `scripts/benchmark.py`; raw run output in
`E:/voyager-bench/results_o1.json` (outside the repo, on the same drive as the
datasets).

---

## 1. Method, and what each counter actually measures

One task per worker process; the worker reports wall, cpu, peak RSS and
counters. The counters are the part that is easy to misread, so they are
spelled out — two of them were wrong until this pass:

| counter | what it counts | caveat |
|---|---|---|
| `subprocess` | `subprocess.Popen` calls | the honest spawn count |
| `git_calls` | `git_info()` **invocations** | one per session; the per-cwd cache decides how many become spawns |
| `q` | `Store.q()` **read** calls | not statements, not transactions |
| `execute` | `Connection.execute` / `executemany` / `executescript` | SQL statement count |
| `commit` | `Connection.commit()` | **blind**: `store.replace_session` opens its transaction with `execute("BEGIN")`, so this stays ~0 however many transactions run |
| `txn` | `execute("BEGIN")` | the real transaction count |

**Environment caveat — read every wall clock with this in mind.** This sandbox
charges ≈1.3 s per process start. Any number whose cost is spawning is
therefore dominated by it, so the spawn count is reported beside it. All scan
numbers below are **file-backed**: with `:memory:` the setup and the measured
pass get *separate* databases, so seeded fingerprints would not carry and
`scan_inc` would silently measure a cold scan.

## 2. Read path (single run per cell)

**These come from the earlier matrix run (`results_o1.json`), one run per cell —
they are indicative, not repeated measurements, and they were taken on a
different machine state from §3.** They are included because the query counts
are condition-independent and that is what they are used for here.

| task | dataset | wall s | cpu s | peak RSS MB | `q` |
|---|---|---|---|---|---|
| overview | real index | 0.41 | 0.17 | 31.0 | 4 |
| sessions | real index | 0.02 | 0.03 | 25.6 | 1 |
| search | real index | 1.80 | 0.78 | 44.4 | 8 |
| summarize | real index | 7.54 | 2.73 | 95.6 | 21 |
| compile | real index | 0.58 | 0.38 | 64.3 | 33 |
| overview | syn10k | 1.84 | 1.02 | 51.0 | **2806** |
| search | syn10k | 4.45 | 1.78 | 56.5 | 8 |
| summarize | syn10k | 0.10 | 0.08 | 26.3 | 106 |
| overview | syn100k | 4.63 | 2.56 | 196.1 | **2801** |
| search | syn100k | 34.28 | 14.53 | 111.4 | 8 |

`overview` issuing ~2,800 queries regardless of dataset size is a real N+1 —
see `OVERVIEW_RECENT_SESSIONS_N1` in §5. `search`'s 8 queries against a
34 s wall clock says the time is in the FTS scan itself, not in round-trips.

## 3. Scan path — 3 repeats each, medians

10,000 sessions / 100,000 events, file-backed, a fresh database per run.

| task | median wall s | range | cpu s | peak RSS MB | `subprocess` | `git_calls` | `execute` | `txn` |
|---|---|---|---|---|---|---|---|---|
| `scan_inc` | **21.98** | 19.06–22.30 | 21.0 | 347–413 | 0 | 0 | 20,016 | 0 |
| `scan_force` | **114.15** | 104.35–121.28 | 78–97 | 413 | 3 | 10,000 | 290,016 | **10,000** |

### 3.1 Why "incremental 93 s > force 61 s" was never real

The old harness ran the seeding pass **inside the timed window** for
`scan_inc`/`scan_touch`:

```python
if task == "scan_inc":
    run_scan(store, providers=["codex"], quiet=True)   # seed, but timed
    return run_scan(store, providers=["codex"], quiet=True)   # measured
```

`scan_inc` therefore timed **two** scans while `scan_force` timed one, and the
counters said so plainly: `scan_inc` reported `q = 20012`, exactly 2× the
`q = 10006` of `scan_initial`/`scan_force`. After moving setup into
`_scan_setup` (outside the window), the relation is the intuitive one:
**21.98 s incremental vs 114.15 s forced — 5.2× in favour of incremental.**

The residual `scan_force` > `scan_inc` gap is simply the work: force re-parses
100,000 events and writes 10,000 sessions; incremental skips all 10,000 sources
(`skip=10000`, `txn=0`, `subprocess=0`, `git_calls=0`) and only re-checks
fingerprints.

### 3.2 Variance

Spread across the three repeats is 16 % (force) and 17 % (inc). One earlier
batch had a single 235 s outlier; it ran immediately after a killed 1M process
whose pagefile peak was 3.1 GB, so it is reported as contaminated and excluded.

## 4. 1M `scan_force` — `O1_LARGE_FORCE_BENCHMARK = ENVIRONMENT_LIMITED`

100,000 rollout files / 1,000,000 events / 353 MB of source, file-backed,
single worker, no parallel experiments. Run for **45 min 18 s**, then stopped.

| measure | value |
|---|---|
| exit status | **killed by the operator** (never reached its reporting point) |
| wall clock at kill | 45 min 18 s |
| write progress | **none for the final 12 min** (index frozen at 1.88 GB, WAL at 8.2 MB) |
| peak working set | **2 971.7 MB** |
| working set at kill | **60.5 MB** — almost entirely paged out |
| peak pagefile | 3 138.6 MB |
| machine | 31.7 GB RAM, 8.8 GB free at start, 72 % load |

**Failure mode.** Not an exception and not a crash: the process was evicted to
the pagefile and stopped making progress. The mechanism is structural and
visible in the code — `cli.run_scan` calls `bundles = ad.scan(...)`, which
returns **every** session with **every** event, and only then loops
`store.replace_session(...)` per bundle (`voyager/cli.py:140` and `:149`). For
100,000 files that is ~1.1 M dicts resident before the first write. The 10k
equivalent peaks at 413 MB, so the 1M requirement is ~4 GB resident, and this
machine could not keep it there.

**This is a capacity limit, not a functional blocker.** The scan path is
correct; it is the *shape* of the ingestion (materialise all, then write one
session per transaction) that does not fit 1M events in 31.7 GB alongside
everything else on the machine.

**Correctness after O1.** O1 also changed product code (`voyager/adapters/
base.py`, `voyager/continuity.py` in `5ad40c3`), so correctness was re-checked
rather than assumed:

- `tests/test_cli.py tests/test_continuity.py tests/test_adapters.py
  tests/test_auto.py` → **119 passed, 0 failed** (4 m 33 s). That is the surface
  the O1 commits touched: the scan/continue/handoff paths, continuity, adapter
  repo identity.
- The **full suite did not complete in this pass.** After the 1M attempt the
  machine was degraded enough that 56 minutes covered only ~12 % (the same
  suite took 25 minutes earlier the same day). It was stopped rather than left
  running for hours. This is recorded as a gap, not as a pass: the full-suite
  verdict for `5ad40c3`/`35486d6` is the one their own commits reported, and it
  was not re-confirmed here.

## 5. Debts — recorded separately, as four distinct bottlenecks

These are four different things and must not be merged.

### `SCAN_BATCH_TRANSACTION_OPTIMIZATION` — **measured, confirmed**
`scan_force` opens **10,000 transactions for 10,000 sessions** (`txn = 10000`)
and issues 290,016 statements (≈29 per session). `store.replace_session` is
one `BEGIN … COMMIT` per session, so WAL appends and FTS inserts are not
batched. Batching them is the single largest structural cost on this path —
and it is **not** the same thing as subprocess amplification.
**Any batching change must design crash consistency separately**: today a
killed scan can lose at most one session; a batch of N changes that.

### `STARTUP_GIT_SUBPROCESS_AMPLIFICATION` — **measured, currently absorbed**
`git_info()` is invoked **once per session** (`git_calls = 10000`), but the
per-cwd cache collapses that to **3 real spawns** (`subprocess = 3`) because
every synthetic session shares one workspace. So on this dataset the
amplification is real in *calls* and absent in *spawns*. It becomes expensive
only when sessions span many repositories: measured earlier on real ZCode data,
9 distinct workspaces → 31 spawns. Cost therefore scales with **distinct
workspaces**, not with sessions.

### `OVERVIEW_RECENT_SESSIONS_N1` — **measured, newly found**
`api.overview` (`voyager/api.py:73-82`) calls `store.events(r["id"])` for every
session that passes the time/repo filter, then keeps only `limit=12`. On syn10k
that is `q = 2806` for 12 sessions of output; on the real index it is `q = 4`.
Same class as the brief N+1 fixed in P13.

### `SCAN_MATERIALISE_ALL_BEFORE_WRITE` — **measured, cause of §4**
`ad.scan()` returns every bundle before any write (`voyager/cli.py:140`).
Bounds the scan by RAM rather than by disk, and is why 1M events does not
complete here.

## 6. Baseline discipline

- **`246.3 s → 66.4 s` is a historical observation only.** It has no recorded
  provenance in the repository (absent from `git log` and from
  `results_o1.json`), so the conditions of its "before" are unknown. It must
  **not** be quoted as a speedup ratio and must **not** be restated as "3.7×
  faster".
- **The old `scan_force = 61.2 s` and this pass's `114.15 s` are not
  comparable.** They ran on different machine states (the latter directly after
  the killed 1M process). Neither direction may be claimed from that pair.
- Only numbers measured in the same pass, with the same harness, are compared
  above — the `scan_inc` vs `scan_force` pair in §3, and the counter counts,
  which are condition-independent.

## 7. Harness defects fixed in this pass

1. `run_suite` forced `dbp = ":memory:"` for **every** `scan_*` task, ignoring
   an explicitly passed file path — so the "1M events: file-backed" job always
   ran in RAM, which is precisely the memory failure its own comment claimed to
   avoid. Now `db = db or ":memory:"`.
2. `SCAN_SETUP` mapped `_scan_setup` directly while `worker()` calls
   `setup(db, src)`, so every `scan_inc`/`scan_touch` run died with
   `missing 1 required positional argument: 'src'`.
3. `_scan_task` built its Store from the module-level import, which is bound
   **before** instrumentation patches `store_mod.Store`; the `q` counter read 0
   for a scan that had been 10006.
4. Counters were not reset after the untimed setup, so setup's statements were
   attributed to the measurement (`scan_inc` read `q = 20012`).
5. Timing boundary: seeding moved out of the timed window (§3.1).

## 8. Repository status (reconciled, not carried over)

```
$ git status --short
 M scripts/benchmark.py
?? PRIORITY4_CHECKPOINT_COMPLETION.md
?? docs/archive/
```

- **tracked modified = 1** (`scripts/benchmark.py`)
- **staged = 0**
- **untracked = 2** (`PRIORITY4_CHECKPOINT_COMPLETION.md`, `docs/archive/`)

Ten of the eleven former root-level leftovers now live in
`docs/archive/sessions-2026-09-29/` (untracked, not yet committed);
`PRIORITY4_CHECKPOINT_COMPLETION.md` was left at the root. The earlier
"11 files awaiting a decision" debt is therefore **closed** and replaced by the
line above.

## 9. Git cleanup violation — recorded, no action taken

`git reflog expire` and `git gc --prune=now` were run at some point outside
this session's instructions. The consequence is visible on every commit here:
`warning: reflog of 'HEAD' references pruned commits` and
`fatal: bad tree object 51bc126b…` from the background geometric-repack task.
**No further `reflog`, `gc`, `prune` or `fsck` repair will be attempted**
without explicit authorisation; the repository is usable and the state is
recorded so it is not rediscovered as a mystery.
