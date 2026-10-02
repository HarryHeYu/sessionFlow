# Archive — session reports, 2026-09-29

These files were produced by an autonomous work session on 2026-09-29 and were
left untracked in the repository root. They are kept here for provenance, not
maintained: they describe an intermediate state of the project and some of the
numbers in them are stale (for example the test-coverage claims — the two
`test_*.py` scripts below sit outside `pyproject.toml`'s `testpaths = ["tests"]`,
so pytest never collected them, and both fail on collection today).

**Do not run `clean_qoder_cache.py`.** It is a one-shot destructive script that
`shutil.rmtree`s `tmp/`, `cache/` and `bin/` under `C:\Users\<user>\.qoder`
including the note "this includes the large git-staging directory". It is kept
only as a record of what the session did, not as a tool. It is not referenced
anywhere in the project and nothing imports it.

Current performance state and the four open debts live in
[../PERF_O1.md](../PERF_O1.md); decisions in [../DECISIONS.md](../DECISIONS.md).
