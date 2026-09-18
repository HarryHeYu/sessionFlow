"""Phase 2: merge over N sessions must yield a WorkThread, and
`continue --thread` must recompile from its members."""

from __future__ import annotations

import pytest

from voyager.cli import main
from voyager.store import Store


def test_merge_creates_thread_and_continue_thread_recompiles(
        synthetic_trio, tmp_path, capsys):
    store, rows = synthetic_trio
    db = str(store.db_path)
    out = tmp_path / "bundle.md"

    rc = main(["--db", db, "merge", "sess-1", "sess-2", "sess-3",
               "-o", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "Thread thr_" in printed and "created" in printed
    assert "members:" in printed

    tid = next(w for w in printed.split() if w.startswith("thr_"))

    # thread list/show are stable
    assert main(["--db", db, "thread", "list"]) == 0
    assert main(["--db", db, "thread", "show", tid]) == 0

    # re-merge the same set -> same thread, updated (no duplicate threads)
    rc = main(["--db", db, "merge", "sess-1", "sess-2", "sess-3",
               "-o", str(tmp_path / "b2.md")])
    printed2 = capsys.readouterr().out
    assert "updated" in printed2
    assert main(["--db", db, "thread", "list"]) == 0
    listed = capsys.readouterr().out
    assert listed.count("thr_") == 1

    # continue --thread recompiles from members
    out3 = tmp_path / "b3.md"
    rc = main(["--db", db, "continue", "--thread", tid, "--to", "codex",
               "-o", str(out3)])
    assert rc == 0
    content = out3.read_text(encoding="utf-8")
    assert "Approach Y implemented" in content
    printed3 = capsys.readouterr().out
    assert f"thread {tid}" in printed3
    assert '$ codex "<continuation prompt>"' in printed3

    # member safety: sessions untouched by thread lifecycle
    assert store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 3
