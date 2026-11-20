# Continuity benchmark — Level 2 Agent Effectiveness harness — 2026-11-20

> **Correction (same date):** the v1 harness described below mixed Oracle goal/thread inputs with work discovery and kept grader data in the shared work tree. It is superseded by the v2 fairness correction in `docs/audits/2026-11-20-level2-fairness-correction.md`. The v1 DRY-RUN numbers below remain historical context-construction observations and are NOT autonomous discovery results.

> **Honest status: harness delivered and DRY-RUN verified; LIVE MODEL NOT VERIFIED.**
> No agent was launched, no paid call made, no completion rate invented. This
> extends Core PR #64's Level 1 harness; it does not rewrite its scorer.

## Two layers, kept separate

| Layer | What it measures | Method | Status |
|---|---|---|---|
| **Level 1** (PR #64) | Context *preparation* quality | deterministic synthetic scoring, no LLM | reproducible (0/17/11/24 A–H) |
| **Level 2** (this) | Actual agent *continuation* outcome | hidden acceptance tests on a real synthetic repo | harness real; agent outcome **NOT VERIFIED** |

Level 2 does not reuse Level 1's metrics as a proxy for success. It runs the
incoming agent on a real code repo and judges the resulting code with tests the
agent never saw. The most important metric is `independent_tests_pass`, not
tool-call count.

## Task construction (synthetic, isolated)

`scripts/agent_continuity_benchmark.py::build_task` builds, under a
caller-supplied workdir:

- a **green starting repo** (`calc.py` = `add(a, b)`, visible test passes);
- a **hidden acceptance test** (`test_add_three_args`) stored OUTSIDE the repo,
  requiring `add(a, b, clamp=10)` — this requirement exists **only** in a
  prior-agent session, so the incoming agent cannot infer it from the code;
- a synthetic index with the goal-bearing session, a contradicting older
  session (`SOURCE_MISSING`, "default should be 100"), a correct WorkThread and
  a decoy WorkThread ("unrelated docs cleanup");
- four context files: `no_history`, `raw_history`, `latest_summary`,
  `sessionflow_bundle` (equal 4000-token budget).

The harness asserts the task is genuine: the hidden test **fails** on the green
starting repo. If a mutation makes the answer visible in `calc.py`, two quality
gates go red (answer-leak + sanity). No real session, index, home or repo is
read or written; no LLM/embedding/vector DB/long-term memory is introduced.

## Metrics recorded

- **Context-quality (measured here)**: per-baseline `context_tokens`.
- **Agent outcome (all LIVE_MODEL_NOT_VERIFIED until an authorised run)**:
  used_sessionflow, found_correct_workthread, restored_correct_goal,
  fabricated_task, used_historical_evidence, final_code_meets_goal,
  independent_tests_pass, tool_call_count, token_spend, elapsed_seconds,
  failure_type.

DRY-RUN (default) builds everything and marks every outcome metric
`LIVE_MODEL_NOT_VERIFIED`. With `--runner '<command with {prompt} and {repo}>'`,
the harness runs that command once per baseline, then evaluates the resulting
repo with the hidden tests — and only then fills `independent_tests_pass` /
`final_code_meets_goal` from the real result. Other outcome fields (e.g.
`used_sessionflow`, `token_spend`) stay null until a run-specific probe supplies
them; the harness never guesses them.

## Additional scenarios required by the round

The current constructor covers: requirement-only-in-history, two conflicting
agent conclusions, multiple WorkThreads + a decoy, `SOURCE_MISSING`, and an
equal-budget comparison. The remaining two the round listed — "user says only
'continue'" and "long context budget insufficient" — are representable with the
same constructor by varying the prompt file and `--budget`; they are declared
as **future scenario variants**, not claimed as already measured.

## DRY-RUN result (executed here, synthetic only)

| Baseline | context tokens | independent verdict |
|---|---:|---|
| no_history | 0 | LIVE_MODEL_NOT_VERIFIED |
| raw_history | 46 | LIVE_MODEL_NOT_VERIFIED |
| latest_summary | 47 | LIVE_MODEL_NOT_VERIFIED |
| sessionflow_bundle | 542 | LIVE_MODEL_NOT_VERIFIED |

Sanity: `hidden test fails on starting repo = True` (the task is real).
`tests/test_agent_continuity_benchmark.py`: 7/7 quality gates pass; a mutation
that exposed the answer turned 2 gates red.

## Reproduction

```bash
python scripts/agent_continuity_benchmark.py --workdir <isolated-dir> --out r.json
# LIVE MODEL — only with explicit authorisation and an installed agent command:
python scripts/agent_continuity_benchmark.py --workdir <isolated-dir> \
  --runner 'my-agent --prompt-file {prompt} --repo {repo}'
```

A single run is **not** statistical evidence; repeat across scenarios and
report the distribution before drawing any conclusion about one method being
better. No result here may be written as "sessionFlow agents succeed X% more".

## Scope compliance

No new provider, no real user data, no Store/Bridge schema change, no LLM/
vector DB/long-term memory system, no tag/release. Level 1 scorer untouched.
