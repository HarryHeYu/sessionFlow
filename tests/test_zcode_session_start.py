"""ZCode SessionStart handler tests.

The handler is a thin shell over ``startup_continuity``: payload normalisation
in, tiered-v1 envelope out, fail-open everywhere.  These tests pin exactly that
boundary, plus the parts of ZCode's documented contract
(https://zcode.z.ai/cn/docs/hooks) that the handler depends on:

  * stdin carries ``session_id`` / ``cwd`` with camelCase aliases alongside
  * stdout must be ``{"hookSpecificOutput": {"hookEventName": "SessionStart",
    "additionalContext": ...}}``
  * the handler must never break a session: every path exits 0

The shared core is covered by the startup/cache/continuity suites.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from voyager.integrations.zcode_session_start import (
    emit,
    handle_zcode_session_start,
    normalize_event,
)
from voyager.model import new_event, new_session
from voyager.store import Store


@pytest.fixture
def repo_store(tmp_path):
    """Isolated store + real git repo + one attached seeded session."""
    subprocess.run(["git", "init", "-q", str(tmp_path / "workrepo")],
                   capture_output=True, timeout=30)
    repo = str(tmp_path / "workrepo")
    store = Store(tmp_path / "hook.db")
    yield store, repo
    store.close()


def _seed(store, tmp_path, repo, content="把测试标记写进上下文"):
    tid = store.thread_create(repo_root=repo, title="tiered zcode",
                              goal="continue across agents")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    sess = new_session(id="zcode:seed", provider="zcode",
                       native_session_id="seed", title="seeded",
                       started_at=1.0, updated_at=2.0,
                       repo_root=repo, cwd=repo)
    store.replace_session(
        sess,
        [new_event(sid="zcode:seed", seq=1, kind="user", ts=1.0, content=content),
         new_event(sid="zcode:seed", seq=2, kind="assistant", ts=2.0,
                   content="state so far")],
        "zcode", src)
    store.thread_attach(tid, "zcode:seed")
    return tid


def _run(monkeypatch, payload, store=None, cwd=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return handle_zcode_session_start(cwd=cwd, store=store)


def test_normalize_event_reads_both_field_spellings():
    """ZCode writes snake_case with camelCase aliases alongside."""
    snake = normalize_event(json.dumps(
        {"session_id": "abc", "cwd": "E:/repo", "hook_event_name": "SessionStart"}))
    assert snake["cwd"] == "E:/repo"
    assert snake["native_session_id"] == "abc"

    camel = normalize_event(json.dumps({"sessionId": "xyz", "cwd": "E:/repo"}))
    assert camel["native_session_id"] == "xyz"


def test_normalize_event_degrades_without_crashing():
    garbage = normalize_event("not json {{{")
    assert garbage["native_session_id"] is None
    assert garbage["cwd"]  # falls back to the process cwd
    assert normalize_event("")["native_session_id"] is None
    assert normalize_event('["a", "list"]')["native_session_id"] is None


def test_tiered_context_reaches_the_zcode_envelope(
        repo_store, tmp_path, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo)

    result = _run(monkeypatch, {"session_id": "sess-native-1", "cwd": repo,
                                "hook_event_name": "SessionStart",
                                "source": "startup"}, store=store)

    assert result["status"] == "context_ready", result.get("message")
    ctx = result["context"]
    assert ctx.startswith("format: tiered-v1")
    for section in ("[L0 Thread State]", "[Runtime State]",
                    "[Historical Evidence]", "[L1 Active Working Context]"):
        assert section in ctx

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit(result) == 0
    payload = json.loads(out.getvalue())          # pure JSON or this raises
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert payload["hookSpecificOutput"]["additionalContext"] == ctx
    out.getvalue().encode("ascii")                # Chinese rides escaped


def test_the_handler_runs_for_the_zcode_provider(repo_store, tmp_path, monkeypatch):
    """The shared core must be told `zcode`, not a default."""
    store, repo = repo_store
    _seed(store, tmp_path, repo)
    result = _run(monkeypatch, {"cwd": repo}, store=store)
    assert result["status"] == "context_ready"
    assert result["thread"]["id"].startswith("thr_")


def test_native_session_id_is_reported_back(repo_store, tmp_path, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo)
    result = _run(monkeypatch, {"session_id": "sess-native-9", "cwd": repo},
                  store=store)
    assert result["status"] == "context_ready"
    assert result["native_session_id"] == "sess-native-9"
    assert "attach_status" in result


def test_no_thread_emits_nothing_and_exits_zero(tmp_path, monkeypatch):
    store = Store(tmp_path / "empty.db")
    try:
        result = _run(monkeypatch, {"session_id": "s", "cwd": str(tmp_path)},
                      store=store)
        assert result["status"] == "no_thread"
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        assert emit(result) == 0
        assert out.getvalue() == ""
    finally:
        store.close()


def test_fail_open_on_garbage_payload_and_unknown_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json {{{"))
    result = handle_zcode_session_start(cwd=str(tmp_path))
    assert result["status"] in ("no_thread", "error")

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit(result) == 0                    # never raises, always 0


def test_no_thread_preserves_correlation_id_for_emit(tmp_path, monkeypatch):
    """Regression: correlation_id must survive no_thread early return."""
    from voyager.integrations.zcode_session_start import handle_zcode_session_start
    
    # Run with bogus input → likely no_thread/error outcome
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json {{{"))
    result = handle_zcode_session_start(cwd=str(tmp_path))
    
    if result["status"] not in ("no_thread", "error"):
        pytest.skip(f"Got {result['status']} instead of expected path")
    
    # CRITICAL: correlation_id MUST be present for emit() to work
    assert "_verification_correlation_id" in result, \
        "emit() needs correlation_id to record transport success"
    
    correlation_id = result["_verification_correlation_id"]
    assert isinstance(correlation_id, str), "Must be string ID"
    assert len(correlation_id) > 0, "Cannot be empty"
    
    # Emit should succeed (return 0) even with no context  
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit(result) == 0


def test_context_ready_is_logged(tmp_path, repo_store, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo)
    logdir = tmp_path / "logs"
    monkeypatch.setenv("VOYAGER_LOG_DIR", str(logdir))

    result = _run(monkeypatch, {"session_id": "sess-native-log", "cwd": repo},
                  store=store)
    assert result["status"] == "context_ready"

    log = logdir / "zcode-hooks.jsonl"
    assert log.exists(), "the hook must leave a trace even when it succeeds"
    events = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l]
    ready = [e for e in events if e.get("event") == "context_ready"]
    assert ready, events
    assert ready[-1]["thread_id"].startswith("thr_")
    assert ready[-1]["context_chars"] > 0


def test_emit_never_writes_a_partial_envelope(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit({"status": "no_thread"}) == 0
    assert out.getvalue() == ""
    assert emit({"status": "error", "message": "boom"}) == 0
    assert out.getvalue() == ""
