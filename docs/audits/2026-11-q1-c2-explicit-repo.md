# Q1-C2 — explicit `--repo` for `voyager merge`

Status: **ADR / design decision only. No code in this change.**

## User-visible problem

`voyager merge <a> <b>` creates a WorkThread from a set of sessions. When the
sessions come from different repositories, the new thread's `repo_root` is
taken from the first row that has one:

```python
repo_root = next((r["repo_root"] or r["cwd"] for r in rows
                  if r["repo_root"] or r["cwd"]), None)
```

The member set is exact (`thread_find_by_members`), but the repository label is
order-dependent. Reversing the same two session ids can attach the same
WorkThread to a different repo, which changes what `current_work` and the
machine-derived repo scope consider in-scope. The merge identity did not change;
its display/scope metadata did.

## Current contract

- Merge identity is the **member set**, found by
  `Store.thread_find_by_members({sids})`.
- `thread_sessions.ord` follows the caller's session order; it is not derived
  from `repo_root`.
- An existing active thread is reused and its `repo_root` is not updated.
- A new thread's `repo_root` is the first usable row value.
- Bridge schema and Store schema do not expose a merge-repo override today.

## Options

### A — additive `merge --repo <path>` for new threads (recommended)

- If the flag is present and a new thread is created, use the explicit path as
  `repo_root`.
- If the flag is absent, preserve the current first-row behaviour exactly.
- If an existing thread is reused, do **not** mutate its `repo_root`; report a
  warning when the explicit value differs. A metadata rewrite is `relink`, not
  merge.
- Does not change the member set, thread id lookup, `ord`, bundle ordering,
  evidence, provenance, Store schema or Bridge schema.
- Additive CLI flag; existing scripts keep their current behaviour.

### B — derive a common repo or `NULL` from the rows

Rejected. A common-prefix guess can silently widen or narrow scope, and a
`NULL`-scoped thread is invisible under the current `current_work` predicate.
This would change behaviour for existing users without an explicit choice.

### C — model a thread as multi-repo

Not now. It changes the WorkThread concept, the scope predicate and possibly the
schema. It needs a separate compatibility decision, not a merge flag.

## Decision

Adopt **A** when this item is implemented. This change records the decision only;
it does not add the flag, change `_thread_from_merge()`, or alter any existing
merge output.

## Acceptance tests for a future implementation

1. The same two sessions merged in reverse order with `--repo A` produce a new
   thread with `repo_root == A` in both runs.
2. Without `--repo`, the current first-row behaviour is pinned unchanged.
3. `--repo` does not change the member set, `ord`, thread id lookup or bundle
   order.
4. Reusing an existing thread never rewrites `repo_root`; a differing explicit
   value is surfaced as a warning, not silently applied.
5. Rows from one repo with no `--repo` keep the current result.
6. JSON output exposes the chosen `repo_root` and any mismatch warning without
   changing Bridge schema 1.

## `relink` remains separate

A command that changes an existing thread's `repo_root` is a persisted identity
claim. It needs its own decision, audit/timeline entry and explicit user intent.
It must not be folded into `merge --repo`, and it is not part of Q1-C2.

## Compatibility

- CLI: additive optional flag only.
- Store: no schema change.
- Bridge: no schema change; no new op.
- Merge identity: unchanged (member set remains the identity).
- Existing merge order and `ord`: unchanged.
