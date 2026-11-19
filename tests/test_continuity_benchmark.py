"""Quality gates for the reproducible continuity benchmark.

A benchmark whose score cannot be moved is not a benchmark. These tests are
the *audit of the scorer*: they mutate the answer key, delete evidence, add
noise, swap the goal, and relabel a method, and assert the verdict responds
exactly as declared. They do NOT assert that sessionFlow wins — a scorer that
always reported "Bundle best" would fail here.

The runner itself (`scripts/continuity_benchmark.py`) is pure and synthetic:
it builds every session in a pytest-managed temp dir, reads no real index, and
records the Core commit that produced the numbers.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from typing import Any, Dict

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _runner():
    spec = importlib.util.spec_from_file_location(
        "voyager_continuity_benchmark", REPO / "scripts" / "continuity_benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bench():
    return _runner()


@pytest.fixture(scope="module")
def result(bench, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("continuity-bench")
    return bench.run_benchmark(workdir)


def _scenario(bench, sid="A"):
    return next(s for s in bench.SCENARIOS if s["id"] == sid)


# --- reproducibility --------------------------------------------------------

def test_historical_ah_totals_reproduce(result):
    """The 2026-11-18 report's A-H required-hit counts must survive promotion.

    Scenario I is the later hold-out and is excluded from this legacy total on
    purpose — mixing it in would silently change what "24/24" means.
    """
    ah = [r for r in result["scenarios"] if r["id"] in "ABCDEFGH"]
    assert len(ah) == 8
    totals = {name: sum(r["baselines"][name]["metrics"]["_raw"]["required_hits"]
                        for r in ah)
              for name in ("no_history", "raw_history", "latest_summary",
                           "sessionflow_bundle")}
    assert totals == {
        "no_history": 0, "raw_history": 17,
        "latest_summary": 11, "sessionflow_bundle": 24,
    }, "the published 0/17/11/24 result is not reproducible from this runner"


def test_result_records_core_commit_and_all_metrics(result):
    assert len(result["core_commit"]) in (7, 40), (
        "the result must name the Core commit that produced it")
    assert result["metrics"] == [
        "semantic_fact_recall", "source_attribution",
        "contradiction_preservation", "state_ambiguity_correctness",
        "budget_compliance", "false_claim_rate",
    ]
    for r in result["scenarios"]:
        for name in ("no_history", "raw_history", "latest_summary",
                     "sessionflow_bundle"):
            m = r["baselines"][name]["metrics"]
            for metric in result["metrics"]:
                assert 0.0 <= m[metric] <= 1.0, (r["id"], name, metric, m[metric])


def test_runner_has_no_developer_machine_paths(bench):
    """The scratch harness hardcoded E:/ paths; the promoted one must not.

    The module docstring legitimately mentions the old path when explaining
    *why* this file exists, so check executable lines only — a hardcoded path
    in a comment cannot write to the developer's machine.
    """
    source = (REPO / "scripts" / "continuity_benchmark.py").read_text(encoding="utf-8")
    code_lines = [ln for ln in source.splitlines()
                  if not ln.lstrip().startswith("#")]
    body = "\n".join(code_lines)
    # A hardcoded absolute path would appear as a Path("E:/...") or an
    # assignment to a string literal; docstring prose never has that shape.
    for marker in ('Path(r"E:', 'Path("E:', 'r"E:\\', '"E:\\',
                   'E:/sessionflow-scratch', 'E:/code/voyager"'):
        assert marker not in body, "developer path literal %r in code" % marker


def test_independent_holdout_is_present_and_untouched_by_ah_design(result):
    holdout = [r for r in result["scenarios"] if r["id"] == "I"]
    assert len(holdout) == 1, "the independent sample must be in the static run"
    sc = holdout[0]
    assert sc["baselines"]["sessionflow_bundle"]["metrics"]["_raw"]["required_hits"] == 2


# --- the scorer responds to reality ----------------------------------------

def test_mutating_the_answer_key_changes_the_score(bench):
    """Change a correct answer and the related metric must move."""
    sc = _scenario(bench, "A")
    text = "GOAL_A fix CI NEXT_A run pytest pytest -q FAIL_A test_bench failed"
    good = bench.score_text(text, sc)
    assert good["semantic_fact_recall"] == 1.0

    mutated = dict(sc)
    mutated["required"] = [r for r in sc["required"]] + ["NEVER_PRESENT_FACT"]
    bad = bench.score_text(text, mutated)
    assert bad["semantic_fact_recall"] < good["semantic_fact_recall"], (
        "adding a required fact did not lower recall — the answer key is inert")


def test_deleting_key_evidence_lowers_recall(bench):
    sc = _scenario(bench, "A")
    full = "GOAL_A fix CI NEXT_A run pytest pytest -q FAIL_A test_bench failed"
    trimmed = "GOAL_A fix CI NEXT_A run pytest"
    assert (bench.score_text(trimmed, sc)["semantic_fact_recall"] <
            bench.score_text(full, sc)["semantic_fact_recall"])


def test_irrelevant_noise_does_not_raise_accuracy(bench):
    sc = _scenario(bench, "A")
    base = "GOAL_A fix CI NEXT_A run pytest pytest -q FAIL_A test_bench failed"
    noisy = base + "\n" + ("irrelevant chatter " * 500)
    quiet = bench.score_text(base, sc)
    loud = bench.score_text(noisy, sc)
    for metric in ("semantic_fact_recall", "source_attribution",
                   "contradiction_preservation", "state_ambiguity_correctness"):
        assert loud[metric] <= quiet[metric], (
            "noise raised %s — it must not improve a quality score" % metric)


def test_false_claim_is_detected_but_template_labels_are_not(bench):
    sc = dict(_scenario(bench, "A"))
    sc["forbidden"] = ["INVENTED_FACT_XYZ"]
    clean = bench.score_text("GOAL_A fix CI", sc)
    dirty = bench.score_text("GOAL_A fix CI INVENTED_FACT_XYZ", sc)
    assert clean["false_claim_rate"] == 0.0
    assert dirty["false_claim_rate"] == 1.0
    # Core's own section labels are not answers and must never be fabricated.
    assert bench.score_text("## Evidence & Provenance", sc)["false_claim_rate"] == 0.0


def test_equal_budget_penalises_only_the_oversized_text(bench):
    sc = _scenario(bench, "A")
    short = bench.score_text("GOAL_A fix CI", sc, budget_tokens=bench.EQUAL_BUDGET_TOKENS)
    long = bench.score_text("x " * 20000, sc, budget_tokens=bench.EQUAL_BUDGET_TOKENS)
    assert short["budget_compliance"] == 1.0
    assert long["budget_compliance"] == 0.0


def test_scorer_does_not_read_method_name(bench):
    """Score is a pure function of (text, scenario); no method label is input."""
    sc = _scenario(bench, "A")
    text = "GOAL_A fix CI NEXT_A run pytest pytest -q FAIL_A test_bench failed"
    a = bench.score_text(text, sc)
    b = bench.score_text(text, dict(sc))
    assert a == b
    import inspect
    params = list(inspect.signature(bench.score_text).parameters)
    assert "method" not in params and "baseline" not in params and "name" not in params


# --- goal conditioning is content-driven -----------------------------------

def test_swapping_goal_changes_ranked_content(result):
    """The same history, a different goal -> a different top-3 evidence list."""
    c = next(r for r in result["scenarios"] if r["id"] == "C")
    assert c["baselines"]["sessionflow_bundle"]["focus"]["focus_in_top3"] is True
    assert c["baselines"]["sessionflow_bundle"]["focus"]["noise_in_top3"] is False
    # Hold-out I is designed so the noise IS tempting; a ranker that never
    # changes would also never be wrong, but would not be goal-conditioned.
    i = next(r for r in result["scenarios"] if r["id"] == "I")
    assert i["baselines"]["sessionflow_bundle"]["focus"]["goal_ranked_section"] is True


# --- Phase 4: command-relevance down-weight experiment ----------------------

def test_successful_command_can_crowd_out_goal_fact_and_downweight_recovers_it(bench, tmp_path):
    """Reproducible synthetic experiment; NOT a product-ranking change.

    On the hold-out's weak goal-token overlap, the current ranker lets a
    SUCCESSFUL `pytest -q` tool_call take a top-3 slot that a goal fact needs.
    A minimal down-weight of successful tool_calls (per-kind priority only)
    frees that slot. A FAILED command is genuine evidence and must be kept by
    both variants. This test pins the observation so the ADR is reproducible;
    it does not assert the product ranking should change.
    """
    from voyager.ranker import (extract_candidate_facts, score_fact,
                                tokenize, expand_tokens, FACT_PRIORITY)
    from voyager.store import Store

    sc = next(s for s in bench.SCENARIOS if s["id"] == "I")
    store = Store(tmp_path / "ci.db")
    store.con.commit()
    sids = [bench._add_session(store, sc, spec, i, tmp_path)
            for i, spec in enumerate(sc["sessions"])]
    rows = [store.session(x)[0] for x in sids]
    facts = extract_candidate_facts(store, rows)
    store.close()

    goal = sc["goal"]
    raw, exp = tokenize(goal), expand_tokens(tokenize(goal))

    def current(f):
        return score_fact(f, raw, exp)

    def downweight(f):
        s = score_fact(f, raw, exp)
        if f.kind == "tool_call" and f.exit_code in (0, None):
            s = round(s - FACT_PRIORITY.get("tool_call", 0.5), 4)
        return s

    def top3(fn):
        ranked = sorted(facts, key=lambda f: (-fn(f), -(f.ts or 0), f.provenance))[:3]
        return [(f.command or f.text or " ".join(f.paths)).lower() for f in ranked], ranked

    cur_text, _ = top3(current)
    down_text, _ = top3(downweight)

    # The SSL failure is the goal fact; the successful pytest command is noise
    # for this goal. Both strings come from the synthetic scenario itself.
    assert any("cert verify" in t for t in cur_text) is False, (
        "the scenario no longer reproduces the crowding observation; update "
        "the ADR rather than keeping a stale assertion")
    assert any("pytest" in t for t in cur_text), (
        "the successful command is no longer in the current top-3")
    assert any("pytest" in t for t in down_text) is False, (
        "the minimal down-weight did not free the slot")

    # A failed command is evidence, not noise: verify the down-weight rule
    # leaves failed tool_calls untouched by comparing their score directly.
    failed = [f for f in facts if f.kind == "tool_call" and f.exit_code not in (0, None)]
    for f in failed:
        assert downweight(f) == current(f), (
            "a failed command's score changed — that would discard real evidence")
