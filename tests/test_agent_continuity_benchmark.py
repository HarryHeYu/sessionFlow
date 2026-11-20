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
import inspect
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

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
    assert failed["failure_type"] == "runner_error"
    tests_failed = mod.classify_outcome(0, {"independent_tests_pass": False})
    assert tests_failed["failure_type"] == "tests_failed"


def test_scorer_does_not_take_a_method_name(mod):
    params = inspect.signature(mod.evaluate_repo).parameters
    assert "baseline" not in params and "mode" not in params and "name" not in params


def test_no_llm_no_real_data_declaration(result):
    assert result["no_llm_in_harness"] is True
    assert result["no_real_user_data"] is True
    assert result["isolation"]["os_sandbox"] is False
    assert result["isolation"]["live_runner_disabled"] is True
