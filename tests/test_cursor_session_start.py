"""Cursor sessionStart handler tests.

Cursor's contract differs from Claude/Codex/ZCode and these tests pin the
differences, because getting them wrong fails silently
(https://cursor.com/docs/hooks):

  * sessionStart stdin has **no `cwd`** -- the repository comes from
    ``workspace_roots`` or the ``CURSOR_PROJECT_DIR`` environment variable
  * stdout is a **top-level `additional_context`**; Cursor does not use the
    ``hookSpecificOutput`` envelope at all
  * the handler must never break a session: every path exits 0
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from voyager.integrations.cursor_session_start import (
    emit,
    handle_cursor_session_start,
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
    tid = store.thread_create(repo_root=repo, title="tiered cursor",
                              goal="continue across agents")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    sess = new_session(id="cursor:seed", provider="cursor",
                       native_session_id="seed", title="seeded",
                       started_at=1.0, updated_at=2.0,
                       repo_root=repo, cwd=repo)
    store.replace_session(
        sess,
        [new_event(sid="cursor:seed", seq=1, kind="user", ts=1.0, content=content),
         new_event(sid="cursor:seed", seq=2, kind="assistant", ts=2.0,
                   content="state so far")],
        "cursor", src)
    store.thread_attach(tid, "cursor:seed")
    return tid


def _run(monkeypatch, payload, store=None, cwd=None, env=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return handle_cursor_session_start(cwd=cwd, store=store, env=env)


# --- payload normalisation -------------------------------------------------

def test_cwd_comes_from_workspace_roots_not_from_the_process():
    """sessionStart has no `cwd`; the hook process runs in ~/.cursor/."""
    payload = normalize_event(json.dumps(
        {"session_id": "abc", "workspace_roots": ["E:/repo", "E:/other"],
         "hook_event_name": "sessionStart"}))
    assert payload["cwd"] == "E:/repo"
    assert payload["native_session_id"] == "abc"
    assert payload["workspace_roots"] == ["E:/repo", "E:/other"]


def test_project_dir_env_is_the_fallback():
    payload = normalize_event("{}", env={"CURSOR_PROJECT_DIR": "E:/from-env"})
    assert payload["cwd"] == "E:/from-env"
    # Claude's alias is honoured too, as the provider sets both
    payload = normalize_event("{}", env={"CLAUDE_PROJECT_DIR": "E:/alias"})
    assert payload["cwd"] == "E:/alias"


def test_normalize_event_degrades_without_crashing():
    garbage = normalize_event("not json {{{", env={})
    assert garbage["native_session_id"] is None
    assert garbage["cwd"]  # falls back to the process cwd
    assert normalize_event("", env={})["native_session_id"] is None
    assert normalize_event('["a", "list"]', env={})["native_session_id"] is None
    assert normalize_event(json.dumps({"workspace_roots": "not-a-list"}),
                           env={})["cwd"]


def test_composer_mode_and_background_flag_are_preserved():
    payload = normalize_event(json.dumps(
        {"session_id": "s", "composer_mode": "ask",
         "is_background_agent": True}))
    assert payload["composer_mode"] == "ask"
    assert payload["is_background_agent"] is True


# --- envelope --------------------------------------------------------------

def test_tiered_context_reaches_the_cursor_envelope(
        repo_store, tmp_path, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo)

    result = _run(monkeypatch, {"session_id": "sess-native-1",
                                "workspace_roots": [repo],
                                "hook_event_name": "sessionStart",
                                "composer_mode": "agent"}, store=store)

    assert result["status"] == "context_ready", result.get("message")
    ctx = result["context"]
    assert ctx.startswith("format: tiered-v1")
    for section in ("[L0 Thread State]", "[Runtime State]",
                    "[Historical Evidence]", "[L1 Active Working Context]"):
        assert section in ctx

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit(result) == 0
    payload = json.loads(out.getvalue())
    # Cursor's contract: top level, and NOT the Claude envelope
    assert "additional_context" in payload
    assert payload["additional_context"] == ctx
    assert "hookSpecificOutput" not in payload
    out.getvalue().encode("ascii")     # Chinese rides losslessly escaped


def test_native_session_id_is_reported_back(repo_store, tmp_path, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo)
    result = _run(monkeypatch, {"session_id": "sess-native-9",
                                "workspace_roots": [repo]}, store=store)
    assert result["status"] == "context_ready"
    assert result["native_session_id"] == "sess-native-9"
    assert "attach_status" in result


# --- fail-open -------------------------------------------------------------

def test_no_thread_emits_nothing_and_exits_zero(tmp_path, monkeypatch):
    store = Store(tmp_path / "empty.db")
    try:
        result = _run(monkeypatch, {"session_id": "s",
                                    "workspace_roots": [str(tmp_path)]},
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
    result = handle_cursor_session_start(cwd=str(tmp_path), env={})
    assert result["status"] in ("no_thread", "error")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit(result) == 0


def test_emit_never_writes_a_partial_envelope(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit({"status": "no_thread"}) == 0
    assert out.getvalue() == ""
    assert emit({"status": "error", "message": "boom"}) == 0
    assert out.getvalue() == ""


def test_context_ready_is_logged(tmp_path, repo_store, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo)
    logdir = tmp_path / "logs"
    monkeypatch.setenv("VOYAGER_LOG_DIR", str(logdir))

    result = _run(monkeypatch, {"session_id": "sess-native-log",
                                "workspace_roots": [repo],
                                "composer_mode": "agent"}, store=store)
    assert result["status"] == "context_ready"

    log = logdir / "cursor-hooks.jsonl"
    assert log.exists(), "the hook must leave a trace even when it succeeds"
    events = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l]
    ready = [e for e in events if e.get("event") == "context_ready"]
    assert ready, events
    assert ready[-1]["thread_id"].startswith("thr_")
    assert ready[-1]["context_chars"] > 0
    assert ready[-1]["composer_mode"] == "agent"
