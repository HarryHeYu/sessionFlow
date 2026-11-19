# Reproducible continuity benchmark — 2026-11-19

> **Status: reproducible harness, not an agent-success claim.** No LLM, no real
> session, no live index. This promotes the 2026-11-18 one-off experiment into
> the repository so another developer can re-run it on their own machine.

## 1. What changed from the scratch harness

The [one-off report](2026-11-18-continuity-effectiveness-benchmark.md) used
`E:/sessionflow-scratch/...` and hardcoded `E:/code/voyager`. It measured one
number — required-fact hits — over eight scenarios. The promoted version:

| Property | Scratch | In-repo |
|---|---|---|
| Location | scratch dir, absolute paths | `scripts/continuity_benchmark.py`, repo-relative import |
| Data dir | fixed E: path | caller-supplied `--workdir` or a fresh auto-cleaned temp dir |
| Metrics | required hits + token count | six declared metrics (below) |
| Baselines | four | four + the same four under an **equal 4000-token budget** |
| Scenarios | A–H | A–H + independent hold-out **I** |
| Quality gate | none | `tests/test_continuity_benchmark.py` (11 gates, mutation-checked) |
| Reproducibility | not stated | records `core_commit`, `python`, `schema` in JSON |

Run:
```bash
python scripts/continuity_benchmark.py --out result.json
python -m pytest tests/test_continuity_benchmark.py -q
```

## 2. The six metrics (all normalised to 0.0–1.0)

1. **Semantic Fact Recall** — declared `required` strings present in the text.
2. **Source Attribution** — provider / session-id / `SOURCE_MISSING` markers.
3. **Contradiction Preservation** — `keep both readings` style markers where the
   scenario has a contradiction.
4. **State / Ambiguity Correctness** — `thread_ambiguous` / `thread_candidates`
   / `thread_default_reason` / `SOURCE_MISSING` where the scenario calls for it.
5. **Budget Compliance** — 1.0 if the text is under the token ceiling, 0.0 if
   over. Evaluated both with each scenario's native budget and with a shared
   4000-token equal-budget pass.
6. **False Claim Rate** — `forbidden` strings (facts never in any source) that
   appear in the text. Core's own section labels are **not** counted as answers.

Metrics 2–4 return 1.0 on a scenario that has nothing to attribute / preserve /
disambiguate. That is deliberate: a baseline must not be penalised for not
saying "keep both readings" when no contradiction exists. It also means the
headline comparison must be **Semantic Fact Recall**, not the mean of all six.

## 3. Reproducing the historical result

Run on Core `9d5c0fc`, Python 3.13.12. A–H required-hit totals (legacy metric,
hold-out I excluded):

| Baseline | A–H required hits |
|---|---:|
| no_history | 0 / 24 |
| raw_history | 17 / 24 |
| latest_summary | 11 / 24 |
| sessionflow_bundle | **24 / 24** |

The published 0/17/11/24 is exactly reproducible. Full A–I totals: raw 19/26,
latest 11/26, Bundle 26/26 (2 of the 26 facts come from the hold-out).

A–H mean across the six metrics (native budget):

| Baseline | recall | source | contra | state | budget | false |
|---|---:|---:|---:|---:|---:|---:|
| no_history | 0.000 | 0.125 | 0.750 | 0.750 | 1.000 | 0.000 |
| raw_history | 0.729 | 0.125 | 0.750 | 0.750 | 1.000 | 0.000 |
| latest_summary | 0.479 | 0.875 | 0.750 | 0.750 | 1.000 | 0.000 |
| sessionflow_bundle | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 |

Interpretation: Bundle's advantage comes from **structure** (source list,
cross-session conclusions, ambiguity fields, evidence labels), not from fewer
tokens — mean Bundle size (3079) is larger than raw (1909). Under the shared
4000-token pass no baseline exceeded the ceiling on these scenarios, so equal
budget did not change the ranking; the equal-budget columns exist to catch a
future method that wins only by emitting more text.

Scenario G is the discriminator for metric 4: Bundle scores 1.0; all three
baselines score 0.0, because only the Core's `current_work()` bridge exposes
`thread_ambiguous` / `thread_candidates` / `thread_default_reason`.

## 4. Independent hold-out and an honest negative result

Scenario I (TLS-proxy error + unrelated logging refactor) was not part of the
A–H design. All of Bundle's required facts were preserved (2/2), **but** the
`## Goal-ranked evidence` top-3 put the `NOISE_I` line in and pushed the goal
fact out. A manual probe confirmed the ranker is goal-responsive (with goal
"fix CI pipeline" the CI facts lead; with "UI color palette" the noise leads),
but on I the command-shaped/noisy evidence still wins a slot.

This is recorded as a **finding to investigate**, not silently patched. It is
the concrete input to Phase 4's command-relevance down-weighting experiment.
Changing the ranking rule is a separate, evidence-gated decision — this file
only makes the failure reproducible.

## 5. Quality gates on the scorer

`tests/test_continuity_benchmark.py` (11 tests) asserts the benchmark can
actually measure:

- editing the answer key lowers recall;
- deleting a required fact lowers recall;
- inserting 500 noise sentences never raises a quality metric;
- a fabricated fact raises False Claim Rate, while Core's template labels do not;
- oversized text fails Budget Compliance and short text passes;
- the scorer takes no method-name argument (a relabel cannot change a verdict);
- the historical 0/17/11/24 and the hold-out I are present in the static run;
- the runner source contains no developer-machine path literals.

Mutation check: forcing `required_hits = required_total` (simulating a scorer
that hands out full marks) turned `test_historical_ah_totals_reproduce`,
`test_mutating_the_answer_key_changes_the_score`, and
`test_deleting_key_evidence_lowers_recall` red; restoring the real scorer made
all 11 pass. The gate tests the measurement, not whether sessionFlow wins.

## 6. Fairness limits (do not overstate)

- **No agent success.** This measures context *preparation*: string presence,
  structure, budget. It cannot show an agent solved the task faster or more
  correctly. That needs real sessions + real models + authorisation.
- **MATCH ≠ truth.** A preserved string is still only a claim from a source.
- **n/a metrics are generous.** Metrics 2–4 return 1.0 when a scenario has no
  applicable marker; read the per-metric means with that in mind.
- **Equal budget is a ceiling, not a normaliser.** The four baselines are not
  truncated to the same token count on the native pass; the shared 4000-token
  pass scores them under one ceiling but does not rewrite their text.
- **Synthetic only.** No real user session, index, or home directory was read
  or written. All paths derive from the caller-supplied workdir.

No Store schema, Bridge schema, or scoring behaviour of the shipped product was
changed by this work.
