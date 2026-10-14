# AGENTS.md

Instructions for AI coding agents working in this repository. Read this first;
it is short on purpose.

## What this is

**sessionFlow** (package `voyager`) indexes the session history of every AI
coding agent on the machine, so you can search across them and continue work
from one agent in another. It is local-only, and it **never writes provider
session files**.

The DSH plugin lives in a separate repository (`dsh-sessionflow`). The two are
bound only by the bridge contract in `voyager/api.py`
(`BRIDGE_SCHEMA_VERSION`).

## Before you change anything

1. Read [docs/INDEX.md](docs/INDEX.md) to find the document that owns the area.
   It classifies every document and says which are maintained and which are
   frozen history.
2. Read [docs/GLOSSARY.md](docs/GLOSSARY.md) for the canonical names. Use them.
   Do not invent a second name for a concept that already has one.
3. If you touch the scan write path, read
   [docs/SCAN_CONSISTENCY.md](docs/SCAN_CONSISTENCY.md) — it is the contract, and
   its invariants are pinned by tests.
4. If you are about to "clean up" something that looks duplicated, check
   [docs/DECISIONS.md](docs/DECISIONS.md) first. Several things that look like
   duplication are deliberate isolation, and the reason is recorded.

## Skills

- **`.agents/skills/sessionflow-ci/SKILL.md`** — how to run the quality gates:
  scoping a change, targeted tests, the full suite, the plugin build and tests,
  reading GitHub Actions, diagnosing a flaky test, verifying the bridge contract,
  and committing and pushing safely. Load it before claiming any change works.

Agents that discover skills elsewhere can point at that file directly; the
procedure is the file, not the path.

## Rules that matter more than style

- **Do not delete anything to make a problem go away.** A missing source means
  the history is retained and marked `SOURCE_MISSING`, never removed.
- **Do not merge `same_repo` and `same_repo_loose`.** They are deliberately
  different, and tests pin both behaviours.
- **Do not add a second handoff engine or a second context compiler.** There is
  one, in `voyager/continuity.py`.
- **Do not reuse the WorkThread lease as a scan lock.** Different scope,
  different lifetime; the scanner takes no lease.
- **Do not claim a capability is verified.** `SUPPORTED` means implemented;
  only evidence promotes a cell to `LIVE_VERIFIED`.
- **Do not run `git stash` in this repository.** See the CI skill, step 10.

## Reporting

State what you changed, what you ran, what passed, and what you did **not**
verify. An unverified item reported as unverified is useful. The same item
reported as verified is a defect.
