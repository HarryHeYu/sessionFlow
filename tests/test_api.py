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


def test_bridge_survives_a_malformed_op_and_answers_the_next_request(db):
    """One bad request envelope must not kill a long-lived bridge process.

    The VS Code extension keeps a single stdio bridge for the lifetime of the
    panel. `handle_request()` used to call `_OPS.get(op)` before its try block,
    so `{"op": []}` killed the loop and every later valid request hung.
    """
    checkout = Path(__file__).resolve().parents[1]
    p = subprocess.Popen(
        [sys.executable, "-m", "voyager.api", "--db", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", cwd=str(checkout))
    try:
        reqs = [
            {"id": 1, "op": "integration_info", "params": {}},
            {"id": 2, "op": [], "params": {}},
            {"id": 3, "op": "integration_info", "params": {}},
        ]
        p.stdin.write("\n".join(json.dumps(r) for r in reqs) + "\n")
        p.stdin.flush()
        answers = [json.loads(p.stdout.readline()) for _ in reqs]
        assert "error" in answers[1]
        assert answers[2]["id"] == 3
        assert answers[2]["result"]["schema_version"] == 1
    finally:
        p.terminate()
        p.wait(timeout=10)


def test_overview_reads_events_only_for_the_rows_it_returns(tmp_path,
                                                            monkeypatch):
    """`overview` fetched every session's events, then kept `limit` rows.

    That is the documented ``OVERVIEW_RECENT_SESSIONS_N1``.  On the 10k
    benchmark dataset it issued 10,016 statements to produce 12 rows, with
    `subprocess = 0` — so the debt's recorded justification ("the wall-clock
    cost is dominated by sandbox spawn overhead, not the query") does not hold.
    """
    from voyager.api import overview

    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}\n", encoding="utf-8")

    store = Store(db)
    total = 40
    for i in range(total):
        sid = "codex:s%02d" % i
        session = new_session(
            id=sid, provider="codex", native_session_id=sid, title="t",
            started_at=1.0, updated_at=1.0 + i, cwd="E:/p", repo_root="E:/p",
            message_count=1, tool_count=0)
        store.replace_session(
            session,
            [new_event(sid=sid, ts=1.0 + i, seq=1, kind="user", role="user",
                       content="hello")],
            "codex", src)
    store.close()

    seen = {"n": 0}
    real_events = Store.events

    def counting_events(self, sid):
        seen["n"] += 1
        return real_events(self, sid)

    monkeypatch.setattr(Store, "events", counting_events)

    limit = 12
    out = overview(db=db, hours=10 ** 6, limit=limit)

    assert len(out["recent_sessions"]) == limit
    assert seen["n"] <= limit, (
        "overview read events for %d of %d sessions to return %d rows"
        % (seen["n"], total, limit))

# --- bundle_preview: live git state, not cached index state -----------------

def _fake_git(monkeypatch, branch, oid):
    from voyager import continuity
    calls = []

    class Result:
        returncode = 0
        stdout = "# branch.oid %s\n# branch.head %s\n" % (oid, branch)

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    monkeypatch.setattr(continuity.subprocess, "run", fake_run)
    return calls


def test_bundle_preview_uses_one_git_probe(db, monkeypatch):
    calls = _fake_git(monkeypatch, "main", "abc1234def")
    res = handle_request({"id": 7, "op": "bundle_preview",
                          "params": {"db": str(db),
                                     "session_refs": ["codex:api1"]}})
    assert "error" not in res, res
    assert len(calls) == 1, "one git status probe per preview"
    assert calls[0][0][:2] == ["git", "status"]
    bundle = res["result"]["bundle"]
    assert "Current repository state (live snapshot)" in bundle
    assert "`main`" in bundle and "`abc1234`" in bundle


def test_bundle_preview_live_git_state_is_not_cached(db, monkeypatch):
    calls = _fake_git(monkeypatch, "main", "abc1234def")
    first = handle_request({"id": 8, "op": "bundle_preview",
                            "params": {"db": str(db),
                                       "session_refs": ["codex:api1"]}})["result"]
    _fake_git(monkeypatch, "feature/live", "def5678abc")
    second = handle_request({"id": 9, "op": "bundle_preview",
                             "params": {"db": str(db),
                                        "session_refs": ["codex:api1"]}})["result"]
    assert first["bundle"] != second["bundle"]
    assert "`main`" in first["bundle"]
    assert "`feature/live`" in second["bundle"]
    assert len(calls) == 1
