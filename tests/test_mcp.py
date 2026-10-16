"""MCP server tests.

The MCP server is the "agents query Voyager themselves" surface, so the tools
must stay callable and must not blow up on an empty or populated index.
Without the optional `mcp` extra the module still imports (tools exist as
plain functions) and only *starting* the server refuses, with the install
hint — that is the packaging fix (`[mcp]` / `[all]`) made observable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

mcp_server = pytest.importorskip("voyager.mcp_server")

pytestmark = pytest.mark.skipif(
    not mcp_server.MCP_AVAILABLE, reason="mcp extra not installed"
)

TOOL_NAMES = ["voyager_brief", "voyager_search", "voyager_list", "voyager_show",
              "voyager_handoff", "voyager_merge", "voyager_thread_list",
              "voyager_thread_show", "voyager_thread_attach",
              "voyager_thread_close"]


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

    merged = mcp_server.voyager_merge(["11111111-2222", "sess_z9"], target="claude")
    assert "Continuation bundle written to" in merged
    assert "Target agent (claude) should read this file" in merged


class StoreProxy:
    """Hand the server the shared fixture store without closing it."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, item):
        return getattr(self._store, item)

    def close(self):
        pass


#: The developer's real home, captured during collection — before conftest's
#: autouse fixture redirects HOME, after which it is no longer discoverable.
#: Same trick as tests/test_isolation.py and tests/test_bundle_write.py.
REAL_HOME = Path(os.path.expanduser("~"))


def _files_under(root: Path) -> set:
    if not root.is_dir():
        return set()
    return {p for p in root.rglob("*") if p.is_file()}


def test_both_writing_tools_honour_the_bundles_dir_and_leave_home_alone(
        indexed_store, monkeypatch, tmp_path):
    """The two MCP tools that write must not reach the real home.

    `voyager_handoff` passes an explicit `output` computed from
    `get_bundles_dir()`, and `voyager_merge` lets the engine default it.  Those
    are two different code paths to the same place, and neither was covered:
    the tests above assert on the returned *text*, so a handler changed to a
    hardcoded `Path.home()` path would still pass them.  This asserts on the
    filesystem instead.
    """
    monkeypatch.setattr(mcp_server, "Store", lambda *a, **kw: StoreProxy(indexed_store))
    target = tmp_path / "mcp-bundles"
    monkeypatch.setenv("VOYAGER_BUNDLES_DIR", str(target))
    real_bundles = REAL_HOME / ".voyager" / "bundles"
    before = _files_under(real_bundles)

    handoff = mcp_server.voyager_handoff("11111111-2222", target="claude")
    merged = mcp_server.voyager_merge(["11111111-2222"], target="claude")

    written = _files_under(target)
    assert len(written) == 2, f"expected two files in the isolated dir: {written}"
    # Pull the path out of the prose without assuming a path shape: an anchored
    # `[A-Za-z]:` pattern works on Windows and finds nothing on Linux, which is
    # how this test first passed locally and failed every ubuntu job.  Start from
    # the resolved directory (the reply carries `str(out.resolve())`) and read to
    # the end of the ".md" filename.
    prefix = str(target.resolve())
    for text in (handoff, merged):
        assert "written to" in text
        assert prefix in text, f"the reply does not name the isolated dir: {text}"
        rest = text.split(prefix, 1)[1]
        m = re.search(r"\.md\b", rest)
        assert m, f"no bundle filename after the directory in: {text}"
        claimed = target / rest[:m.end()].lstrip("/\\")
        assert claimed.is_file(), f"the reply names a file that is not there: {claimed}"
        assert claimed in written, f"{claimed} is not one of the written files"

    assert _files_under(real_bundles) == before, (
        "an MCP write reached the real bundles directory: "
        f"{sorted(str(p) for p in _files_under(real_bundles) - before)}")


def test_an_uncreatable_bundles_dir_makes_the_mcp_tools_say_so(
        indexed_store, monkeypatch, tmp_path):
    """No silent fallback: the caller gets an error naming the path."""
    monkeypatch.setattr(mcp_server, "Store", lambda *a, **kw: StoreProxy(indexed_store))
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("VOYAGER_BUNDLES_DIR", str(blocker / "bundles"))
    real_bundles = REAL_HOME / ".voyager" / "bundles"
    before = _files_under(real_bundles)

    for reply in (mcp_server.voyager_handoff("11111111-2222", target="claude"),
                  mcp_server.voyager_merge(["11111111-2222"], target="claude")):
        assert "failed" in reply and "cannot create bundle directory" in reply, reply
        assert str(blocker) in reply, "the error must name the path it could not use"

    assert _files_under(real_bundles) == before, (
        "a failed MCP write fell back to the real bundles directory")

