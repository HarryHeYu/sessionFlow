# O6 — Architecture Hygiene Audit

**Date**: 2026-10-03
**Status**: Complete
**Decision**: D19 (`docs/DECISIONS.md`)

O6 had two halves: an architecture-hygiene pass, and completing the roadmap
items that were still open. This file records what the audit found and what
was done about it.

---

## Part 1 — Architecture hygiene

### Finding 1 — `_same_repo` had **five** copies and **two** behaviours

The same name meant different things in different modules:

| Location | Behaviour |
|---|---|
| `startup.py` (×2, nested) | strict: equal, or one path is a **suffix** of the other |
| `api.py`, `auto.py`, `cli.py` | loose: strict **plus** raw substring containment |

`startup.py` uses it to decide whether two active WorkThreads are the same repo
before auto-attaching a session — there, a *false match* attaches work to the
wrong thread, so strict is the safe choice. The other three use it for
user-facing matching, where a partial name typed on the command line
(`code`) must match a stored full path (`E:/code/voyager`).

Copy-paste plus a "close enough" edit is how the two diverged silently. The
risk is real: a repo the CLI considered "the same" was a different repo to the
startup path, and nothing said so.

**Fix.** One definition in `voyager/util.py`, with the variants *named*:

- `same_repo(a, b)` — strict (exact / path-suffix)
- `same_repo_loose(a, b)` — strict + substring

Each former call site now calls the variant it actually had, so behaviour is
unchanged and the difference is visible at the call site instead of hidden in a
copy.

### Finding 2 — `_fmt_ts` had **four** copies and **two** formats

- `cli.py`, `continuity.py`, `handoff.py` — `YYYY-MM-DD HH:MM`
- `export.py` — `YYYY-MM-DD HH:MM:SS`, with a UTC→local conversion

**Fix.** `voyager/util.py`:

- `fmt_ts(ts)` — the minute-precision default
- `fmt_ts_seconds(ts)` — the export variant (second precision, so two events in
  the same minute stay orderable)

`export.py`'s former `Optional[float]` annotation was also a latent bug: it was
never imported (`from __future__ import annotations` masked the undefined name).
Removing the copy removed the trap.

### Finding 3 — dead duplicate CLI: `capability_matrix.main()`

`voyager/capability_matrix.py` carried its own `argparse` CLI
(`python -m voyager.capability_matrix --matrix --json`). It is:

- **not** a `[project.scripts]` entry point,
- referenced by **no** test and **no** current doc (only an archived checklist),
- superseded by `voyager verify [--matrix] [--json]`, shipped in O5.

**Fix.** Removed the function and the `__main__` block; the module docstring now
points at `voyager verify`. The import API (`summary()`, `matrix()`,
`provider_state()`) is unchanged.

### Finding 4 — a test pinned a private name

`tests/test_continuity.py::test_flat_path_remains_byte_identical` monkeypatched
`voyager.continuity._fmt_ts`. Updated to the canonical
`voyager.continuity.fmt_ts` (the module re-exports the shared function).

### Guard against regression

`tests/test_util.py` (7 tests) pins:

- the strict vs loose semantics, including the case that distinguishes them
  (`code` inside `E:/code/voyager` is a substring but not a suffix);
- that loose is a strict superset of strict;
- that `_same_repo` / `_fmt_ts` are **not defined anywhere** under `voyager/`,
  and the shared names are defined **only** in `util.py`;
- that every former duplicator now imports the canonical helper.

The last two are the actual guard: a copy that comes back fails the suite.

---

## Part 2 — Completing open roadmap items

### Scoped pre-compile scan (POST-1.0 §4)

`continue` / `handoff` / `merge` must refresh the index before compiling (D12),
but they were refreshing **every** provider. Added
`cli._scan_scope_for_sessions(store, refs)`:

- returns the union of the named sessions' providers, **plus** every member
  provider of any WorkThread those sessions belong to (the engine compiles the
  whole thread);
- returns `None` — scan everything — whenever the set cannot be proven, e.g.
  a named ref that is not indexed yet (the exact case the pre-compile scan
  exists for), an ambiguous prefix, or a store error.

Wired into `cmd_handoff` and `cmd_merge`. `cmd_continue` already scoped to an
explicit `--platform`; `cmd_switch` resolves its thread inside the engine and
still scans all providers — scoping it would mean resolving the thread before
the scan, which the engine owns, so it is left alone rather than duplicated.

The guarantee is unchanged: scoping can only *narrow* which providers are
walked, and only when every source that command reads is already known to be in
scope. `tests/test_scan_scope.py` (7 tests) pins the contract, including the
fallback-to-all cases.

### Roadmap truth-up

`docs/POST-1.0.md` and `docs/ROADMAP.md` under-reported what had shipped:

- **#8 / Phase 7**: the Context Composer webview, the **timeline view**
  (`vscode-extension/timeline.js`, `voyager.openTimeline`) and the **one-click
  switch** (`voyager.switchThread`) are all present now, but both docs still
  listed the last two as pending. Only VSIX packaging / marketplace listing
  remains.

Corrected in both files. No code changed for this part.

(An earlier note claimed `#6` / Phase 5 was unmarked; checking the file, it
already carries `shipped `2f01a7f`` in both the phase heading and the issue
table — the note was stale, and no change was needed.)

---

## Still open (not part of O6)

These need something this sandbox cannot supply, or are explicit non-goals:

- **#10 transcript writers** — Claude (headless resume timed out) and DSH need
  a live probe HIT before a writer exists.
- **#8 packaging** — VSIX build + marketplace listing need `vsce` and a
  publisher account.
- **PyPI release** — needs credentials and a final package name.
- **filesystem event watcher** — replace the 300s poll; correctness must not
  depend on it (D12), so it is an optimisation, not a gap.
- **Cursor token counts** — often 0; needs the real `agentKv` usage payload to
  reverse-engineer.
