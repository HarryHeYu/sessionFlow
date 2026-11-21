"""Quality gates for the v2 Agent Continuation Effectiveness harness.

The harness must not fabricate a completion rate, and its context modes must
not be pooled. These tests prove that:
  * the synthetic task is genuine (green start, hidden test fails);
  * Oracle-conditioned, explicit-goal and work-discovery modes are distinct;
  * hidden grader data is outside every agent workspace;
  * baselines have independent repos/homes and cannot contaminate each other;
  * live runner execution is disabled unless an OS sandbox exists;
  * runner errors are not scored as task success, and scoring is not keyed to
    a method name.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "voyager_agent_continuity_benchmark_v2",
        REPO / "scripts" / "agent_continuity_benchmark.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def mod():
    return _mod()


@pytest.fixture(scope="session")
def scratch_root():
    base = os.environ.get("SESSIONFLOW_BENCH_TEST_TMPDIR")
    if base:
        pathlib.Path(base).mkdir(parents=True, exist_ok=True)
        path = pathlib.Path(tempfile.mkdtemp(prefix="agent-bench-v2-", dir=base))
    else:
        path = pathlib.Path(tempfile.mkdtemp(prefix="agent-bench-v2-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def result(mod, scratch_root):
    return mod.run_level2(scratch_root / "default")


@pytest.fixture(scope="module")
def runner_result(mod, scratch_root):
    return mod.run_level2(
        scratch_root / "runner",
        runner="my-agent --prompt-file {prompt} --repo {repo}")


def test_starting_repo_is_green(mod, scratch_root):
    task = mod.build_task(scratch_root / "green")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=task["repo_template"], capture_output=True, text=True)
    assert proc.returncode == 0, "the starting repo must be green: " + proc.stdout


def test_hidden_answer_is_not_in_visible_repo(result):
    repo = pathlib.Path(result["task"]["repo_template"])
    visible = (repo / "calc.py").read_text(encoding="utf-8")
    assert "clamp" not in visible, "the requirement leaked into visible code"
    hidden = pathlib.Path(result["task"]["hidden_test"]).read_text(encoding="utf-8")
    assert "assert add(1, 9, 5) == 5" in hidden


def test_hidden_test_fails_on_starting_repo(result):
    assert result["sanity_hidden_test_fails_on_start"] is True


def test_three_modes_and_four_baselines_are_explicit(result, mod):
    assert result["schema"] == "agent-continuity-benchmark-v2"
    assert set(result["modes"]) == set(mod.MODES)
    for mode in result["modes"].values():
        assert set(mode["baselines"]) == set(mod.BASELINES)
        assert mode["counted_for"]


def test_removing_oracle_input_changes_discovery_signals(result):
    oracle = result["modes"]["oracle_conditioned"]
    realistic = result["modes"]["realistic_work_discovery"]
    explicit = result["modes"]["explicit_goal_continuation"]
    for name in oracle["baselines"]:
        o = oracle["baselines"][name]
        r = realistic["baselines"][name]
        e = explicit["baselines"][name]
        assert o["external_goal_supplied"] is True
        assert o["thread_id_supplied"] is True
        assert e["external_goal_supplied"] is True
        assert e["thread_id_supplied"] is False
        assert r["external_goal_supplied"] is False
        assert r["thread_id_supplied"] is False


def test_explicit_goal_is_identical_across_all_baselines(result, mod):
    prompts = []
    for name, row in result["modes"]["explicit_goal_continuation"]["baselines"].items():
        text = pathlib.Path(row["prompt_file"]).read_text(encoding="utf-8")
        assert mod.EXTERNAL_GOAL_MARKER in text
        assert mod.THREAD_ID_MARKER not in text
        prompts.append(text.split(mod.EXTERNAL_GOAL_MARKER, 1)[1].splitlines()[0].strip())
    assert len(set(prompts)) == 1


def test_private_grader_data_is_outside_every_agent_root(result):
    private = pathlib.Path(result["isolation"]["private_grader_root"]).resolve()
    hidden = pathlib.Path(result["task"]["hidden_test"]).resolve()
    manifest = (private / "task_manifest.json").resolve()
    for mode in result["modes"].values():
        for row in mode["baselines"].values():
            agent = pathlib.Path(row["layout"]["agent_root"]).resolve()
            assert not hidden.is_relative_to(agent)
            assert not manifest.is_relative_to(agent)
            assert not private.is_relative_to(agent)


def test_baselines_are_independent_and_one_edit_does_not_pollute(result):
    layouts = [
        row["layout"]
        for mode in result["modes"].values()
        for row in mode["baselines"].values()
    ]
    assert len({x["agent_root"] for x in layouts}) == len(layouts)
    assert len({x["repo"] for x in layouts}) == len(layouts)
    assert len({x["home"] for x in layouts}) == len(layouts)
    first = pathlib.Path(layouts[0]["repo"]) / "calc.py"
    original = first.read_text(encoding="utf-8")
    first.write_text(original + "\n# baseline-local edit\n", encoding="utf-8")
    for other in layouts[1:]:
        assert "# baseline-local edit" not in (
            pathlib.Path(other["repo"]) / "calc.py").read_text(encoding="utf-8")


def test_hidden_answer_is_never_in_a_prompt(result):
    for mode in result["modes"].values():
        for name, row in mode["baselines"].items():
            text = pathlib.Path(row["prompt_file"]).read_text(encoding="utf-8")
            assert "assert add(1, 9, 5) == 5" not in text, name


def test_dry_run_marks_all_outcomes_not_verified(result, mod):
    assert result["mode"] == "DRY_RUN"
    assert result["live_model_authorised"] is False
    for mode in result["modes"].values():
        for row in mode["baselines"].values():
            for metric in mod.OUTCOME_METRICS:
                assert row["evaluation"][metric] == "LIVE_MODEL_NOT_VERIFIED"
            assert row["runner"]["status"] == "NOT_EXECUTED"


@pytest.mark.parametrize("task_id", ["T1", "T2", "T3"])
def test_hidden_answer_key_matches_the_task(mod, scratch_root, task_id):
    task = mod.build_task(scratch_root / ("answer-key-" + task_id),
                          task_id=task_id)
    hidden = pathlib.Path(task["hidden_test"]).read_text(encoding="utf-8")
    visible = "".join(mod.TASK_SPECS[task_id]["files"].values())
    assert task["hidden_answer_line"] in hidden, task_id
    assert task["hidden_answer_line"] not in visible, task_id


@pytest.mark.parametrize("task_id", ["T2", "T3"])
def test_independent_synthetic_tasks_build_and_fail_hidden(mod, scratch_root, task_id):
    task = mod.build_task(scratch_root / ("independent-" + task_id),
                          task_id=task_id)
    assert task["task_id"] == task_id
    repo = pathlib.Path(task["repo_template"])
    assert not (repo / "calc.py").exists(), task_id
    visible = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=repo, capture_output=True, text=True)
    assert visible.returncode == 0, visible.stdout

    hidden = pathlib.Path(task["hidden_test"])
    hidden_text = hidden.read_text(encoding="utf-8")
    assert "assert add(1, 9, 5)" not in hidden_text, task_id
    eval_root = pathlib.Path(task["private_root"]) / "eval"
    eval_root.mkdir(parents=True, exist_ok=True)
    verdict = mod.evaluate_repo(
        repo, hidden, sys.executable, eval_root,
        mode="task-sanity", baseline=task_id, run_id="build")
    assert verdict["independent_tests_pass"] is False, task_id


def test_run_level2_accepts_an_independent_task(mod, scratch_root):
    result = mod.run_level2(scratch_root / "independent-t2-level2", task_id="T2")
    assert result["task"]["task_id"] == "T2"
    assert result["sanity_hidden_test_fails_on_start"] is True
    assert set(result["modes"]) == set(mod.MODES)
    for mode in result["modes"].values():
        assert set(mode["baselines"]) == set(mod.BASELINES)


def test_live_runner_is_disabled_but_contract_is_inspectable(runner_result):
    runner = runner_result["runner"]
    assert runner["status"] == "DISABLED"
    assert runner["reason"] == "NO_OS_SANDBOX"
    assert runner["cwd"].endswith("repo")
    assert "HOME" in runner["env_keys"]
    assert "SSH_AUTH_SOCK" not in runner["env_keys"]
    assert runner_result["live_model_authorised"] is False
    for mode in runner_result["modes"].values():
        for row in mode["baselines"].values():
            assert row["evaluation"]["independent_tests_pass"] == "LIVE_MODEL_NOT_VERIFIED"


def test_runner_template_cannot_reference_private_data(mod, result):
    layout = result["modes"]["oracle_conditioned"]["baselines"]["no_history"]["layout"]
    private = result["isolation"]["private_grader_root"]
    with pytest.raises(mod.BenchmarkContractError):
        mod.runner_contract("cat {prompt} {repo} " + private, layout,
                            pathlib.Path(private).parent)


def test_runner_error_is_not_task_success(mod):
    failed = mod.classify_outcome(2, {"independent_tests_pass": True})
    assert failed["independent_tests_pass"] is False
    assert failed["failure_type"] == "runner_failed"
    tests_failed = mod.classify_outcome(0, {"independent_tests_pass": False})
    assert tests_failed["failure_type"] == "hidden_tests_failed"


def _forced_thread_ids(mod, monkeypatch):
    """Force every generated WorkThread id to differ across stores."""
    counter = {"n": 0}

    def create(self, repo_root=None, title=None, goal=None):
        counter["n"] += 1
        tid = "thr_forced_%04d" % counter["n"]
        now = time.time()
        self.con.execute(
            "INSERT INTO threads(id, repo_root, title, goal, created_at,"
            " updated_at, status) VALUES (?,?,?,?,?,?,'active')",
            (tid, repo_root, title, goal, now, now))
        self.con.commit()
        return tid

    monkeypatch.setattr(mod.Store, "thread_create", create)
    return counter


def test_oracle_prompt_uses_baseline_local_thread_id(mod, monkeypatch, scratch_root):
    _forced_thread_ids(mod, monkeypatch)
    result = mod.run_level2(scratch_root / "forced-thread-ids")
    private_id = result["task"]["correct_thread_id"]

    oracle = result["modes"]["oracle_conditioned"]
    for name, row in oracle["baselines"].items():
        local_id = row["layout"]["correct_thread_id"]
        assert local_id != private_id, name
        text = pathlib.Path(row["prompt_file"]).read_text(encoding="utf-8")
        assert mod.THREAD_ID_MARKER + " " + local_id in text, name
        assert mod.THREAD_ID_MARKER + " " + private_id not in text, name
        store = mod.Store(pathlib.Path(row["layout"]["index_db"]))
        try:
            thread = store.thread_get(local_id)
            assert thread is not None and thread["id"] == local_id, name
            assert len(store.thread_members(local_id)) == 2, name
        finally:
            store.close()

    for mode in ("explicit_goal_continuation", "realistic_work_discovery"):
        for name, row in result["modes"][mode]["baselines"].items():
            text = pathlib.Path(row["prompt_file"]).read_text(encoding="utf-8")
            assert private_id not in text, (mode, name)
            assert row["layout"]["correct_thread_id"] not in text, (mode, name)


def test_eval_repos_are_unique_per_mode_baseline_and_run(mod, scratch_root):
    task = mod.build_task(scratch_root / "eval-directory-names")
    hidden = pathlib.Path(task["hidden_test"])
    eval_root = pathlib.Path(task["private_root"]) / "eval"
    eval_root.mkdir(parents=True, exist_ok=True)
    repo = pathlib.Path(task["repo_template"])

    combos = [
        ("oracle_conditioned", "no_history", "run-1"),
        ("oracle_conditioned", "no_history", "run-2"),
        ("explicit_goal_continuation", "no_history", "run-1"),
        ("realistic_work_discovery", "sessionflow_bundle", "run-1"),
    ]
    seen = set()
    for mode, baseline, run_id in combos:
        verdict = mod.evaluate_repo(
            repo, hidden, sys.executable, eval_root,
            mode=mode, baseline=baseline, run_id=run_id)
        eval_repo = pathlib.Path(verdict["eval_repo"]).resolve()
        assert eval_repo not in seen, (mode, baseline, run_id)
        seen.add(eval_repo)
        for token in (mode, baseline, run_id):
            assert token in eval_repo.name
        assert (eval_repo / "tests" / "test_hidden.py").exists()
        assert verdict["independent_tests_pass"] is False


def test_private_path_string_check_is_lint_only(mod, result):
    layout = result["modes"]["oracle_conditioned"]["baselines"]["no_history"]["layout"]
    private = result["isolation"]["private_grader_root"]
    # A different spelling must not be mistaken for an isolation guarantee.
    # On Windows the case differs; on POSIX the separators differ.
    variant = private.replace("\\", "/").replace("/", "\\").lower()
    contract = mod.runner_contract(
        "my-agent --private-root '" + variant + "' --prompt-file {prompt} --repo {repo}",
        layout, pathlib.Path(private).parent)
    assert contract["status"] == "DISABLED"
    assert contract["reason"] == "NO_OS_SANDBOX"
    assert contract["live_execution_supported"] is False
    assert contract["private_path_check"] == "LINT_ONLY_NOT_SECURITY_BOUNDARY"


def test_runner_failure_contract_distinguishes_sources(mod):
    cases = [
        (("completed", 0, {"independent_tests_pass": True}),
         "success", True),
        (("completed", 0, {"independent_tests_pass": False}),
         "hidden_tests_failed", False),
        (("completed", 2, {"independent_tests_pass": True}),
         "runner_failed", False),
        (("spawn_failed", None, {}),
         "runner_spawn_failed", False),
        (("timeout", None, {}),
         "runner_timeout", False),
        (("evaluator_failed", None, {}),
         "evaluator_failed", False),
        (("completed", 0, {"evaluator_failed": True,
                           "independent_tests_pass": False}),
         "evaluator_failed", False),
    ]
    for args, failure_type, passed in cases:
        verdict = mod.classify_outcome(args[1], args[2], runner_status=args[0])
        assert verdict["failure_type"] == failure_type, args
        assert verdict["independent_tests_pass"] is passed, args
        assert verdict["final_code_meets_goal"] is passed, args
        assert verdict["runner_status"] == args[0], args


def test_scorer_does_not_depend_on_method_name(mod, scratch_root):
    task = mod.build_task(scratch_root / "method-name-independence")
    hidden = pathlib.Path(task["hidden_test"])
    eval_root = pathlib.Path(task["private_root"]) / "eval"
    eval_root.mkdir(parents=True, exist_ok=True)
    repo = pathlib.Path(task["repo_template"])
    verdicts = [
        mod.evaluate_repo(
            repo, hidden, sys.executable, eval_root,
            mode=mode, baseline=baseline, run_id=run_id)["independent_tests_pass"]
        for mode, baseline, run_id in (
            ("oracle_conditioned", "no_history", "run-1"),
            ("explicit_goal_continuation", "sessionflow_bundle", "run-2"),
            ("realistic_work_discovery", "raw_history", "run-3"),
        )
    ]
    assert len(set(verdicts)) == 1


def test_no_llm_no_real_data_declaration(result):
    assert result["no_llm_in_harness"] is True
    assert result["no_real_user_data"] is True
    assert result["isolation"]["os_sandbox"] is False
    assert result["isolation"]["live_runner_disabled"] is True


# ---------------------------------------------------------------------------
# R5 ground-truth and verdict-provenance gates.
# ---------------------------------------------------------------------------

def _evaluate_with_source(mod, scratch_root, name, task_id, source_files):
    """Evaluate a candidate implementation against one task's hidden test."""
    task = mod.build_task(scratch_root / name, task_id=task_id)
    repo = pathlib.Path(task["repo_template"])
    for rel, text in source_files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    eval_root = pathlib.Path(task["private_root"]) / "eval"
    eval_root.mkdir(parents=True, exist_ok=True)
    visible = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_visible.py"],
        cwd=str(repo), capture_output=True, text=True)
    verdict = mod.evaluate_repo(
        repo, pathlib.Path(task["hidden_test"]), sys.executable, eval_root,
        mode="ground-truth", baseline=task_id, run_id=name)
    return task, verdict, visible.returncode


def test_t2_requirement_and_visible_test_are_consistent(mod, scratch_root):
    task = mod.build_task(scratch_root / "t2-consistency", task_id="T2")
    hidden = pathlib.Path(task["hidden_test"]).read_text(encoding="utf-8")
    visible = mod.TASK_SPECS["T2"]["files"]["tests/test_visible.py"]
    assert "assert normalize('  Hi ') == 'Hi'" in visible
    assert "assert normalize('  Hi ') == 'Hi'" in hidden
    assert "assert normalize('  Hi ', keep_case=False) == 'hi'" in hidden
    assert "keep_case, default True" in task["required_fact"]


def test_t2_noop_parameter_cannot_pass(mod, scratch_root):
    noop = "def normalize(value, keep_case=True):\n    return value.strip()\n"
    _, verdict, visible_rc = _evaluate_with_source(
        mod, scratch_root, "t2-noop", "T2", {"textutil.py": noop})
    assert visible_rc == 0
    assert verdict["independent_tests_pass"] is False


def test_t2_real_implementation_passes_both_suites(mod, scratch_root):
    impl = ("def normalize(value, keep_case=True):\n"
            "    stripped = value.strip()\n"
            "    return stripped if keep_case else stripped.lower()\n")
    task, verdict, visible_rc = _evaluate_with_source(
        mod, scratch_root, "t2-real", "T2", {"textutil.py": impl})
    hidden = pathlib.Path(task["hidden_test"]).read_text(encoding="utf-8")
    assert task["hidden_answer_line"] in hidden
    assert visible_rc == 0
    assert verdict["independent_tests_pass"] is True
    assert verdict["failure_type"] == "success"


def test_t1_hidden_test_enforces_default_clamp(mod, scratch_root):
    task = mod.build_task(scratch_root / "t1-default", task_id="T1")
    hidden = pathlib.Path(task["hidden_test"]).read_text(encoding="utf-8")
    assert "assert add(8, 9) == 10" in hidden
    wrong = "def add(a, b, clamp=5):\n    return min(a + b, clamp)\n"
    _, verdict, visible_rc = _evaluate_with_source(
        mod, scratch_root, "t1-wrong-default", "T1", {"calc.py": wrong})
    assert visible_rc == 0
    assert verdict["independent_tests_pass"] is False


def test_evaluator_does_not_report_pytest_code_as_runner_code(mod, scratch_root):
    task = mod.build_task(scratch_root / "verdict-provenance")
    hidden = pathlib.Path(task["hidden_test"])
    eval_root = pathlib.Path(task["private_root"]) / "eval"
    eval_root.mkdir(parents=True, exist_ok=True)
    verdict = mod.evaluate_repo(
        pathlib.Path(task["repo_template"]), hidden, sys.executable, eval_root,
        mode="verdict", baseline="T1", run_id="provenance")
    assert verdict["independent_tests_pass"] is False
    assert verdict["failure_type"] == "hidden_tests_failed"
    assert verdict["runner_returncode"] is None
    assert verdict["runner_status"] == "NOT_EXECUTED"
    assert verdict["evaluator_returncode"] == 1


def test_classify_outcome_keeps_runner_and_evaluator_codes_apart(mod):
    verdict = mod.classify_outcome(
        0, {"independent_tests_pass": True, "evaluator_returncode": 1})
    assert verdict["failure_type"] == "success"
    assert verdict["runner_returncode"] == 0
    assert verdict["evaluator_returncode"] == 1
