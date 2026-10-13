# Archived phase reports (September 2026)

These are historical records, kept because they are the evidence behind
decisions that are still in force — not because they describe the current
state. **Do not read them as documentation of what sessionFlow does today.**

They were moved out of the repository root in P7 (architecture audit) so the
front page reads as a finished project rather than a work in progress. Nothing
was edited in the move; a report that was superseded still says so, in its own
words, exactly as it did at the root.

| File | What it is |
|---|---|
| `claude_continuity_verdict.md` | Live zero-touch verification for Claude Code. Cited by README, CHANGELOG, `docs/DOGFOOD.md` and `docs/ROADMAP.md` as the evidence for the continuity claims. |
| `grok_continuity_verdict.md` | The same for Grok, including the cross-provider leg closed on 2026-09-25. |
| `dogfood_report.md`, `dogfood_test_findings.md`, `dogfood_verification_state.md` | Dogfooding rounds. `dogfood_report.md` is a superseded snapshot and says so. |
| `DOGFOOD_INSTRUCTIONS.md` | How those rounds were run. |
| `IMPLEMENTATION_STATUS.md`, `COMPLETION_SUMMARY.md`, `SUMMARY_VERIFICATION_STATE.md`, `ZERO_TOUCH_PHASE2_SUMMARY.md` | Progress snapshots from the build-out. |
| `PROVIDER_CLASSIFICATION_REPORT.md`, `PROVIDER_CLASSIFICATION_REPORT_CONTINUED.md` | The original provider capability classification, later superseded by the canonical matrix in `voyager/capability_matrix.py`. |
| `gork_injection_design.md`, `live_continuity_test_plan.md`, `GRK_E2E_RESULTS.md` | Design notes and results from the continuity experiments. |

For the current state see the repository README, `docs/DOGFOOD.md`,
`docs/ROADMAP.md` and `docs/DECISIONS.md`. Earlier archived rounds live in
`docs/archive/sessions-2026-09-29/`.
