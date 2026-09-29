"""Codex SessionStart handler tests (G3-B).

The handler is a thin shell over startup_continuity: payload normalisation in,
tiered-v1 envelope out, fail-open everywhere.  These tests pin exactly that
boundary — the shared core is covered by the startup/cache/continuity suites.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from voyager.integrations.codex_session_start import (
    emit,
    handle_codex_session_start,
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
    tid = store.thread_create(repo_root=repo, title="tiered codex",
                              goal="continue across agents")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    sess = new_session(id="codex:seed", provider="codex",
                       native_session_id="seed", title="seeded",
                       started_at=1.0, updated_at=2.0,
                       repo_root=repo, cwd=repo)
    store.replace_session(
        sess,
        [new_event(sid="codex:seed", seq=1, kind="user", ts=1.0, content=content),
         new_event(sid="codex:seed", seq=2, kind="assistant", ts=2.0,
                   content="state so far")],
        "codex", src)
    store.thread_attach(tid, "codex:seed")
    return tid


def _run(monkeypatch, payload, store=None, cwd=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return handle_codex_session_start(cwd=cwd, store=store)


def test_normalize_event_degrades_without_crashing():
    good = normalize_event(json.dumps(
        {"session_id": "abc", "cwd": "E:/repo", "hook_event_name": "SessionStart"}))
    assert good == {"cwd": "E:/repo", "native_session_id": "abc"}
    garbage = normalize_event("not json {{{")
    assert garbage["native_session_id"] is None
    assert garbage["cwd"]  # falls back to the process cwd
    assert normalize_event("")["native_session_id"] is None


def test_tiered_context_reaches_the_codex_envelope(
        repo_store, tmp_path, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo)

    result = _run(monkeypatch, {"session_id": "sess-native-1", "cwd": repo},
                  store=store)

    assert result["status"] == "context_ready", result.get("message")
    ctx = result["context"]
    assert ctx.startswith("format: tiered-v1")
    assert "[L0 Thread State]" in ctx
    assert "[Runtime State]" in ctx
    assert "[Historical Evidence]" in ctx
    assert "[L1 Active Working Context]" in ctx
    assert "# Continuation Bundle" not in ctx

    # the envelope is the Claude-compatible shape, emitted as pure ASCII
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit(result) == 0
    emitted = out.getvalue()
    payload = json.loads(emitted)  # would raise if it were not pure JSON
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert payload["hookSpecificOutput"]["additionalContext"] == ctx
    emitted.encode("ascii")  # the Chinese context rides losslessly escaped


def test_native_session_id_feeds_the_identity_pending(
        repo_store, tmp_path, monkeypatch):
    """The hook knows the native session id, so the pending row is an
    identity match -- exactly what the MCP fallback could not provide."""
    store, repo = repo_store
    tid = _seed(store, tmp_path, repo)

    result = _run(monkeypatch, {"session_id": "sess-native-7", "cwd": repo},
                  store=store)

    assert result["status"] == "context_ready"
    assert result["attach_status"] == "pending_resolve"
    assert result["native_session_id"] == "sess-native-7"
    rows = store.pending_open(thread_id=tid)
    assert len(rows) == 1
    assert rows[0]["provider"] == "codex"
    assert rows[0]["native_session_id"] == "sess-native-7"


def test_fail_open_on_garbage_and_unknown_repo(tmp_path, monkeypatch):
    """Malformed payload + repo with no WorkThread: no crash, no context,
    silent protocol output -- a hook must never break a Codex session."""
    bogus = str(tmp_path / "nowhere")
    result = _run(monkeypatch, "not json {{{", cwd=bogus)
    assert result["status"] == "no_thread"
    assert "context" not in result or not result.get("context")

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit(result) == 0
    assert out.getvalue() == ""  # silent: nothing to inject


def test_context_ready_is_logged_and_log_stays_bounded(
        repo_store, tmp_path, monkeypatch):
    store, repo = repo_store
    _seed(store, tmp_path, repo)
    log = tmp_path / "logs" / "codex-hooks.jsonl"
    monkeypatch.setenv("VOYAGER_LOG_DIR", str(tmp_path / "logs"))

    _run(monkeypatch, {"session_id": "sess-native-2", "cwd": repo}, store=store)

    lines = log.read_text(encoding="utf-8").splitlines()
    events = [json.loads(l)["event"] for l in lines if l.strip()]
    assert "context_ready" in events
    assert all(len(l) < 4000 for l in lines)
    assert time.time() - json.loads(lines[-1])["ts"] < 60


# --- non-ASCII hook cwd: the rollout is authoritative ------------------------

def _fake_codex_home(tmp_path, session_id, cwd):
    """Lay out ~/.codex/sessions/<Y>/<M>/<D>/rollout-<ts>-<sid>.jsonl."""
    root = tmp_path / "codex-home"
    day = root / "sessions" / "2026" / "09" / "29"
    day.mkdir(parents=True, exist_ok=True)
    f = day / ("rollout-2026-09-29T13-00-00-%s.jsonl" % session_id)
    f.write_text(
        json.dumps({"type": "session_meta",
                    "payload": {"id": session_id, "cwd": cwd,
                                "cli_version": "0.155.0"}}) + "\n",
        encoding="utf-8")
    return root


def test_canonical_cwd_comes_from_the_rollout(tmp_path, monkeypatch):
    from voyager.integrations.codex_session_start import canonical_cwd_from_rollout

    sid = "01a0e7a2-6b1f-7011-ad65-103e17fb1094"
    monkeypatch.setenv("CODEX_HOME", str(_fake_codex_home(tmp_path, sid, "E:/work/项目")))
    assert canonical_cwd_from_rollout(sid) == "E:/work/项目"


def test_canonical_cwd_is_none_without_a_session_or_rollout(tmp_path, monkeypatch):
    from voyager.integrations.codex_session_start import canonical_cwd_from_rollout

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    assert canonical_cwd_from_rollout(None) is None
    assert canonical_cwd_from_rollout("") is None
    assert canonical_cwd_from_rollout("does-not-exist") is None


def test_a_mangled_payload_cwd_is_overridden_by_the_rollout(tmp_path, monkeypatch):
    """The whole point: a non-ASCII cwd must come from Codex, not from the payload."""
    import io as _io
    from voyager.integrations.codex_session_start import handle_codex_session_start

    sid = "01a0e7a2-6b1f-7011-ad65-103e17fb1094"
    monkeypatch.setenv("CODEX_HOME", str(_fake_codex_home(tmp_path, sid, "E:/work/项目")))
    monkeypatch.setattr(sys, "stdin", _io.StringIO(json.dumps(
        {"session_id": sid, "cwd": "E:/work/??", "hook_event_name": "SessionStart"})))

    store = Store(tmp_path / "hook2.db")
    try:
        result = handle_codex_session_start(store=store)
        # no thread exists for that repo, but the *resolved* cwd is what matters
        assert result["status"] == "no_thread"
    finally:
        store.close()
    # the resolver itself is the assertion that counts
    from voyager.integrations.codex_session_start import canonical_cwd_from_rollout
    assert canonical_cwd_from_rollout(sid) == "E:/work/项目"


def test_an_explicit_cwd_still_wins(tmp_path, monkeypatch):
    from voyager.integrations.codex_session_start import handle_codex_session_start

    sid = "01a0e7a2-6b1f-7011-ad65-103e17fb1094"
    monkeypatch.setenv("CODEX_HOME", str(_fake_codex_home(tmp_path, sid, "E:/work/项目")))
    store = Store(tmp_path / "hook3.db")
    try:
        result = handle_codex_session_start(cwd=str(tmp_path), stdin_raw="{}", store=store)
        assert result["status"] == "no_thread"
    finally:
        store.close()


def test_rollout_without_cwd_is_not_guessed(tmp_path, monkeypatch):
    """A rollout that does not record a cwd must not be invented."""
    from voyager.integrations.codex_session_start import canonical_cwd_from_rollout

    root = tmp_path / "codex-home"
    day = root / "sessions" / "2026" / "09" / "29"
    day.mkdir(parents=True)
    (day / "rollout-2026-09-29T13-00-00-sess-x.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "sess-x"}}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(root))
    assert canonical_cwd_from_rollout("sess-x") is None
