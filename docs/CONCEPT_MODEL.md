# Concept model

The entities, how they relate, and the invariants that must hold. Each
relationship names the code that implements it and, where one exists, the test
that would fail if it broke.

This is a description, not a system: there is no RDF, no graph database and no
separate knowledge store. The model lives in SQLite tables, Python dataclasses
and the tests.

## Entities and relationships

```
Provider ──produces──▶ Session ──contains──▶ Event
                          │
                          │ backed by
                          ▼
                       Source ──(lifecycle)──▶ ACTIVE_SOURCE
                          │                     SOURCE_MISSING
                          │                     ARCHIVED_CANONICAL
                          ▼
                    recoverable?

WorkThread ──groups──▶ Session (many, possibly across Providers)
     │
     ├── Checkpoint  (deterministic snapshot of the work)
     └── Lease       (which Provider may write the thread log, right now)

Session evidence ──compiled by──▶ Context ──packed as──▶ Continuation Bundle
                                        └──transferred by──▶ Handoff
                                                                 │
                                                     Switch / Merge (same engine)

Native Resume ──uses──▶ Provider capability + an intact Source   (not a Handoff)
```

| Relationship | Implemented in | Pinned by |
|---|---|---|
| Provider produces Session | `voyager/adapters/*.py` → `store.replace_session` | `tests/test_adapters.py` |
| Session is backed by Source | `sources` table; `(provider, path, sid)` | `tests/test_scan_consistency.py` |
| Session contains Event | `events`, `event_fts` | `tests/test_store.py`, `tests/test_search.py` |
| WorkThread groups Sessions | `threads`, `thread_sessions` | `tests/test_threads.py` |
| WorkThread is resolved, never guessed | `auto.resolve_work_thread` | `tests/test_auto.py` |
| Bundle synthesises session evidence | `continuity.handoff_thread` | `tests/test_continuity.py` |
| Handoff transfers context | `continuity.handoff_thread` (one engine) | `tests/test_continuity.py` |
| Native resume needs capability **and** source | `adapters/base.py`, `capability_matrix` | `tests/test_capability_truth.py` |
| Source lifecycle gates recovery | `store.py` `source_state`, `auto.py` | `tests/test_retention.py` |
| Provenance attributes an event | `voyager/provenance.py` | `tests/test_provenance.py` |

## Invariants

**I1 — A missing Source does not delete its Session.**
When every source of a session disappears, the row is marked
`SOURCE_MISSING`; history, search and timeline keep returning it, and only live
continuity stops. Nothing in the codebase deletes a session because a file went
away. Reversal is a scan, not a command.
Code: `store.py` (`source_state`), `auto.py` · Test: `tests/test_retention.py`

**I2 — A WorkThread can span Providers.**
Thread membership is a list of session ids, and a session id is
`provider:native_id`; nothing requires the providers to match. Continuity
compiles across them, and a merge is scoped to the providers of the sessions
being merged rather than to one.
Code: `store.py` (`thread_sessions`), `continuity.py` · Test: `tests/test_continuity.py`

**I3 — An assistant conclusion is not a verified fact.**
Text produced by an agent — including text this project compiled — is a claim.
Only a verification-evidence row or a test is a fact about the repository. The
bundle marks claims as claims, and the capability matrix separates `SUPPORTED`
(implemented) from `LIVE_VERIFIED` (observed) for exactly this reason.
Code: `provenance.py`, `capability_matrix.py` · Test: `tests/test_capability_truth.py`

**I4 — Native resume capability is not context synthesis capability.**
A provider can declare `can_resume` and still have no session-start hook, no
context injection, and no way to attach a session automatically. The two are
measured as separate dimensions; a green resume tick says nothing about
continuity.
Code: `capability_matrix.py::DIMENSIONS` · Test: `tests/test_capability_consistency.py`

**I5 — Two agents can disagree about the same task.**
Sessions from different providers carry different conclusions about the same
repository. Voyager stores both and attributes each; it does not pick a winner,
and continuity does not present one agent's claim as the state of the work.
Code: `provenance.py`, `continuity.py` · Test: `tests/test_continuity.py`

**I6 — A partial write is never recorded as complete.**
For a shared artifact, no source is stamped fresh until every session in it has
been written. A failed write withholds the stamp for the whole artifact, so the
next scan re-reads it. Stamping the survivors is the defect D5 and would strand
the failed session permanently.
Code: `scanner.py` (`stamp_sources`, `_pre_read_fingerprints`) ·
Test: `tests/test_scan_error_isolation.py`, `tests/test_scan_source_change.py`

**I7 — Opening the index never needs the write lock.**
Every entry point — scan, continue, status, the MCP tools, the DSH bridge — must
be able to open the database while another process is writing. `Store.__init__`
performs no unconditional writes.
Code: `store.py::__init__` · Test: `tests/test_concurrent_open.py`

**I8 — The lease is not a scan lock.**
A lease is scoped to one WorkThread and its holder's lifetime. The scanner takes
no lease; concurrent scans are protected by SQLite WAL and the busy timeout, and
the measured behaviour is convergence without contention.
Code: `store.py` (`thread_leases`), `scanner.py` ·
Evidence: `docs/releases/v1.1.1-reliability-hardening.md`

## What this model is not

It is not a promise that every entity is independently replaceable. `Store` is
one module on purpose: the tables above share transaction boundaries, and
splitting them into per-entity repositories would make the invariants above
harder to state, not easier. That was assessed in
[releases/p7-architecture-audit.md](releases/p7-architecture-audit.md) and the
conclusion was to leave it as it is.
