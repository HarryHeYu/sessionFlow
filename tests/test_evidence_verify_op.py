"""The additive ``evidence_verify`` bridge op (dsh-sessionflow#9 / Core #22).

A machine client reaching this op over ``voyager api`` must receive the SAME
five verdicts the CLI does, by name, in ``result.status``.  The verdict is a
*field*, not the process exit code: ``callOp()`` reads one JSON line and would
otherwise lose the four non-zero normal states to ``CLI_FAILED``.

This op is a thin pass-through.  ``verify()`` itself is owned and tested by
``test_evidence_verify.py``; here we assert only the bridge contract:
dispatch, the five names, the {id, result} envelope, discoverability, and that
an older Core's ``unknown op`` is a capability error and never a verdict.

Fixtures follow the real ``voyager evidence verify --json`` shape (the same
seeded index the CLI tests use); nothing here invents a response field.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from voyager.api import (BRIDGE_SCHEMA_VERSION, handle_request,
                         integration_info)
from voyager.evidence import (EXIT_CODES, INVALID_REFERENCE, MATCH, MISMATCH,
                              NOT_FOUND, UNVERIFIABLE)
from voyager.model import new_event, new_session
from voyager.store import Store
from voyager.util import event_digest

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

CLAIM = "All 42 tests pass."


def _events(sid="codex:m", claim=CLAIM, rc=1):
    return [
        new_event(sid=sid, ts=1000, seq=0, kind="user", content="run the tests"),
        new_event(sid=sid, ts=1001, seq=1, kind="tool_call", tool_name="exec",
                  command="pytest -q", tool_call_id="c1"),
        new_event(sid=sid, ts=1002, seq=2, kind="tool_result", role="tool",
                  tool_call_id="c1", exit_code=rc, tool_output="3 failed"),
        new_event(sid=sid, ts=1003, seq=3, kind="assistant", content=claim),
    ]


@pytest.fixture
def indexed(tmp_path):
    """A seeded index; the same shape the CLI verification tests use."""
    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store = Store(db)
    store.replace_session(
        new_session(id="codex:m", provider="codex", native_session_id="m",
                    title="t", started_at=1000, updated_at=1000,
                    cwd="E:/r", repo_root="E:/r", message_count=4,
                    can_resume=True),
        _events(), "codex", src)
    store.close()
    return db


def _digest_at(db, seq, sid="codex:m"):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("select * from events where sid=? and seq=?",
                      (sid, seq)).fetchone()
    con.close()
    assert row is not None, "no event at seq %s" % seq
    return event_digest(row["kind"], row["content"], row["command"],
                        row["tool_output"], row["tool_call_id"],
                        row["exit_code"])


def _op(db, **params):
    """Dispatch through the real handle_request, exactly as `voyager api` does."""
    req = {"id": 1, "op": "evidence_verify",
           "params": {"db": str(db), **params}}
    res = handle_request(req)
    assert res.get("error") is None, res
    assert res["id"] == 1, "the {id, result} envelope is intact"
    return res["result"]


# ---------------------------------------------------------------------------
# every state round-trips BY NAME, including the easy-to-lose ones
# ---------------------------------------------------------------------------

def test_all_five_states_round_trip_by_name(indexed):
    d3 = _digest_at(indexed, 3)
    # MATCH: a consistent reference
    r = _op(indexed, session_id="codex:m", seq=3, digest=d3)
    assert r["status"] == MATCH
    assert r["exit_code"] == EXIT_CODES[MATCH] == 0

    # MISMATCH: the same seq now holds a different event
    r = _op(indexed, session_id="codex:m", seq=3, digest="deadbeef")
    assert r["status"] == MISMATCH
    assert r["exit_code"] == EXIT_CODES[MISMATCH] == 1

    # NOT_FOUND: unknown session / unknown position are both NOT_FOUND, never
    # collapsed into a transport error
    r = _op(indexed, session_id="codex:does-not-exist", seq=3, digest="deadbeef")
    assert r["status"] == NOT_FOUND
    assert r["exit_code"] == EXIT_CODES[NOT_FOUND] == 2
    r = _op(indexed, session_id="codex:m", seq=99, digest="deadbeef")
    assert r["status"] == NOT_FOUND

    # UNVERIFIABLE: no digest, only the position can be checked
    r = _op(indexed, session_id="codex:m", seq=3)
    assert r["status"] == UNVERIFIABLE
    assert r["exit_code"] == EXIT_CODES[UNVERIFIABLE] == 3
    assert r["found_digest"] == d3, "it still names what is there"

    # INVALID_REFERENCE: malformed seq / non-8-hex digest (the state most
    # easily lost when a client maps exit codes)
    r = _op(indexed, session_id="codex:m", seq="three", digest="deadbeef")
    assert r["status"] == INVALID_REFERENCE
    assert r["exit_code"] == EXIT_CODES[INVALID_REFERENCE] == 4
    r = _op(indexed, session_id="codex:m", seq=3, digest="zzzz")
    assert r["status"] == INVALID_REFERENCE
    r = _op(indexed, session_id="codex:m", seq=3, digest="c71c180a1")
    assert r["status"] == INVALID_REFERENCE


def test_the_result_is_documented_shape_verbatim(indexed):
    """The op returns verify()'s dict, not a reinterpretation of it."""
    d3 = _digest_at(indexed, 3)
    r = _op(indexed, session_id="codex:m", seq=3, digest=d3)
    for key in ("status", "exit_code", "session_id", "seq", "digest",
                "found_digest", "source_available", "message"):
        assert key in r, "verbatim result must carry %s" % key
    assert r["session_id"] == "codex:m"
    assert r["seq"] == 3
    assert r["digest"] == d3
    assert r["found_digest"] == d3
    assert isinstance(r["message"], str) and r["message"]


def test_the_verdict_is_a_field_not_the_process_exit_code(indexed):
    """A non-zero exit_code is a NORMAL verdict, not a failed transport."""
    d3 = _digest_at(indexed, 3)
    req = {"id": 7, "op": "evidence_verify",
           "params": {"db": str(indexed), "session_id": "codex:m", "seq": 3,
                      "digest": "deadbeef"}}
    res = handle_request(req)
    assert "error" not in res, "a MISMATCH must not become a bridge error"
    assert res["id"] == 7
    assert res["result"]["status"] == MISMATCH
    assert res["result"]["exit_code"] == 1
    # The dict is JSON-able (serve() prints it as one line) and the envelope
    # itself carries no process exit code.
    assert json.dumps(res, ensure_ascii=False)


def test_match_never_claims_the_claim_is_true(indexed):
    """MATCH is reference consistency, never fact-checking."""
    d3 = _digest_at(indexed, 3)
    r = _op(indexed, session_id="codex:m", seq=3, digest=d3)
    assert r["status"] == MATCH
    text = json.dumps(r, ensure_ascii=False).lower()
    assert "the claim is true" not in text
    assert "claim is true" not in text


# ---------------------------------------------------------------------------
# discoverability: additive op, schema stays 1
# ---------------------------------------------------------------------------

def test_integration_info_advertises_the_new_op():
    info = integration_info()
    assert "evidence_verify" in info["ops"]
    assert "evidence_verify" in info["capabilities"]
    assert info["schema_version"] == BRIDGE_SCHEMA_VERSION == 1


def test_the_op_is_dispatchable():
    res = handle_request({"id": 3, "op": "evidence_verify",
                          "params": {"session_id": "s", "seq": 1}})
    assert res["id"] == 3
    # Even with no index, the five-state machinery returns a normal verdict
    # (an empty store -> NOT_FOUND), never a crash or a transport error.
    assert "error" not in res
    assert res["result"]["status"] in (MATCH, MISMATCH, NOT_FOUND,
                                       UNVERIFIABLE, INVALID_REFERENCE)


# ---------------------------------------------------------------------------
# old-Core degradation: 'unknown op' is NOT evidence NOT_FOUND and NOT MATCH
# ---------------------------------------------------------------------------

def test_unknown_op_is_a_capability_error_not_a_verdict(indexed):
    """A Core without this op answers `unknown op: ...`, which callOp maps to
    OP_ERROR.  It must never be mistaken for evidence NOT_FOUND or MATCH."""
    from voyager import api

    saved = dict(api._OPS)
    try:
        api._OPS.pop("evidence_verify", None)  # simulate an older Core
        res = handle_request({"id": 9, "op": "evidence_verify",
                              "params": {"db": str(indexed),
                                         "session_id": "codex:m", "seq": 3,
                                         "digest": "deadbeef"}})
    finally:
        api._OPS.clear()
        api._OPS.update(saved)

    assert "result" not in res, "an unknown op has no verdict result"
    assert "error" in res
    err = res["error"]
    assert err.startswith("unknown op")
    assert "evidence_verify" in err
    # The critical distinction: NOT_FOUND / MATCH describe evidence states, not
    # a missing capability.  The error names the op and the available ops, and
    # is a non-empty, named refusal the plugin can map to OP_ERROR.
    assert err.strip().startswith("unknown op:")
    assert "ops:" in err
    # An evidence verdict would arrive under "result", never as this error.
    assert NOT_FOUND not in err
    assert MATCH not in err


# ---------------------------------------------------------------------------
# real stdio surface: one JSON line, {id, result}, no process-exit coupling
# ---------------------------------------------------------------------------

def test_stdio_api_returns_one_json_line_with_the_verdict(indexed):
    """The path `callOp` actually drives: `voyager api` over stdio."""
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(REPO_ROOT),
        "VOYAGER_NO_SYNC": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    d3 = _digest_at(indexed, 3)
    req = {"id": 42, "op": "evidence_verify",
           "params": {"db": str(indexed), "session_id": "codex:m", "seq": 3,
                      "digest": d3}}
    p = subprocess.run(
        [PY, "-m", "voyager.api", "--db", str(indexed)],
        input=json.dumps(req) + "\n", capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
        cwd=str(indexed.parent), timeout=120)
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, p.stdout + p.stderr
    payload = json.loads(lines[0])
    assert payload["id"] == 42
    assert "error" not in payload
    assert payload["result"]["status"] == MATCH
