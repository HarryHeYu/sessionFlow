# Context Intelligence experiment — equal-budget command relevance — 2026-11-19

> **ADR only. No product ranking rule was changed.** Synthetic, no LLM. This
> records an experiment against the in-repo benchmark, its result, and why the
> result is NOT sufficient to modify `voyager.ranker`.

## Decision

**Do not change the ranking rule this round.** A minimal down-weight of
*successful* tool_calls frees a goal-ranked top-3 slot in one synthetic
hold-out, but the gain is marginal and the noise problem remains. One
hand-built scenario is not evidence for a general rule. Revisit only with a
larger, independently-authored scenario set — preferably with real (authorised)
histories — and a stated acceptance threshold.

## The question

The 2026-11-19 reproducible benchmark's hold-out I recorded a negative result:
in the `## Goal-ranked evidence` top-3, a successful `env -u HTTPS_PROXY
pytest -q` tool_call occupied a slot while the goal fact (`SSL cert verify
failed`) did not make top-3. The earlier review also flagged that `pytest -q`
kept appearing near the top across different goals. Is command-shaped evidence
over-consuming the fixed top-3 budget when the goal barely overlaps it?

## Setup (reproducible)

- Scenarios **I** (weak goal/command token overlap) and **C** (goal strongly
  overlaps a *failed* command) from `scripts/continuity_benchmark.py`.
- Facts extracted from the synthetic sessions via the real
  `extract_candidate_facts`; scored with the real `score_fact` and goal
  expansion (`tokenize` / `expand_tokens`).
- **Current** variant: product `score_fact` unchanged.
- **Minimal down-weight** variant: subtract `FACT_PRIORITY["tool_call"]` from
  the score of a tool_call whose `exit_code in (0, None)` only. A failed
  command (`exit_code != 0`) keeps its full score, including the `+0.4`
  failed-command bump. No other weight, no special case, no LLM/vector store.
- Pinned by `tests/test_continuity_benchmark.py::test_successful_command_can_crowd_out_goal_fact_and_downweight_recovers_it`
  (synthetic, isolated DB).

## Results

Equal budget = the fixed 3-line `## Goal-ranked evidence` top-3 (both variants
emit the same number of lines; this is a *ranking* comparison, not a token
saving).

| Scenario | Variant | top-3 content | goal fact in top-3 | successful pytest in top-3 | failed cmd in top-3 |
|---|---|---|---|---|---|
| C (`fix CI pipeline`) | current | GOAL_C, `pytest -q`(exit 1), CI_C workflow | yes | n/a (that cmd is failed evidence) | **yes** |
| C | down-weight | identical | yes | n/a | **yes** |
| I (`unset TLS proxy`) | current | GOAL_I, `env -u HTTPS_PROXY pytest -q`(exit 0), NOISE_I | **no** | **yes** | n/a |
| I | down-weight | GOAL_I, NOISE_I, ERR_I(SSL cert verify) | **yes** | **no** | n/a |

Reading:

- In C the command is a **failed** test (`exit 1`), i.e. genuine evidence the
  ranker should keep. The minimal rule leaves it untouched — top-3 is byte-for-
  byte identical. This is the "must not damage command evidence" guard.
- In I the successful pytest command is noise for a TLS goal; the current
  ranker still ranks it second. The minimal rule moves it out and the SSL
  failure enters top-3 — the desired direction.
- But NOISE_I (`refactor logging format`) also remains in top-3, and ERR_I
  enters only on a score tie with NOISE_I (broken by timestamp). The improvement
  is real but **fragile**, not a clean win.

## Why no product change

1. **Gain is marginal and tie-dependent.** One of two top-3 slots changes, and
   the replacement ranks level with unrelated noise.
2. **Generic rule risk.** `tool_call` is not always noise: a successful command
   can be the outcome a goal asked for ("run the migration", "deploy"). The
   proposed rule keys only on `exit_code == 0`, which is exactly the case where
   a command may be the goal. The synthetic I happens to make it irrelevant;
   that is a property of the fixture, not a law.
3. **No acceptance threshold was set before the experiment.** With no
   pre-declared "N of M scenarios must improve with zero regressions", a single
   positive is a hypothesis, not a gate.

The observation is preserved as a **NOT VERIFIED for general ranking** finding
and as a reproducible test; it is not written up as a shipped improvement.

## Contradiction / supersession boundary

Per the round's constraints, contradiction handling was only examined as an ADR
and a synthetic example. The benchmark requires the literal `keep both readings`
marker (scenarios B, D) and the Core preserves both sides of a conflict; it does
**not** infer that an old conclusion is invalidated, and nothing here adds a
`supersedes` relation or silently drops historical evidence. Any structured
supersession must be a separate, explicitly-accepted design with its own
ACID-safe semantics — not a ranking side effect.

## Scope

No change to `voyager/ranker.py`, Store/Bridge schema, or any shipped behaviour.
No LLM, embedding, vector DB, or independent long-term memory system introduced.
