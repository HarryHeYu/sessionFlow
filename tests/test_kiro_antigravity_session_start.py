"""Kiro and Antigravity hook handler tests.

Both providers diverge from the Claude-compatible envelope in ways that fail
silently, so each divergence is pinned here:

  * Kiro: a command action's **stdout is the context** — plain text, no envelope
  * Antigravity: there is **no session-start event**; context reaches the
    conversation only through ``injectSteps`` on ``PreInvocation``, which fires
    before *every* model call, so it must inject exactly once (``invocationNum`` 0)
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from voyager.integrations import antigravity_session_start as ag
from voyager.integrations import kiro_session_start as ki
from voyager.integrations.hook_payload import MAX_ADDITIONAL_CONTEXT_CHARS, payload_len
from voyager.model import new_event, new_session
from voyager.store import Store


@pytest.fixture
def repo_store(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "workrepo")],
                   capture_output=True, timeout=30)
    repo = str(tmp_path / "workrepo")
    store = Store(tmp_path / "hook.db")
    yield store, repo
    store.close()


def _seed(store, tmp_path, repo, provider, content="把测试标记写进上下文"):
    tid = store.thread_create(repo_root=repo, title="tiered %s" % provider,
                              goal="continue across agents")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    sid = "%s:seed" % provider
    sess = new_session(id=sid, provider=provider, native_session_id="seed",
                       title="seeded", started_at=1.0, updated_at=2.0,
                       repo_root=repo, cwd=repo)
    store.replace_session(
        sess,
        [new_event(sid=sid, seq=1, kind="user", ts=1.0, content=content),
         new_event(sid=sid, seq=2, kind="assistant", ts=2.0, content="state")],
        provider, src)
    store.thread_attach(tid, sid)
    return tid


# --- Kiro ------------------------------------------------------------------

def test_kiro_cwd_is_the_process_or_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ki.normalize_event("{}")["cwd"] == str(tmp_path)
    p = ki.normalize_event(json.dumps({"workspacePaths": ["E:/repo", "E:/other"]}))
    assert p["cwd"] == "E:/repo"


def test_kiro_normalize_degrades_without_crashing():
    assert ki.normalize_event("not json {{{")["native_session_id"] is None
    assert ki.normalize_event("")["native_session_id"] is None
    assert ki.normalize_event('["a"]')["native_session_id"] is None
    assert ki.normalize_event(json.dumps({"sessionId": "x"}))["native_session_id"] == "x"


def test_kiro_emits_plain_text_not_an_envelope(repo_store, tmp_path, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo, "kiro")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"workspacePaths": [repo]})))

    result = ki.handle_kiro_session_start(store=store)
    assert result["status"] == "context_ready", result.get("message")

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ki.emit(result) == 0
    emitted = out.getvalue()
    assert emitted.startswith("format: tiered-v1"), emitted[:60]
    assert not emitted.lstrip().startswith("{"), "Kiro takes raw stdout, not JSON"
    for section in ("[L0 Thread State]", "[Runtime State]",
                    "[Historical Evidence]", "[L1 Active Working Context]"):
        assert section in emitted


def test_kiro_no_thread_emits_nothing_and_exits_zero(tmp_path, monkeypatch):
    store = Store(tmp_path / "e.db")
    try:
        result = ki.handle_kiro_session_start(cwd=str(tmp_path), stdin_raw="{}", store=store)
        assert result["status"] == "no_thread"
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        assert ki.emit(result) == 0
        assert out.getvalue() == ""
    finally:
        store.close()


def test_kiro_caps_a_huge_context(monkeypatch, tmp_path):
    monkeypatch.setenv("VOYAGER_CONTEXT_DIR", str(tmp_path / "spill"))
    huge = "k" * (MAX_ADDITIONAL_CONTEXT_CHARS * 2)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ki.emit({"status": "context_ready", "context": huge}) == 0
    assert payload_len(out.getvalue()) <= MAX_ADDITIONAL_CONTEXT_CHARS


def test_kiro_context_ready_is_logged(tmp_path, repo_store, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo, "kiro")
    logdir = tmp_path / "logs"
    monkeypatch.setenv("VOYAGER_LOG_DIR", str(logdir))
    result = ki.handle_kiro_session_start(cwd=repo, stdin_raw="{}", store=store)
    assert result["status"] == "context_ready"
    events = [json.loads(l) for l in
              (logdir / "kiro-hooks.jsonl").read_text(encoding="utf-8").splitlines() if l]
    assert any(e.get("event") == "context_ready" for e in events), events


# --- Antigravity -----------------------------------------------------------

def test_antigravity_cwd_comes_from_workspace_paths():
    p = ag.normalize_event(json.dumps({"workspacePaths": ["E:/repo", "E:/x"],
                                       "conversationId": "c-1", "invocationNum": 0}))
    assert p["cwd"] == "E:/repo"
    assert p["native_session_id"] == "c-1"
    assert p["invocation_num"] == 0


def test_antigravity_normalize_degrades_without_crashing():
    assert ag.normalize_event("not json {{{", cwd="E:/r")["native_session_id"] is None
    assert ag.normalize_event("", cwd="E:/r")["invocation_num"] is None
    # a non-int invocation number is not trusted
    assert ag.normalize_event(json.dumps({"invocationNum": "3"}),
                              cwd="E:/r")["invocation_num"] is None
    assert ag.normalize_event(json.dumps({"invocationNum": True}),
                              cwd="E:/r")["invocation_num"] is None


def test_antigravity_emits_inject_steps(repo_store, tmp_path, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo, "antigravity")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"workspacePaths": [repo], "conversationId": "c-9", "invocationNum": 0})))

    result = ag.handle_antigravity_pre_invocation(store=store)
    assert result["status"] == "context_ready", result.get("message")

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ag.emit(result) == 0
    payload = json.loads(out.getvalue())
    assert list(payload) == ["injectSteps"], payload.keys()
    step = payload["injectSteps"][0]
    assert list(step) == ["ephemeralMessage"], "context rides as a system message"
    assert step["ephemeralMessage"].startswith("format: tiered-v1")


def test_antigravity_injects_only_on_the_first_invocation(repo_store, tmp_path, monkeypatch):
    """PreInvocation fires before every model call; repeating the window would
    flood the conversation."""
    store, repo = repo_store
    _seed(store, tmp_path, repo, "antigravity")
    result = ag.handle_antigravity_pre_invocation(
        store=store, stdin_raw=json.dumps({"workspacePaths": [repo], "invocationNum": 3}))
    assert result["status"] == "skipped"
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ag.emit(result) == 0
    assert out.getvalue() == ""


def test_antigravity_absent_invocation_num_is_treated_as_first(repo_store, tmp_path):
    store, repo = repo_store
    _seed(store, tmp_path, repo, "antigravity")
    result = ag.handle_antigravity_pre_invocation(
        store=store, stdin_raw=json.dumps({"workspacePaths": [repo]}))
    assert result["status"] == "context_ready"


def test_antigravity_caps_a_huge_context(monkeypatch, tmp_path):
    monkeypatch.setenv("VOYAGER_CONTEXT_DIR", str(tmp_path / "spill"))
    huge = "a" * (MAX_ADDITIONAL_CONTEXT_CHARS * 2)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ag.emit({"status": "context_ready", "context": huge}) == 0
    msg = json.loads(out.getvalue())["injectSteps"][0]["ephemeralMessage"]
    assert payload_len(msg) <= MAX_ADDITIONAL_CONTEXT_CHARS


def test_both_handlers_fail_open_on_garbage(monkeypatch, tmp_path):
    for mod, handler in ((ki, ki.handle_kiro_session_start),
                         (ag, ag.handle_antigravity_pre_invocation)):
        result = handler(cwd=str(tmp_path), stdin_raw="not json {{{",
                         store=Store(tmp_path / ("g-%s.db" % mod.PROVIDER)))
        assert result["status"] in ("no_thread", "error", "skipped")
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        assert mod.emit(result) == 0
