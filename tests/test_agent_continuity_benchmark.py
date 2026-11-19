"""Quality gates for the Level 2 Agent Continuation Effectiveness harness.

The harness must never fabricate a completion rate. These tests prove:
  * the synthetic task is genuine (starting repo is green, hidden test fails —
    the requirement lives only in prior sessions, not in visible code);
  * the four context variants are built and the equal budget is applied;
  * DRY-RUN marks EVERY agent-outcome metric LIVE_MODEL_NOT_VERIFIED;
  * no real user data / LLM / vector store is involved.

No model is launched here. LIVE MODEL remains NOT VERIFIED by design.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "voyager_agent_continuity_benchmark",
        REPO / "scripts" / "agent_continuity_benchmark.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def mod():
    return _mod()


@pytest.fixture(scope="module")
def result(mod, tmp_path_factory):
    wd = tmp_path_factory.mktemp("agent-bench")
    return mod.run_level2(wd)


def test_starting_repo_is_green(mod, tmp_path):
    m = mod.build_task(tmp_path / "task")
    import subprocess
    import sys
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=m["repo"], capture_output=True, text=True)
    assert proc.returncode == 0, "the starting repo must be green: " + proc.stdout


def test_hidden_answer_is_not_in_visible_repo(result):
    import pathlib
    repo = pathlib.Path(result["task"]["repo"])
    hidden = pathlib.Path(result["task"]["hidden_test"]).read_text(encoding="utf-8")
    # The visible code never contains the hidden assertion's distinctive line.
    visible = (repo / "calc.py").read_text(encoding="utf-8")
    assert "clamp" not in visible, "the requirement leaked into visible code"
    assert "test_add_three_args" in hidden or "clamp" in hidden


def test_hidden_test_fails_on_starting_repo(result):
    assert result["sanity_hidden_test_fails_on_start"] is True, (
        "if the hidden test passes on the starting repo, the task is not real — "
        "the requirement would be inferable from visible code")


def test_four_context_variants_and_equal_budget(mod, tmp_path):
    m = mod.build_task(tmp_path / "task")
    ctx = mod._build_contexts(tmp_path / "task", m)
    assert set(ctx) == set(mod.BASELINES)
    assert ctx["no_history"] == ""
    assert "clamp" in ctx["sessionflow_bundle"], "bundle must carry the goal"
    from voyager.budget import estimate_tokens
    assert estimate_tokens(ctx["sessionflow_bundle"]) <= mod.DEFAULT_BUDGET


def test_dry_run_marks_all_outcomes_not_verified(result, mod):
    assert result["mode"] == "DRY_RUN"
    assert result["live_model_authorised"] is False
    for name, row in result["baselines"].items():
        for metric in mod.OUTCOME_METRICS:
            assert row["evaluation"][metric] == "LIVE_MODEL_NOT_VERIFIED", (
                name, metric, row["evaluation"][metric])
        assert row["context_tokens"] >= 0


def test_no_llm_no_real_data_declaration(result):
    assert result["no_llm_in_harness"] is True
    assert result["no_real_user_data"] is True


def test_manifest_never_embeds_the_answer_as_a_prompt(result):
    # The prompt files must not contain the hidden success condition; they may
    # contain the goal (which is in the prior session) but not the test code.
    for name, row in result["baselines"].items():
        prompt = pathlib.Path(row["prompt_file"]).read_text(encoding="utf-8")
        assert "test_add_three_args" not in prompt, (
            "the hidden test leaked into a prompt: " + name)
