"""Shared fixtures for the Voyager test suite.

Static fixtures live in ``tests/fixtures/`` and are **synthetic only** — no
real user session, path or message is used anywhere. They are text (JSON /
JSONL / SQL seeds) so they stay reviewable in diffs, and fixtures that need a
binary artifact (zstd-compressed DSH session, SQLite databases) are built
from those seeds at test time. Running the suite therefore needs no vendor
agent installed and touches nothing outside ``tmp_path``.

Each provider fixture points an adapter's module-level path globals at the
generated tree (see the ``patch_paths`` fixture) — adapters always read the
real locations, tests just redirect them.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _never_touch_the_real_index(tmp_path_factory, monkeypatch):
    """Safety net: tests must not touch the real index or the real cwd.

    The whole suite is supposed to pass ``--db``/``Store(path)`` everywhere,
    but a bug in that plumbing once let CLI tests write to (and prune!)
    ``~/.voyager/index.db``. Any `Store()` without a path now resolves to a
    throwaway file for the duration of the test, and the working directory is
    a temp dir — commands that default to a relative output path (e.g.
    `voyager continue`, whose handoff package lands in the cwd) write there
    instead of into the checkout.
    """
    import voyager.store as store_mod

    safe = tmp_path_factory.mktemp("default-index") / "index.db"
    monkeypatch.setattr(store_mod, "default_db_path", lambda: safe)
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    return safe


def _copy_tree(name: str, dest: Path) -> Path:
    """Copy tests/fixtures/<name> into <dest>/<name> and return the copy."""
    dst = dest / name
    shutil.copytree(FIXTURES / name, dst)
    return dst


def _seed_sqlite(seed: str, db_path: Path) -> Path:
    """Build a SQLite fixture from a committed text seed script."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript((FIXTURES / seed).read_text(encoding="utf-8"))
        con.commit()
    finally:
        con.close()
    return db_path


# ---------------------------------------------------------------------------
# adapter plumbing
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter_of():
    """Resolve a registered adapter by provider name."""
    from voyager.adapters import load_all
    from voyager.adapters.base import get_adapter

    def _load(name: str):
        load_all()
        ad = get_adapter(name)
        assert ad is not None, f"adapter {name!r} not registered"
        return ad

    return _load


@pytest.fixture
def patch_paths(monkeypatch):
    """Redirect an adapter module's path globals at a fixture tree.

    The globals live on the adapter *module* (``voyager.adapters.codex``),
    not on the adapter instance.
    """
    import importlib

    def _patch(ad, **values):
        mod = importlib.import_module(type(ad).__module__)
        for key, value in values.items():
            assert hasattr(mod, key), f"{mod.__name__} has no global {key!r}"
            monkeypatch.setattr(mod, key, value)

    return _patch


# ---------------------------------------------------------------------------
# codex — rollout JSONL
# ---------------------------------------------------------------------------

@pytest.fixture
def codex_fixture(tmp_path):
    return _copy_tree("codex", tmp_path) / "sessions"


# ---------------------------------------------------------------------------
# claude — project JSONL (+ file-history snapshot rows)
# ---------------------------------------------------------------------------

@pytest.fixture
def claude_fixture(tmp_path):
    return _copy_tree("claude", tmp_path)


# ---------------------------------------------------------------------------
# dsh — zstd-compressed JSONL (compressed from the committed .jsonl source)
# ---------------------------------------------------------------------------

@pytest.fixture
def dsh_fixture(tmp_path):
    zstd = pytest.importorskip("zstandard", reason="dsh extra not installed")
    root = tmp_path / "dsh" / "sessions"
    src = (FIXTURES / "dsh" / "sessions" / "--E--proj--demo--" /
           "session-dsh-1" / "session.jsonl")
    session_dir = root / "--E--proj--demo--" / "session-dsh-1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl.zstd").write_bytes(
        zstd.ZstdCompressor().compress(src.read_bytes()))
    return root


# ---------------------------------------------------------------------------
# zcode — SQLite CLI store
# ---------------------------------------------------------------------------

@pytest.fixture
def zcode_fixture(tmp_path):
    return _seed_sqlite("zcode/seed.sql",
                        tmp_path / "zcode" / "cli" / "db" / "db.sqlite")


# ---------------------------------------------------------------------------
# grok — chat_history.jsonl + summary.json
# ---------------------------------------------------------------------------

@pytest.fixture
def grok_fixture(tmp_path):
    return _copy_tree("grok", tmp_path) / "sessions"


# ---------------------------------------------------------------------------
# cursor — SQLite key-value store
# ---------------------------------------------------------------------------

@pytest.fixture
def cursor_fixture(tmp_path):
    return _seed_sqlite("cursor/seed.sql", tmp_path / "cursor" / "state.vscdb")


# ---------------------------------------------------------------------------
# kiro — workspace-session JSON under %APPDATA%/Kiro/...
# ---------------------------------------------------------------------------

@pytest.fixture
def kiro_fixture(tmp_path, monkeypatch):
    agent_dir = (tmp_path / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent")
    shutil.copytree(FIXTURES / "kiro", agent_dir)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return agent_dir


# ---------------------------------------------------------------------------
# antigravity — conversation SQLite with protobuf-ish step payloads
# ---------------------------------------------------------------------------

@pytest.fixture
def antigravity_fixture(tmp_path):
    d = tmp_path / "antigravity" / "conversations"
    _seed_sqlite("antigravity/seed.sql", d / "ag-1.db")
    return d


# ---------------------------------------------------------------------------
# a populated index (adapters not involved) — export / handoff / CLI tests
# ---------------------------------------------------------------------------

CODEX_NATIVE = "11111111-2222-3333-4444-555555555555"
CODEX_SID = f"codex:{CODEX_NATIVE}"
ZCODE_SID = "zcode:sess_z9"


@pytest.fixture
def indexed_store(tmp_path):
    """A real Store holding two synthetic sessions.

    One resumable codex session (full event mix: user / reasoning / tool_call
    / tool_result / error / assistant) and one ZCode session with no native
    resume path — enough to exercise export, handoff, continue and the CLI.
    """
    from voyager.model import new_event, new_session
    from voyager.store import Store

    store = Store(tmp_path / "index.db")

    src_a = tmp_path / "rollout-codex.jsonl"
    src_a.write_text("{}\n", encoding="utf-8")
    a = new_session(
        id=CODEX_SID, provider="codex", native_session_id=CODEX_NATIVE,
        title="fix the parser", started_at=1757000000.0, updated_at=1757000100.0,
        cwd="E:/proj/demo", repo_root="E:/proj/demo",
        git_remote="git@github.com:u/demo.git", git_branch="main",
        git_commit="abc1234def5678", model="gpt-5-codex",
        message_count=2, tool_count=1, can_resume=True, can_fork=True,
        resume_cmd=f"codex resume {CODEX_NATIVE}",
        metadata={"usage_totals": {"input": 100, "output": 20}},
        raw_metadata={"originator": "codex_vscode"},
    )
    events_a = [
        new_event(sid=CODEX_SID, ts=1757000000.0, seq=1, kind="user", role="user",
                  content="fix the parser"),
        new_event(sid=CODEX_SID, ts=1757000010.0, seq=2, kind="reasoning",
                  role="assistant", content="look at parser.py"),
        new_event(sid=CODEX_SID, ts=1757000020.0, seq=3, kind="tool_call",
                  role="assistant", tool_name="shell_command", tool_call_id="c1",
                  command="python -m pytest -q",
                  tool_input='{"command":"python -m pytest -q"}'),
        new_event(sid=CODEX_SID, ts=1757000030.0, seq=4, kind="tool_result",
                  role="tool", tool_call_id="c1", tool_output="all good",
                  exit_code=0),
        new_event(sid=CODEX_SID, ts=1757000040.0, seq=5, kind="error",
                  role="system", content="flaky network call"),
        new_event(sid=CODEX_SID, ts=1757000050.0, seq=6, kind="assistant",
                  role="assistant", content="parser fixed",
                  file_path="E:/proj/demo/parser.py"),
    ]
    store.replace_session(a, events_a, "codex", src_a)

    src_b = tmp_path / "db.sqlite"
    src_b.write_bytes(b"")
    b = new_session(
        id=ZCODE_SID, provider="zcode", native_session_id="sess_z9",
        title="装修 README", started_at=1757000000.0, updated_at=1757000000.0,
        cwd="E:/code/voyager", repo_root="E:/code/voyager",
        model="anthropic:claude-sonnet", message_count=1, tool_count=0,
        can_resume=False, resume_cmd=None,
    )
    events_b = [
        new_event(sid=ZCODE_SID, ts=1757000000.0, seq=1, kind="user", role="user",
                  content="给 README 加个架构图"),
    ]
    store.replace_session(b, events_b, "zcode", src_b)

    yield store
    store.close()


@pytest.fixture
def codex_row(indexed_store):
    """The synthetic codex session row (resumable, full event mix)."""
    row, ambiguous = indexed_store.session(CODEX_SID)
    assert row is not None and not ambiguous, "codex fixture row missing"
    return row


@pytest.fixture
def zcode_row(indexed_store):
    """The synthetic ZCode session row (no native resume path)."""
    row, ambiguous = indexed_store.session(ZCODE_SID)
    assert row is not None and not ambiguous, "zcode fixture row missing"
    return row
