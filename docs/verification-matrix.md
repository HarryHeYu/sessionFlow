# Canonical verification matrix

This page is the canonical source for verification claims in Core. Other
documents may describe a feature, but they must not silently upgrade a claim
beyond the boundary recorded here. A claim is always tied to its evidence date
and evidence class; "historical live verified" is not the same as "re-verified
in the current round", and "BLOCKED in one round" is not the same as "never
verified anywhere".

## Evidence classes

| Class | Meaning |
|---|---|
| `UNIT` | hermetic test; no real host behaviour |
| `INTEGRATION` | real components in a synthetic/isolated environment |
| `LIVE-HISTORICAL` | real host/model/session evidence from a dated past run |
| `LIVE-R16` | real process or real Agent evidence from the 2026-11-24 R16 round |
| `LIVE-R17` | real evidence from the 2026-11-25 R17 round |
| `INSTALL-ONLY` | install/discovery succeeded; launch, activation or model use did not |
| `BLOCKED` | environment, credential or user constraint stopped the experiment |
| `NOT VERIFIED` | no sufficient evidence; implementation existence does not promote it |

## Capability matrix

| Capability | Highest defensible claim | Evidence class | Boundary |
|---|---|---|---|
| Local multi-provider index and cross-agent timeline | LIVE VERIFIED (historical) | `LIVE-HISTORICAL` + `UNIT` | Historical live index evidence exists. R17 did not re-run a real user index. |
| WorkThread / merge / continue work continuity | LIVE-R16 VERIFIED | `INTEGRATION` + `LIVE-R16` | Real Core CLI and real WorkBuddy subagent A→B and A+B→C. Not cross-vendor native provider E2E. |
| Ambiguous WorkThread write safety | LIVE-R16 VERIFIED for Core CLI/API; DSH Agent path BLOCKED | `INTEGRATION` + `LIVE-R16` + `BLOCKED` | Ambiguous preview has `thread_ambiguous` and candidates, no `selected_thread`; ambiguous continue exits 2 with no side effects. |
| Claude Code / Grok / Codex startup hook zero-touch | LIVE VERIFIED (2026-09-24/25/28) | `LIVE-HISTORICAL` | Dated historical runs. The CLI static letter remains `H`; that is a state-expression design, not a refutation. |
| ZCode / Cursor / Kiro startup hook | UNIT VERIFIED; live provider firing NOT VERIFIED | `UNIT` | Handler driven with a real payload; no provider-fired run claimed. |
| Evidence Verify five-state API | VERIFIED (historical) | `INTEGRATION` + `LIVE-HISTORICAL` | `MATCH` is reference consistency, not claim truth. |
| Native Resume | NOT VERIFIED end to end | `NOT VERIFIED` + `BLOCKED` | Commands exist in adapters/help. R16 and R17 did not run authenticated sessions. See [native resume manual verification](native-resume-manual-verification.md). |
| VSIX package and isolated install | LIVE-R16 VERIFIED | `LIVE-R16` + `INSTALL-ONLY` | `@vscode/vsce` package and isolated `code --install-extension` succeeded. |
| Extension Host activation / selector | PARTIAL (R16), full E2E NOT VERIFIED | `PARTIAL` / `BLOCKED` | Real host observation reached the Voyager icon, WorkThreads tree and selector iframe; full case matrix and internal UI interaction were not completed. GUI was stopped at the user's request. |
| One-click switch | Implemented; real UI E2E NOT VERIFIED | `BLOCKED` | No real click and resulting thread ID were captured. |
| Reproducible continuity benchmarks | UNIT/INTEGRATION VERIFIED; live model completion NOT VERIFIED | `UNIT` + `INTEGRATION` | Harnesses are research tools. No live model completion rate is claimed. |
| Causal continuity with hidden information | LIVE-R17 VERIFIED for E1/E2; E3 partial | `LIVE-R17` | Three falsifiable tasks with CONTROL/SESSIONFLOW. CONTROL 0/3, sessionFlow 2/3 complete. See [causal continuity benchmark](causal-continuity-benchmark.md). |

## Rules when editing other documents

1. Keep historical dates when quoting historical live evidence.
2. Do not turn `BLOCKED THIS RUN` into global `NOT VERIFIED` when dated historical evidence exists.
3. Do not turn implementation existence or unit tests into `LIVE VERIFIED`.
4. Do not turn R16-only evidence into a current public claim without its `LIVE-R16` label.
5. When this matrix and a feature document disagree, update the feature document.
