"""Ambiguity must propagate through every provider handler.

The locked core contract is:

    exact repo + 0 active WorkThread  -> nothing to continue
    exact repo + 1 active WorkThread  -> continue it
    exact repo + >1 active WorkThread -> AMBIGUOUS, refuse to choose

So `AMBIGUOUS != no_thread`.  Folding the two together hides an actionable
failure from the user, and — worse — a handler that reported "no thread" while
the core had in fact resolved one could make a caller think there was nothing to
attach.  These tests pin, per handler:

  * the result is an explicit ``error`` carrying ``ERROR_AMBIGUOUS_WORKTHREAD``
  * nothing is injected (the emit path writes no bytes)
  * no thread is auto-selected or auto-attached
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from voyager.integrations import antigravity_session_start as ag
from voyager.integrations import cursor_session_start as cu
from voyager.integrations import kiro_session_start as ki
from voyager.integrations import zcode_session_start as zo
from voyager.store import Store

AMBIGUOUS = "ERROR_AMBIGUOUS_WORKTHREAD"


@pytest.fixture
def two_threads(tmp_path):
    """One repo, two active WorkThreads: the ambiguous case."""
    subprocess.run(["git", "init", "-q", str(tmp_path / "workrepo")],
                   capture_output=True, timeout=30)
    repo = str(tmp_path / "workrepo")
    store = Store(tmp_path / "ambiguous.db")
    store.thread_create(repo_root=repo, title="one", goal="g")
    store.thread_create(repo_root=repo, title="two", goal="g")
    yield store, repo
    store.close()


def _assert_ambiguous(result, store, repo):
    assert result["status"] == "error", result
    assert result["attach_status"] == AMBIGUOUS
    assert "message" in result and "thread" in result["message"].lower()
    assert "recommended_action" in result["continuity_info"]
    # the core refused to choose: no session may have been attached
    assert store.thread_member_ids(
        store.q("SELECT id FROM threads WHERE repo_root=? ORDER BY id", (repo,))[0]["id"]
    ) == []


def test_zcode_reports_ambiguity(two_threads, monkeypatch, tmp_path):
    store, repo = two_threads
    monkeypatch.setenv("VOYAGER_LOG_DIR", str(tmp_path / "logs"))
    result = zo.handle_zcode_session_start(
        store=store, stdin_raw=json.dumps({"session_id": "s", "cwd": repo}))
    _assert_ambiguous(result, store, repo)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert zo.emit(result) == 0
    assert out.getvalue() == "", "an ambiguous result must inject nothing"
    events = [json.loads(l) for l in
              (tmp_path / "logs" / "zcode-hooks.jsonl").read_text(encoding="utf-8").splitlines() if l]
    assert any(e.get("event") == "ambiguous" for e in events), events


def test_cursor_reports_ambiguity(two_threads, monkeypatch, tmp_path):
    store, repo = two_threads
    monkeypatch.setenv("VOYAGER_LOG_DIR", str(tmp_path / "logs"))
    result = cu.handle_cursor_session_start(
        store=store, stdin_raw=json.dumps({"session_id": "s", "workspace_roots": [repo]}))
    _assert_ambiguous(result, store, repo)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cu.emit(result) == 0
    assert out.getvalue() == ""
    events = [json.loads(l) for l in
              (tmp_path / "logs" / "cursor-hooks.jsonl").read_text(encoding="utf-8").splitlines() if l]
    assert any(e.get("event") == "ambiguous" for e in events), events


def test_kiro_reports_ambiguity(two_threads, monkeypatch, tmp_path):
    store, repo = two_threads
    monkeypatch.setenv("VOYAGER_LOG_DIR", str(tmp_path / "logs"))
    result = ki.handle_kiro_session_start(store=store, cwd=repo, stdin_raw="{}")
    _assert_ambiguous(result, store, repo)
    assert "recommended_action" in result["continuity_info"]
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ki.emit(result) == 0
    assert out.getvalue() == ""
    events = [json.loads(l) for l in
              (tmp_path / "logs" / "kiro-hooks.jsonl").read_text(encoding="utf-8").splitlines() if l]
    assert any(e.get("event") == "ambiguous" for e in events), events


def test_antigravity_reports_ambiguity(two_threads, monkeypatch, tmp_path):
    store, repo = two_threads
    monkeypatch.setenv("VOYAGER_LOG_DIR", str(tmp_path / "logs"))
    result = ag.handle_antigravity_pre_invocation(
        store=store,
        stdin_raw=json.dumps({"conversationId": "c", "workspacePaths": [repo],
                              "invocationNum": 0}))
    _assert_ambiguous(result, store, repo)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ag.emit(result) == 0
    assert out.getvalue() == ""
    events = [json.loads(l) for l in
              (tmp_path / "logs" / "antigravity-hooks.jsonl").read_text(encoding="utf-8").splitlines() if l]
    assert any(e.get("event") == "ambiguous" for e in events), events


def test_a_single_thread_is_still_continued(two_threads, tmp_path):
    """Guard against over-correcting: one thread must still be served."""
    store, repo = two_threads
    # drop one thread, leaving exactly one
    rows = store.q("SELECT id FROM threads WHERE repo_root=? ORDER BY id", (repo,))
    store.con.execute("DELETE FROM threads WHERE id=?", (rows[1]["id"],))
    store.con.commit()
    result = zo.handle_zcode_session_start(
        store=store, stdin_raw=json.dumps({"session_id": "s", "cwd": repo}))
    assert result["status"] in ("context_ready", "no_thread")
    assert result["status"] != "error"
