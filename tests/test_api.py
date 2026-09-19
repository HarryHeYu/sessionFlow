"""Phase 7 local API + stdio bridge contract tests (roadmap #8).

Contracts:
- api functions are JSON-able wrappers over the core (no direct SQLite in
  callers) and never write anything
- unknown ops and bad params produce {"error": ...}, never a crash
- bundle_preview applies budget/goal identically to the CLI path
- the stdio bridge round-trips requests over a real subprocess
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from voyager.api import handle_request
from voyager.model import new_event, new_session
from voyager.store import Store


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / "api.db")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    s = new_session(
        id="codex:api1", provider="codex", native_session_id="api1",
        title="api demo", started_at=1000.0, updated_at=2000.0,
        cwd="E:/proj/demo", repo_root="E:/proj/demo", message_count=1,
        can_resume=True, resume_cmd="codex resume api1",
        metadata={}, raw_metadata={})
    store.replace_session(
        s, [new_event(sid=s["id"], ts=1000.0, seq=0, kind="user",
                      content="api test session")], "codex", src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="api thread")
    store.thread_attach(tid, s["id"])
    store.close()
    return tmp_path / "api.db"


def test_overview_shape(db):
    res = handle_request({"id": 1, "op": "overview",
                          "params": {"db": str(db), "repo": "E:/proj/demo",
                                     "hours": 10 ** 9}})
    assert res["id"] == 1 and "error" not in res
    r = res["result"]
    assert r["stats"]["sessions"] == 1
    assert len(r["threads"]) == 1 and r["threads"][0]["title"] == "api thread"
    assert r["recent_sessions"][0]["last_user"] == "api test session"
    assert r["recent_sessions"][0]["id"] == "codex:api1"


def test_thread_detail_includes_lease_and_members(db):
    res = handle_request({"id": 2, "op": "thread_detail",
                          "params": {"db": str(db), "thread_id": "thr"}})
    r = res["result"]
    assert r["lease"]["held"] is False
    assert r["members"][0]["native_id"] == "api1"


def test_sessions_filter(db):
    res = handle_request({"id": 3, "op": "sessions",
                          "params": {"db": str(db), "repo": "nomatch"}})
    assert res["result"] == []


def test_bundle_preview_applies_budget(db):
    res = handle_request({"id": 4, "op": "bundle_preview",
                          "params": {"db": str(db),
                                     "session_refs": ["codex:api1"],
                                     "goal": "api test",
                                     "budget": "compact"}})
    assert "error" not in res, res
    r = res["result"]
    assert r["estimated_tokens"] <= 4000
    assert "api test session" in r["bundle"]


def test_unknown_op_is_an_error(db):
    res = handle_request({"id": 5, "op": "nope", "params": {}})
    assert "unknown op" in res["error"]


def test_missing_session_is_an_error_not_a_crash(db):
    res = handle_request({"id": 6, "op": "bundle_preview",
                          "params": {"db": str(db),
                                     "session_refs": ["ghost"]}})
    assert "not found" in res["result"]["error"]


def test_bridge_round_trip(db):
    """The stdio bridge must answer JSON-lines over a real subprocess."""
    # the subprocess needs voyager importable: run from the checkout root
    checkout = Path(__file__).resolve().parents[1]
    p = subprocess.Popen(
        [sys.executable, "-m", "voyager.api", "--db", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        cwd=str(checkout))
    try:
        reqs = [
            json.dumps({"id": 1, "op": "overview", "params": {}}),
            json.dumps({"id": 2, "op": "thread_detail",
                        "params": {"thread_id": "thr"}}),
            json.dumps({"id": 3, "op": "nope"}),
        ]
        p.stdin.write("\n".join(reqs) + "\n")
        p.stdin.flush()
        lines = [p.stdout.readline() for _ in reqs]
        answers = [json.loads(l) for l in lines if l.strip()]
        assert len(answers) == 3
        assert answers[0]["result"]["stats"]["sessions"] == 1
        assert answers[2]["error"].startswith("unknown op")
    finally:
        p.terminate()
        p.wait(timeout=10)
