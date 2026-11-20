# Level 2 benchmark fairness correction — 2026-11-20

> **Verdict: the original Level 2 harness could produce a misleading pass.** It mixed Oracle-conditioned context quality with work discovery, kept grader data in the same work tree as the agent repo, and would launch an arbitrary runner without a filesystem sandbox. The corrected harness is **DRY-RUN only**. No LIVE MODEL result exists or is implied.

## 1. Reproduced defects in the merged v1 harness

Run against `35139ec` with a stubbed `subprocess.run` (no model launched):

| Observation | Value |
|---|---|
| schema | `agent-continuity-benchmark-v1` |
| explicit evaluation modes | absent |
| hidden test under the shared workdir | `true` |
| `task_manifest.json` under the shared workdir | `true` |
| Oracle goal in Bundle prompt | `true` |
| `manifest.correct_thread_id` present | `true` |
| subprocess calls with `--runner` | 10 |

The ten calls comprise the sanity evaluation, four baseline evaluations and four runner invocations plus one extra check. This proves the old CLI did execute a runner, despite the report's LIVE MODEL boundary language. More importantly, the Bundle received `manifest["goal"]` and `manifest["correct_thread_id"]` directly while other baselines did not, and the hidden test/manifest sat in the same work tree an agent could traverse. A pass in that setup could not be interpreted as autonomous work discovery.

## 2. Corrected v2 contract

### Three non-pooled modes

| Mode | External goal | Explicit thread | Counted for |
|---|---|---|---|
| `oracle_conditioned` | same for all four baselines | same for all four baselines | context quality only |
| `explicit_goal_continuation` | same for all four baselines | none | explicit-goal continuation |
| `realistic_work_discovery` | none | none | work discovery |

In v2 the Bundle is built with `goal=None, thread=None` outside Oracle mode. Historical session text may still contain a goal; that is evidence the agent discovers, not an injected answer key. Oracle-conditioned results are explicitly marked and cannot be added to a discovery score.

### Isolation layout

For each mode and baseline the harness creates:

```text
workdir/
  private/                 # grader-only: hidden test, manifest, index, eval copies
  baselines/<mode>/<name>/agent/
    repo/                  # independent copy from one template
    prompt_<name>.txt      # mode-specific, identical external information
    home/.voyager/index.db # synthetic index scoped to this repo
    tmp/
```

`hidden_tests/test_hidden.py` and `task_manifest.json` are never inside an `agent/` root. Evaluation copies the hidden test into `private/eval/<repo>__eval`, not into the agent's repo. Tests assert path separation and that an edit to one baseline's `calc.py` does not appear in any other baseline.

### Runner policy

`--runner` no longer executes anything. It is parsed into an inspectable contract containing explicit `cwd`, an allow-listed environment and a disabled status:

- inherited variables: `PATH`, `PATHEXT`, `SYSTEMROOT`, `COMSPEC`, `OS`, `PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS`;
- isolated variables: `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, XDG roots, `TMP`/`TEMP`;
- removed: real profile variables, `SSH_AUTH_SOCK`, provider tokens, `CODEX_*`, `CLAUDE_*`, `DSH_*`, `WORKBUDDY_*`, ambient `VOYAGER_*`;
- OS-level per-baseline filesystem boundary: **absent**.

Because a shell template cannot stop one agent from reading a sibling repo or a real index, live execution is disabled (`NO_OS_SANDBOX`). `classify_outcome()` keeps `runner_error` separate from `tests_failed`; a non-zero runner never becomes task success.

## 3. Measurements (DRY-RUN, executed)

| Mode | no_history | raw_history | latest_summary | sessionflow_bundle | external goal | explicit thread |
|---|---:|---:|---:|---:|---|---|
| oracle_conditioned | 0 | 46 | 47 | 550 | yes | yes |
| explicit_goal_continuation | 0 | 46 | 47 | 372 | yes | no |
| realistic_work_discovery | 0 | 46 | 47 | 372 | no | no |

`sanity_hidden_test_fails_on_start = true`. Every agent outcome remains `LIVE_MODEL_NOT_VERIFIED`. These are context-construction measurements, not completion rates.

## 4. Tests

`tests/test_agent_continuity_benchmark.py` now has 15 gates covering:

- green starting repo and failing hidden test;
- three modes / four baselines and non-pooled labels;
- removal of Oracle goal/thread changes discovery signals;
- identical explicit goal across baselines and no thread id outside Oracle;
- private grader data outside every agent root;
- per-baseline repo/home independence and no cross-baseline edit pollution;
- no hidden answer in any prompt;
- DRY-RUN marks every outcome NOT VERIFIED;
- runner is disabled but its cwd/env contract is inspectable;
- runner templates cannot name private grader data;
- runner errors are not success;
- scorer signature has no method/baseline name.

Result: **15 passed**. The old v1 harness was separately reproduced as the defect table in section 1.

## 5. Unchanged boundaries

Level 1 `scripts/continuity_benchmark.py` and its historical 0/17/11/24 result are untouched. No Store or Bridge schema change, no provider added, no real session/index/home read, no model launched, no Tag or Release. Real agent effectiveness, paid-model runs and a true OS-level sandbox remain **NOT VERIFIED / NOT AVAILABLE**.
