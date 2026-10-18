"""Repo-scope continuation must compile from a bounded *set*, not one session.

The regression this pins: with no WorkThread linking them, repo scope used to
pick the single newest session.  A task routinely runs Codex → Claude → another
agent, so the newest session is often a documentation touch-up while the open
items and the failing test live in the earlier ones.  Measured on synthetic
cases: 1 of 3 sessions compiled, zero open items, zero errors.

Everything here is synthetic and lives in tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voyager import api
from voyager.model import new_event, new_session
from voyager.store import Store

REPO = "E:/proj/repo-scope"
OTHER = "E:/proj/elsewhere"


def _seed(db_path: Path, sessions) -> None:
    """sessions: (sid_suffix, provider, title, repo, updated_at)."""
    store = Store(db_path)
    src = db_path.parent / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    for suffix, provider, title, repo, updated in sessions:
        sid = "{0}:{1}".format(provider, suffix)
        store.replace_session(
            new_session(id=sid, provider=provider, native_session_id=suffix,
                        title=title, started_at=updated - 100, updated_at=updated,
                        cwd=repo, repo_root=repo, message_count=2, can_resume=True),
            [
                new_event(sid=sid, ts=updated - 100, seq=0, kind="user",
                          content="work on " + title),
                new_event(sid=sid, ts=updated - 50, seq=1, kind="assistant",
                          content=title + ": half done, still open"),
            ],
            provider, src)
    store.close()


def _ctx(db, **params):
    return api.continue_context(db=db, **params)


def test_repo_scope_compiles_the_newest_few_not_just_the_newest(tmp_path):
    """Three agents on one task: all three belong in the bundle."""
    db = tmp_path / "i.db"
    _seed(db, [
        ("a1", "codex", "Add the exporter", REPO, 1000),
        ("a2", "claude", "Fix exporter quoting", REPO, 2000),
        ("a3", "dsh", "Update exporter docs", REPO, 3000),
    ])
    res = _ctx(db, repo=REPO)

    assert res.get("error") is None, res
    assert len(res["scope"]) == 3, (
        "a cross-agent task is several sessions; compiling one drops the rest"
    )
    assert res["selection_scope"] == "repo_recent"
    assert res["selected_session_ids"] == res["scope"]
    assert res["scope_ambiguous"] is False, (
        "nothing was left out here, so this is not ambiguous"
    )
    assert res["excluded_session_count"] == 0
    # The reply must admit that recency is a default, not a task match.
    assert "recency is a default" in res["selection_reason"]
    assert "no WorkThread links" in res["selection_reason"]

    # The earlier sessions' open items are actually in the bundle now.
    bundle = res["bundle"]
    assert bundle.count("still open") >= 3
    assert "## Evidence & Provenance" in bundle


def test_repo_scope_is_bounded_and_reports_what_it_left_out(tmp_path):
    """A repo with many sessions must not produce a many-session bundle."""
    db = tmp_path / "i.db"
    _seed(db, [("s%02d" % i, "codex", "task %d" % i, REPO, 1000 + i)
               for i in range(12)])
    res = _ctx(db, repo=REPO)

    assert len(res["scope"]) == api.MAX_REPO_SCOPE_SESSIONS
    # Absolute bounds too, not just the constant: asserting only
    # `== MAX_REPO_SCOPE_SESSIONS` still passes if the constant itself is
    # lowered to 1, which is exactly the behaviour this test exists to prevent.
    assert 1 < len(res["scope"]) < 12, (
        "bounded means more than the old single session and fewer than all of them"
    )
    assert res["excluded_session_count"] == 12 - len(res["scope"])
    assert res["scope_ambiguous"] is True, (
        "sessions were dropped, so the caller must be told the set is a subset"
    )
    assert "12" in res["selection_reason"], "the reason must name the total"
    # Newest first: the five highest updated_at values.
    assert res["scope"][0] == "codex:s11"
    assert "codex:s00" not in res["scope"]


def test_a_named_thread_still_wins_and_no_default_is_reported(tmp_path):
    """Thread scope is a hard constraint; the fallback must not run."""
    db = tmp_path / "i.db"
    _seed(db, [
        ("t1", "codex", "auth rework", REPO, 1000),
        ("x1", "claude", "unrelated ui", REPO, 9000),
        ("x2", "codex", "unrelated db", REPO, 9500),
    ])
    store = Store(db)
    tid = store.thread_create(repo_root=REPO, title="auth")
    store.thread_attach(tid, "codex:t1")
    store.close()

    res = _ctx(db, thread_id=tid)
    assert res["scope"] == ["codex:t1"], "a named thread is not a suggestion"
    assert "selection_scope" not in res, (
        "no default was invented, so no fallback metadata belongs in the reply"
    )


def test_explicit_session_refs_still_win(tmp_path):
    db = tmp_path / "i.db"
    _seed(db, [
        ("r1", "codex", "one", REPO, 1000),
        ("r2", "claude", "two", REPO, 2000),
        ("r3", "dsh", "three", REPO, 3000),
    ])
    res = _ctx(db, session_refs=["claude:r2"])
    assert res["scope"] == ["claude:r2"]
    assert "selection_scope" not in res


def test_a_single_session_repo_is_not_reported_as_ambiguous(tmp_path):
    db = tmp_path / "i.db"
    _seed(db, [("only", "codex", "the only task", REPO, 1000)])
    res = _ctx(db, repo=REPO)
    assert res["scope"] == ["codex:only"]
    assert res["scope_ambiguous"] is False
    assert res["excluded_session_count"] == 0


def test_repo_scope_does_not_reach_into_another_repo(tmp_path):
    """Widening the set must not widen the repo filter."""
    db = tmp_path / "i.db"
    _seed(db, [
        ("in1", "codex", "ours", REPO, 1000),
        ("in2", "claude", "ours too", REPO, 2000),
        ("out1", "codex", "theirs", OTHER, 9000),
    ])
    res = _ctx(db, repo=REPO)
    assert all(s.startswith(("codex:in", "claude:in")) for s in res["scope"]), res["scope"]
    assert "codex:out1" not in res["scope"]
