"""MCP server tests.

The MCP server is the "agents query Voyager themselves" surface, so the tools
must stay callable and must not blow up on an empty or populated index.
Without the optional `mcp` extra the module still imports (tools exist as
plain functions) and only *starting* the server refuses, with the install
hint — that is the packaging fix (`[mcp]` / `[all]`) made observable.
"""

from __future__ import annotations

import pytest

mcp_server = pytest.importorskip("voyager.mcp_server")

pytestmark = pytest.mark.skipif(
    not mcp_server.MCP_AVAILABLE, reason="mcp extra not installed"
)

TOOL_NAMES = ["voyager_brief", "voyager_search", "voyager_list", "voyager_show",
              "voyager_handoff"]


def test_module_imports_and_exposes_tools():
    assert callable(mcp_server.main)
    for name in TOOL_NAMES:
        assert callable(getattr(mcp_server, name)), f"missing MCP tool: {name}"


def test_unavailable_server_explains_the_missing_extra(monkeypatch):
    """Without the extra the server must print the install line, not a traceback.

    Covers both entry points: `python -m voyager.mcp_server` (main) and the
    `voyager-mcp` console script (which only imports and calls main).
    """
    stub = mcp_server._UnavailableServer("voyager")
    with pytest.raises(SystemExit) as e:
        stub.run()
    message = str(e.value)
    assert "pip install" in message and "mcp" in message

    monkeypatch.setattr(mcp_server, "MCP_AVAILABLE", False)
    with pytest.raises(SystemExit) as e:
        mcp_server.main()
    assert "pip install" in str(e.value)


def test_tools_are_bound_to_the_store_fixture(indexed_store, monkeypatch):
    """Point Store() inside the server at the fixture index (not ~/.voyager)."""
    monkeypatch.setattr(mcp_server, "Store", lambda *a, **kw: StoreProxy(indexed_store))

    listed = mcp_server.voyager_list()
    assert "fix the parser" in listed and "(2 session(s) total)" in listed
    assert mcp_server.voyager_list(platform="zcode").count("session(s) total") == 1

    found = mcp_server.voyager_search("pytest")
    assert "11111111-2222-3333-4444-555555555555" in found
    assert mcp_server.voyager_search("zzz-no-such-text") == "no matches"
    shown = mcp_server.voyager_show("11111111-2222")
    assert "python -m pytest -q" in shown and "exit=0" in shown
    assert mcp_server.voyager_show("nope-nope") == "session not found: nope-nope"

    brief = mcp_server.voyager_brief(hours=1_000_000)
    assert "sessions active in the last 1000000h" in brief
    assert "last user: fix the parser" in brief


class StoreProxy:
    """Hand the server the shared fixture store without closing it."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, item):
        return getattr(self._store, item)

    def close(self):
        pass
