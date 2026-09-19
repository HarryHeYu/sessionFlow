"""Phase E/F MCP continuity surface + provider startup matrix tests.

Contracts:
- voyager_current: returns thread/lease/action for the active WorkThread
- voyager_context: compiles a provenance-bound bundle for the caller
- voyager_continue: automatic discovery + compile, no manual session ids
- voyager_switch: same-provider → native resume hint; cross-provider →
  bundle; unknown → error
- SKILL.md contains the startup protocol (discovery before responding)
- provider startup matrix documented (codex/claude/grok = skill+MCP;
  zcode/kiro/antigravity = best-effort)
"""

from __future__ import annotations

import pytest

mcp_server = pytest.importorskip("voyager.mcp_server")

pytestmark = pytest.mark.skipif(
    not mcp_server.MCP_AVAILABLE, reason="mcp extra not installed"
)


class StoreProxy:
    """Hand the server the shared fixture store without closing it."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, item):
        return getattr(self._real, item)

    def close(self):
        pass


@pytest.fixture
def continuity_store(tmp_path, monkeypatch):
    from voyager.store import Store as RealStore

    real = RealStore(tmp_path / "c.db")
    monkeypatch.setattr(mcp_server, "Store",
                        lambda *a, **kw: StoreProxy(real))
    from voyager.model import new_event, new_session

    (tmp_path / "s.jsonl").write_text("{}", encoding="utf-8")
    s = new_session(id="codex:cc1", provider="codex", native_session_id="cc1",
                    title="continuity work", started_at=1000.0,
                    updated_at=2000.0, cwd="E:/proj/demo",
                    repo_root="E:/proj/demo", message_count=1,
                    can_resume=True, resume_cmd="codex resume cc1")
    real.replace_session(s, [new_event(sid=s["id"], ts=2000.0, seq=0,
                                       kind="user",
                                       content="continuity test work")],
                         "codex", tmp_path / "s.jsonl")
    real.thread_create("E:/proj/demo", "the task")
    tid = real.thread_list("active")[0]["id"]
    real.thread_attach(tid, s["id"])
    yield real, tid
    real.close()


def test_current_returns_thread(continuity_store):
    store, tid = continuity_store
    r = mcp_server.voyager_current(cwd="E:/proj/demo")
    assert "the task" in r
    assert "codex:cc1" in r


def test_current_no_thread(continuity_store):
    r = mcp_server.voyager_current(cwd="E:/nowhere")
    assert "no active WorkThread" in r


def test_context_returns_bundle(continuity_store):
    r = mcp_server.voyager_context(cwd="E:/proj/demo", goal="continuity test")
    assert "continuity test work" in r
    assert "Goal" in r


def test_continue_compiles(continuity_store):
    r = mcp_server.voyager_continue(cwd="E:/proj/demo", goal="continuity test")
    assert "continuity test work" in r


def test_switch_same_provider_hint(continuity_store):
    r = mcp_server.voyager_switch(target="codex", cwd="E:/proj/demo")
    assert "native resume" in r


def test_switch_cross_provider_bundle(continuity_store):
    r = mcp_server.voyager_switch(target="claude", cwd="E:/proj/demo")
    assert "Continuation bundle ready" in r


def test_skill_startup_protocol():
    from voyager.skill import skill_source
    text = skill_source().read_text(encoding="utf-8")
    assert "Startup protocol" in text
    assert "voyager status" in text
    assert "NEVER" in text


def test_provider_startup_matrix():
    """Documented: codex/claude/grok get skill+MCP auto-discovery;
    zcode/kiro/antigravity are best-effort (no skill/MCP hook)."""
    from voyager.skill import SKILL_AGENT_ROOTS
    assert set(SKILL_AGENT_ROOTS) == {"codex", "claude", "grok"}
