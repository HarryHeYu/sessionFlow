"""Machine-readable bridge ops for thin client integrations (DSH plugin, VS Code).

`search`, `current_work`, `continue_context` and `integration_info` are the
only additions a client needs from the core.  Each must be a thin pass-through
to a function the CLI already uses, must be JSON-able, and must never write --
a second implementation is how the two surfaces drift.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import voyager
from voyager.api import BRIDGE_SCHEMA_VERSION, handle_request, integration_info
from voyager.model import new_event, new_session
from voyager.store import Store

REPO_ROOT = Path(voyager.__file__).resolve().parent.parent


def _seed(db_path: Path, with_thread: bool = True) -> None:
    store = Store(db_path)
    src = db_path.parent / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    s = new_session(
        id="codex:br1", provider="codex", native_session_id="br1",
        title="bridge demo", started_at=1000.0, updated_at=2000.0,
        cwd="E:/proj/demo", repo_root="E:/proj/demo", message_count=1,
        can_resume=True, resume_cmd="codex resume br1",
        metadata={}, raw_metadata={})
    store.replace_session(
        s, [new_event(sid=s["id"], ts=1000.0, seq=0, kind="user",
                      content="fix the evaluation pipeline")], "codex", src)
    if with_thread:
        tid = store.thread_create(repo_root="E:/proj/demo",
                                  title="bridge thread", goal="finish the bridge")
        store.thread_attach(tid, s["id"])
    store.close()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "bridge.db"
    _seed(path, with_thread=True)
    return path


@pytest.fixture
def db_no_thread(tmp_path):
    path = tmp_path / "no_thread.db"
    _seed(path, with_thread=False)
    return path


def _op(db, op, **params):
    res = handle_request({"id": 1, "op": op, "params": {"db": str(db), **params}})
    assert res.get("error") is None, res
    return res["result"]


def _cli(*argv):
    """Run the CLI in a child process with a UTF-8 pipe (Windows-safe)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["VOYAGER_NO_SYNC"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-m", "voyager.cli", *argv],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


# --- integration_info -------------------------------------------------------

def test_integration_info_shape():
    info = integration_info()
    assert info["name"] == "sessionFlow"
    assert info["package"] == "voyager"
    assert info["schema_version"] == BRIDGE_SCHEMA_VERSION == 1
    for op in ("search", "current_work", "continue_context", "integration_info",
               "bundle_preview", "overview"):
        assert op in info["ops"]


def test_integration_info_is_dispatchable():
    res = handle_request({"id": 7, "op": "integration_info"})
    assert res["id"] == 7 and "error" not in res
    assert res["result"]["schema_version"] == 1


# --- search -----------------------------------------------------------------

def test_search_returns_the_documented_shape(db):
    r = _op(db, "search", query="evaluation")
    assert r["count"] == 1
    hit = r["results"][0]
    assert set(hit) == {
        "id", "provider", "native_session_id", "title", "repo", "updated_at",
        "matched_at", "matched_kind", "matched_tool", "matched_file", "excerpt",
    }
    assert hit["id"] == "codex:br1"
    assert hit["provider"] == "codex"
    # the excerpt is the matching turn, with the FTS snippet markers around
    # the match (it may be elided: ">>>evalua<<<…")
    assert ">>>" in hit["excerpt"] and "evalua" in hit["excerpt"]


def test_search_no_match_is_empty_not_an_error(db):
    r = _op(db, "search", query="zzz-no-such-term-zzz")
    assert r == {"query": "zzz-no-such-term-zzz", "count": 0, "results": []}


def test_search_rejects_a_bad_time_filter(db):
    res = handle_request({"id": 1, "op": "search",
                          "params": {"db": str(db), "query": "x",
                                     "since": "not-a-date"}})
    assert "error" in res


# --- current_work -----------------------------------------------------------

def test_current_work_with_a_thread(db):
    r = _op(db, "current_work", repo="E:/proj/demo")
    assert r["has_thread"] is True
    assert r["thread"]["title"] == "bridge thread"
    assert [m["id"] for m in r["members"]] == ["codex:br1"]
    assert r["scope"] == ["codex:br1"]
    assert "bundle" in r["continuation"]


def test_current_work_without_a_thread_falls_back_to_the_session(db_no_thread):
    r = _op(db_no_thread, "current_work", repo="E:/proj/demo")
    assert r["has_thread"] is False
    assert r["session"]["id"] == "codex:br1"
    assert r["scope"] == ["codex:br1"]


def test_current_work_for_an_unknown_repo_is_empty_not_an_error(db):
    r = _op(db, "current_work", repo="E:/no/such/repo")
    assert r["has_thread"] is False
    assert r["scope"] == []


# --- continue_context -------------------------------------------------------

def test_continue_context_by_session_ref(db):
    r = _op(db, "continue_context", session_refs=["codex:br1"])
    assert r["scope"] == ["codex:br1"]
    assert "bundle" in r and isinstance(r["bundle"], str)


def test_continue_context_by_thread(db):
    tid = _op(db, "current_work", repo="E:/proj/demo")["thread"]["id"]
    r = _op(db, "continue_context", thread_id=tid)
    assert r["scope"] == ["codex:br1"]


def test_continue_context_by_repo_uses_the_active_thread(db):
    r = _op(db, "continue_context", repo="E:/proj/demo")
    assert r["scope"] == ["codex:br1"]


def test_continue_context_with_no_sessions_is_an_error(tmp_path):
    empty = tmp_path / "empty.db"
    Store(empty).close()
    res = handle_request({"id": 1, "op": "continue_context",
                          "params": {"db": str(empty)}})
    assert "error" in res["result"]


# --- CLI surfaces -----------------------------------------------------------

def test_cli_integration_info_emits_json():
    out = _cli("integration-info", "--json")
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["schema_version"] == 1
    assert "search" in payload["ops"]


def test_cli_merge_json_includes_the_compiled_context(db):
    out = _cli("merge", "codex:br1", "--json", "--db", str(db))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["thread_id"]
    assert payload["action"] in ("bundle", "native-resume", "transcript")
    assert "evaluation" in payload["context"]
