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

`hidden_tests/test_hidden.py` and `task_manifest.json` are never inside an `agent/` root. Evaluation copies the hidden test into `private/eval/<run>_<mode>_<baseline>__eval`, not into the agent's repo. The run/mode/baseline key prevents a later comparison from overwriting an earlier evaluation directory. Tests assert path separation and that an edit to one baseline's `calc.py` does not appear in any other baseline.

### Runner policy

`--runner` no longer executes anything. It is parsed into an inspectable contract containing explicit `cwd`, an allow-listed environment and a disabled status:

- inherited variables: `PATH`, `PATHEXT`, `SYSTEMROOT`, `COMSPEC`, `OS`, `PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS`;
- isolated variables: `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, XDG roots, `TMP`/`TEMP`;
- removed: real profile variables, `SSH_AUTH_SOCK`, provider tokens, `CODEX_*`, `CLAUDE_*`, `DSH_*`, `WORKBUDDY_*`, ambient `VOYAGER_*`;
- OS-level per-baseline filesystem boundary: **absent**.

Because a shell template cannot stop one agent from reading a sibling repo or a real index, live execution is disabled (`NO_OS_SANDBOX`). Any obvious private-path string in a template is rejected only as a lint warning; it is explicitly **not** an isolation boundary and variant spellings are not treated as protection. `classify_outcome()` keeps `runner_spawn_failed`, `runner_timeout`, `runner_failed`, `evaluator_failed` and `hidden_tests_failed` separate from `success`; a non-zero or crashed runner never becomes task success.

## 3. Measurements (DRY-RUN, executed)

| Mode | no_history | raw_history | latest_summary | sessionflow_bundle | external goal | explicit thread |
|---|---:|---:|---:|---:|---|---|
| oracle_conditioned | 0 | 46 | 47 | 550 | yes | yes |
| explicit_goal_continuation | 0 | 46 | 47 | 372 | yes | no |
| realistic_work_discovery | 0 | 46 | 47 | 372 | no | no |

`sanity_hidden_test_fails_on_start = true`. Every agent outcome remains `LIVE_MODEL_NOT_VERIFIED`. These are context-construction measurements, not completion rates.

## 4. Tests

`tests/test_agent_continuity_benchmark.py` now has 19 gates covering:

- green starting repo and failing hidden test;
- three modes / four baselines and non-pooled labels;
- removal of Oracle goal/thread changes discovery signals;
- identical explicit goal across baselines and no thread id outside Oracle;
- Oracle prompt uses the baseline-local WorkThread id even when the private
  manifest id differs;
- private grader data outside every agent root;
- per-baseline repo/home independence and no cross-baseline edit pollution;
- no hidden answer in any prompt;
- DRY-RUN marks every outcome NOT VERIFIED;
- runner is disabled but its cwd/env contract is inspectable;
- obvious private grader references are rejected as lint, explicitly not as an
  isolation boundary;
- evaluation directories are unique per run id / mode / baseline;
- runner failure classes (spawn, timeout, evaluator, non-zero exit, hidden tests)
  are distinct and never become success;
- scorer verdict is independent of method/baseline labels.

Result: **19 passed**. The old v1 harness was separately reproduced as the defect table in section 1. Four post-merge mutations (baseline-local Oracle id, unique eval dir, failure labels, lint marker) each turn their matching gate red.

## 5. Unchanged boundaries

Level 1 `scripts/continuity_benchmark.py` and its historical 0/17/11/24 result are untouched. No Store or Bridge schema change, no provider added, no real session/index/home read, no model launched, no Tag or Release. Real agent effectiveness, paid-model runs and a true OS-level sandbox remain **NOT VERIFIED / NOT AVAILABLE**.

## 6. R4 post-merge correctness correction

Three v2 implementation details could still mislead a reviewer even while no model was launched:

| Finding | Red evidence | Correction |
|---|---|---|
| Oracle prompt used `manifest.correct_thread_id` from the private index, but each baseline rebuilds its own index and gets a new id. | Force all thread ids to differ: the Oracle prompt named `thr_forced_0001` while the baseline store only had `thr_forced_0003`. | `write_prompt()` receives the baseline-local id; `prepare_baseline()` verifies it exists and has two members before use. |
| Every evaluation copied to `repo__eval`, so a second mode/baseline/run could overwrite the first. | Four label combinations produced the same `repo_template__eval` path. | `evaluate_repo()` keys the copy by run id, mode and baseline; tests assert four distinct paths. |
| Private-path string checks looked like a security boundary; runner failures were collapsed into one label. | Variant private paths passed the contract while the contract claimed no live execution boundary; `classify_outcome()` had no spawn/timeout/evaluator classes. | Contract fields now say `LINT_ONLY_NOT_SECURITY_BOUNDARY` / `live_execution_supported: false`; failure classes are explicit and consumed by the evaluator verdict. |

No OS sandbox appeared and no live runner was enabled. The evidence class remains **DRY_RUN / LIVE_MODEL_NOT_VERIFIED**, not real agent success.

## 7. Independent synthetic tasks

T1 remains the historical `calc.add()` scenario and reproduces the published context sizes exactly: `0/46/47/550` in Oracle and `0/46/47/372` in the two non-Oracle modes. Two independent tasks were added without changing the Level 1 scorer:

| Task | Module | Hidden requirement | Hidden failure on start |
|---|---|---|---|
| `T2` | `textutil.normalize()` | add `keep_case=False`; lower-case by default | unexpected keyword argument |
| `T3` | `ledger.total()` | ignore negative and non-numeric amounts | hidden assertion fails |

`build_task(workdir, task_id)` selects a task spec; each spec owns its repo files, visible test, hidden test, hidden-answer key, session content, thread title and decoy. The CLI exposes `--task T1|T2|T3`; default is T1 so earlier measurements stay comparable. `run_level2()` accepts `task_id` and applies the same three modes and four baselines to the selected task.

Quality gates now have 25 checks: T2/T3 build green visible repos, fail their own hidden tests, contain no `calc.py`, and expose their task-specific hidden-answer key; T2 also runs through all three modes and four baselines in DRY-RUN. The harness still launches no model and reports no completion rate.

Runner isolation remains **BLOCKED** with the explicit probe in `docs/audits/2026-11-20-runner-isolation-gate.md`.
