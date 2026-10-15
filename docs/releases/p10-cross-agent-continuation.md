# P10 — Cross-Agent Continuation: characterization and one fix

## Why this round

P9 closed the last structural blocker (Issue #18). The next thing that actually
matters to a user is not more governance — it is whether "continue unfinished
Codex or Claude work" is true. So this round characterised the real
cross-provider continuation output and fixed the one defect it exposed.

## What was checked

An index containing a real **codex** session and a real **claude** session
(the committed fixtures, both scanned through `run_scan`), then a real
`handoff_thread()` for:

1. a single codex session, and
2. **codex + claude together** — the cross-provider case.

### The cross-provider bundle, as produced

```markdown
# Continuation Bundle

## Goal
**Primary user goal:** continue the unfinished work

## Goal-ranked evidence
goal: "..." — top 6 of 6 ranked facts (deterministic; each line is provenance-bound):

- [codex:11111111-...-#1] (user, score 1.7467) fix the parser
- [claude:cla-1111#2]    (tool_call, score 0.869) {"file_path": "E:/proj/demo/a.py"}
- [codex:...-#3]         (tool_call, score 0.8681) python -m pytest
- [claude:cla-1111#4]    (assistant, score 0.669) done reviewing
- [codex:...-#5]         (assistant, score 0.6681) parser fixed
- [claude:cla-1111#1]    (user, score 0.569) review this file

## Latest assistant conclusion
**Where work stopped (active session: `claude` `cla-1111`, newest by activity
(these sessions belong to no WorkThread)) -- that session's latest conclusion,
read as what it said rather than as verified fact:**
done reviewing

## Other sessions' conclusions
> Note: These are the other sessions' latest conclusions. A newer session is not
> automatically more correct -- where they disagree with the section above, keep
> both readings and check the repository snapshot, files and commands below
> before deciding.
- `[codex:11111111-2222-33]` (2026-08-01 18:01): parser fixed

## Files touched across sessions
- `E:/proj/demo/a.py`

## Commands executed
- `python -m pytest`

## Evidence & Provenance
Compiled from 2 session(s):
- **codex** `11111111-...`: fix the parser (2 msgs / 1 tools, 2026-08-01 18:00 → 18:01 repo=`E:/proj/demo`)
- **claude** `cla-1111`: review this file (2 msgs / 1 tools, 2026-08-02 18:00 → 18:00 repo=`E:/proj/demo`)
```

**The three invariants that were at risk are all visible in that output, in the
output itself rather than only in the code:**

* *Assistant Conclusion ≠ Verified Fact* — stated explicitly, in the sentence the
  reader sees: "read as what it said rather than as verified fact".
* *Different Agents may conflict* — the "Other sessions' conclusions" section
  warns that a newer session is not automatically more correct and tells the
  reader to keep both readings.
* *Evidence is attributable* — every ranked line carries
  `[provider:native_id#seq]`, so a reader can go back to the source event.

Ranking is deterministic, cross-provider evidence is interleaved by score rather
than by provider, and the provenance footer counts what actually came from where.
No invented facts, no coverage claim beyond the two sessions compiled.

**Conclusion: the cross-agent continuation experience is genuinely good.** It was
verified rather than assumed, and this is the evidence.

## The one defect found

`sessions=["codex:abc"]` — ids rather than rows — **crashed**:

```
TypeError: string indices must be integers, not 'str'
  continuity.py:1296, in resolve_handoff_source
```

`resolve_handoff_source` is the single normaliser every handoff entry point goes
through (CLI `handoff` / `switch` / `merge` / `continue`, and the MCP tools), and
its whole design is to return an `error` dict rather than raise. A raw `TypeError`
from three frames deep breaks that promise, and it is reachable by any caller
holding ids. The CLI and MCP happen to pass rows, which is why nothing caught it.

**Fixed:** `sessions` now resolves string entries through `store.session()`
exactly as `source` already did. Ids, rows and a mixture all work; an unknown ref
returns `{"error": "session not found: ..."}` with empty members; an ambiguous
prefix reports the candidate count instead of guessing — matching the resolver's
own rule that recency is not evidence of intent.

Two tests, both failing first. The mutation reproduces the original `TypeError`
at the same line and is caught immediately.

## Verification

| | |
|---|---|
| Targeted | `tests/test_continuity.py` 39 passed |
| Full suite, local | **1176 passed, 4 skipped, 31 subtests** |
| CI | **10/10 jobs success** on `e097a4c` — [run 38759351841](https://github.com/HarryHeYu/sessionFlow/actions/runs/38759351841) |
| Plugin | unchanged; no bridge-contract change |
| Tags | unchanged (`v1.0.0`, `v1.0.0-rc1/2`, `v1.1.0`) |

Also in this round, a CI failure from P9 was fixed (`fe8d85c`, CI 10/10): the
concurrent-scan test asserted 3 sessions after two concurrent scans, but 1 in 8
pairs is legitimately short under write-lock contention and 8 of 8 converge on a
follow-up scan. The test now asserts the real contract. That investigation also
exposed that breaking the shared-artifact stamp filter **escaped every test** —
the existing isolation test gave each session its own path, so the filter was
never exercised; a shared-path guard now catches it.

## Git

Core: `fe8d85c` (concurrency test), `e097a4c` (session refs). Plugin: unchanged.
Both trees clean and synced.

## What is still not verified

* Native resume is asserted by capability declaration, not exercised end to end
  in this round — the bundle path is what was characterised.
* The characterisation used fixtures. Two real provider installs on one machine
  would be stronger, and is the natural next step.
* `git dirty` warning behaviour and lease interaction were not re-tested here;
  they have their own suites.

## Verdict

**The cross-Agent continuation experience holds up.** One real robustness defect
found and fixed. No architectural change was needed, which is the expected result
after P9 — and the useful outcome is that the claim on the box is now backed by
output a reader can be shown.
