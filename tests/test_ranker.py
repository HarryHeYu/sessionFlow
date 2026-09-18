"""Phase 3 ranker tests (roadmap #4): real ranking contrasts, not arg parsing.

Scenario fixture: four sessions in one repo —
  * codex:ci       pytest failure + CI workflow edit (the "fix CI" facts)
  * grok:workflow  GitHub Actions build failure
  * claude:readme  README polish (the "improve README" facts)
  * codex:adapter  adapter parse fix

Contracts:
- goal="fix CI"  -> CI/test facts rank above README/UI facts
- goal="improve README" -> the order reverses
- no goal -> chronological order preserved (pre-Phase-3 semantics)
- single-session (handoff) and multi-session (merge) use the SAME
  extract/rank pipeline
- provenance survives rendering
- provider files are never written
"""

from __future__ import annotations

import json

import pytest

from voyager.cli import main
from voyager.model import new_event, new_session
from voyager.ranker import (
    CandidateFact,
    extract_candidate_facts,
    rank_candidates,
    tokenize,
    expand_tokens,
)
from voyager.store import Store

NOW = 1789000000.0


def _ev(sid, seq, kind, **kw):
    kw.setdefault("ts", NOW - 3600 + seq * 10)
    kw.setdefault("seq", seq)
    return new_event(sid=sid, kind=kind, **kw)


@pytest.fixture
def ranked_index(tmp_path):
    """Four synthetic sessions covering the ranking contrast scenario."""
    store = Store(tmp_path / "rank.db")
    src = tmp_path / "src.jsonl"
    src.write_text("{}", encoding="utf-8")
    src2 = tmp_path / "src2.jsonl"
    src2.write_text("{}", encoding="utf-8")

    def add(sid, provider, native, events, title):
        s = new_session(id=sid, provider=provider, native_session_id=native,
                        title=title, started_at=NOW - 7200,
                        updated_at=NOW - 600, cwd="E:/proj/demo",
                        repo_root="E:/proj/demo", message_count=len(events),
                        can_resume=False, resume_cmd=None)
        store.replace_session(s, events, provider, src)
        return s

    # CI facts
    add("codex:ci", "codex", "ci-1", [
        _ev("codex:ci", 1, "user", content="fix CI on main"),
        _ev("codex:ci", 2, "tool_call", tool_name="shell_command",
            command="python -m pytest -q", exit_code=1),
        _ev("codex:ci", 3, "tool_result", tool_call_id="t", exit_code=1,
            content="FAILED tests/test_ci.py::test_report"),
        _ev("codex:ci", 4, "tool_call", tool_name="edit",
            command="edit .github/workflows/ci.yml",
            file_path=".github/workflows/ci.yml", exit_code=0),
    ], title="fix CI")

    # workflow failure facts
    add("grok:wf", "grok", "wf-1", [
        _ev("grok:wf", 1, "tool_call", tool_name="shell_command",
            command="act -j build", exit_code=1),
        _ev("grok:wf", 2, "error", content="workflow build failed: "
             ".github/workflows/release.yml step exited 1"),
    ], title="workflow debug")

    # README facts
    add("claude:readme", "claude", "readme-1", [
        _ev("claude:readme", 1, "user", content="improve the README"),
        _ev("claude:readme", 2, "tool_call", tool_name="edit",
            command="edit README.md", file_path="README.md", exit_code=0),
        _ev("claude:readme", 3, "assistant", content="README polished: "
             "layout and Quick start improved"),
    ], title="improve README")

    # adapter facts
    add("codex:adapter", "codex", "adapter-1", [
        _ev("codex:adapter", 1, "tool_call", tool_name="edit",
            command="edit voyager/adapters/dsh.py",
            file_path="voyager/adapters/dsh.py", exit_code=0),
        _ev("codex:adapter", 2, "assistant", content="dsh adapter parse fix"),
    ], title="dsh adapter fix")

    yield store
    store.close()


def _all_facts(store):
    rows = store.sessions()
    facts = []
    for r in rows:
        facts.extend(extract_candidate_facts(store, [r]))
    return facts


# ---------------------------------------------------------------------------
# extraction (Phase 3a)
# ---------------------------------------------------------------------------

def test_extract_kinds_and_provenance(ranked_index):
    store = ranked_index
    rows = store.sessions()
    facts = extract_candidate_facts(store, rows)
    kinds = {f.kind for f in facts}
    assert {"user", "assistant", "tool_call", "error"} <= kinds
    for f in facts:
        assert f.provenance.startswith(f.source_session + "#")
        assert f.confidence == "extracted"
    # failed tool results are facts; successful ones are noise
    failed = [f for f in facts if f.kind == "tool_result"]
    assert failed and all(f.exit_code not in (0, None) for f in failed)
    assert any("FAILED" in f.text for f in failed)
    # reasoning never becomes a fact
    assert "reasoning" not in kinds


def test_extract_dedupes_identical_bodies(ranked_index):
    store = ranked_index
    rows = store.sessions()
    facts = extract_candidate_facts(store, rows)
    keys = [(f.kind, " ".join(f.text.split()).lower()) for f in facts]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# ranking (Phase 3b) — the required contrasts
# ---------------------------------------------------------------------------

def _score_map(facts, goal):
    ranked = rank_candidates(facts, goal=goal, now=NOW)
    return ranked, {f.provenance: s for f, s in ranked}


def test_goal_fix_ci_ranks_ci_above_readme(ranked_index):
    store = ranked_index
    facts = _all_facts(store)
    ranked, scores = _score_map(facts, "fix CI")

    top = ranked[0][0]
    assert top.source_session in ("codex:ci", "grok:wf"), \
        "CI/workflow facts must outrank everything for goal='fix CI'"
    readme_facts = [f for f in facts if "README" in " ".join(f.paths)
                    or "readme" in f.text.lower()]
    assert readme_facts, "fixture must contain README facts"
    for rf in readme_facts:
        rs = scores[rf.provenance]
        top_s = scores[ranked[0][0].provenance]
        assert rs < top_s, "README facts must rank below CI facts"


def test_goal_improve_readme_reverses_order(ranked_index):
    store = ranked_index
    facts = _all_facts(store)
    ranked, scores = _score_map(facts, "improve README")

    top = ranked[0][0]
    assert "readme" in top.text.lower() or any(
        "README" in p for p in top.paths), \
        "README facts must outrank everything for goal='improve README'"
    ci_facts = [f for f in facts
                if f.source_session in ("codex:ci", "grok:wf")
                and f.kind in ("error", "tool_call", "tool_result")]
    for cf in ci_facts:
        assert scores[cf.provenance] < scores[top.provenance]


def test_goal_lexicon_is_a_boost_not_a_hardcode(ranked_index):
    # an unseen goal with no lexicon entry still ranks by plain overlap
    facts = _all_facts(ranked_index)
    ranked = rank_candidates(facts, goal="dsh adapter parse", now=NOW)
    top = ranked[0][0]
    assert "dsh" in top.text.lower() or any(
        "dsh" in p.lower() for p in top.paths)


def test_no_goal_preserves_chronological_semantics(ranked_index):
    store = ranked_index
    facts = extract_candidate_facts(store, store.sessions())
    ranked = rank_candidates(facts, goal=None)
    assert all(s is None for _, s in ranked)
    assert [f for f, _ in ranked] == facts          # input order untouched
    # also true for a blank/whitespace goal
    assert [f for f, _ in rank_candidates(facts, goal="   ")] == facts


def test_ranking_is_deterministic(ranked_index):
    facts = _all_facts(ranked_index)
    a = rank_candidates(facts, goal="fix CI", now=NOW)
    b = rank_candidates(facts, goal="fix CI", now=NOW)
    assert [(f.provenance, s) for f, s in a] == [(f.provenance, s) for f, s in b]


# ---------------------------------------------------------------------------
# tokenization sanity (CJK + lexicon expansion)
# ---------------------------------------------------------------------------

def test_tokenize_cjk_bigrams():
    assert "中文" in tokenize("中文测试") and "文测" in tokenize("中文测试")
    assert "ci" in tokenize("fix CI on main")


def test_expand_tokens_is_additive_and_safe():
    out = expand_tokens({"ci"})
    assert {"pytest", "workflow"} <= out and "ci" in out
    assert expand_tokens({"totally-unknown-token-xyz"}) is not None


# ---------------------------------------------------------------------------
# Phase 3c: bundle/handoff integration through the SHARED pipeline
# ---------------------------------------------------------------------------

def test_bundle_with_goal_contains_ranked_evidence(ranked_index, tmp_path,
                                                   capsys):
    store = ranked_index
    rows = store.sessions()
    rc = main(["--db", str(store.db_path), "merge", "codex:ci", "grok:wf",
               "claude:readme", "--goal", "fix CI", "-o",
               str(tmp_path / "b.md")])
    assert rc == 0
    out = tmp_path / "b.md"
    content = out.read_text(encoding="utf-8")
    assert "## Goal-ranked evidence" in content
    assert "goal: \"fix CI\"" in content
    # provenance bound: every evidence line carries session#seq
    ev_lines = [l for l in content.splitlines()
                if l.startswith("- [") and "score" in l]
    assert ev_lines and all("#" in l for l in ev_lines)
    # README fact must not lead the evidence section
    head = "\n".join(ev_lines[:2])
    assert "readme" not in head.lower()


def test_bundle_without_goal_has_no_ranked_section(ranked_index, tmp_path):
    store = ranked_index
    rc = main(["--db", str(store.db_path), "merge", "codex:ci", "grok:wf",
               "-o", str(tmp_path / "b.md")])
    assert rc == 0
    content = (tmp_path / "b.md").read_text(encoding="utf-8")
    assert "## Goal-ranked evidence" not in content


def test_handoff_single_session_shares_pipeline(ranked_index, tmp_path):
    """Single-session handoff and multi-session merge must go through the
    same extract/rank pipeline: the same fact leads both outputs."""
    store = ranked_index
    rc = main(["--db", str(store.db_path), "handoff", "codex:ci",
               "--goal", "fix CI", "-o", str(tmp_path / "h.md")])
    assert rc == 0
    h = (tmp_path / "h.md").read_text(encoding="utf-8")
    assert "## Goal-ranked evidence" in h

    rc = main(["--db", str(store.db_path), "merge", "codex:ci",
               "--goal", "fix CI", "-o", str(tmp_path / "m.md")])
    assert rc == 0
    m = (tmp_path / "m.md").read_text(encoding="utf-8")

    def top_evidence(text):
        lines = [l for l in text.splitlines()
                 if l.startswith("- [") and "score" in l]
        return lines[0] if lines else None

    assert top_evidence(h) is not None
    assert top_evidence(h) == top_evidence(m), \
        "handoff and merge must share the ranking pipeline"


def test_goal_runs_never_write_provider_files(ranked_index, tmp_path):
    store = ranked_index
    srcs = sorted(tmp_path.glob("src*.jsonl"))
    before = {p: p.read_bytes() for p in srcs}
    rc = main(["--db", str(store.db_path), "merge", "codex:ci", "grok:wf",
               "--goal", "fix CI", "-o", str(tmp_path / "b.md")])
    assert rc == 0
    assert {p: p.read_bytes() for p in srcs} == before
