"""Machine-derived repo scope, end to end — the surfaces #45 opted in.

Issue #31 (Q5): a sibling directory's WorkThread must not be adopted as the
current work of THIS directory. The resolver-level tests live in
``tests/test_thread_ambiguity.py``; this file drives the three surfaces the
review found needed their own proof, each against **real directories** so the
query directory and the mismatched candidate are both provable:

* ``voyager status`` (cwd = ``demo``, sibling thread registered at ``demo-old``)
* ``launcher.prelaunch`` (cwd = ``demo``)
* ``integrations.hook.startup_handler`` (cwd = ``demo``)

On pre-fix code (main @ ``6767cab``) all three adopted the sibling thread
(``READY`` / ``success``); on the fixed branch all three refuse (``NO_THREAD``
/ ``no_thread``). Pure pathlib + subprocess: valid on Windows and Linux.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from voyager.integrations.hook import startup_handler
from voyager.launcher import prelaunch
from voyager.model import new_event, new_session
from voyager.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed(db_path: Path, threads) -> None:
    """threads: (tid_suffix, title, repo, updated_at) — one session each."""
    store = Store(db_path)
    src = db_path.parent / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    for suffix, title, repo, updated in threads:
        sid = "codex:{0}".format(suffix)
        store.replace_session(
            new_session(id=sid, provider="codex", native_session_id=suffix,
                        title=title, started_at=updated - 100,
                        updated_at=updated, cwd=repo, repo_root=repo,
                        message_count=1, can_resume=True),
            [new_event(sid=sid, ts=updated - 100, seq=0, kind="user",
                       content="work on " + title)],
            "codex", src)
        tid = store.thread_create(repo_root=repo, title=title, goal=title)
        store.thread_attach(tid, sid)
        store.con.execute("UPDATE threads SET updated_at=? WHERE id=?",
                          (updated, tid))
        store.con.commit()
    store.close()


def _demo_and_sibling(tmp_path):
    """Real directories: <tmp>/demo (the query dir) and <tmp>/demo-old."""
    demo = tmp_path / "demo"
    demo_old = tmp_path / "demo-old"
    demo.mkdir()
    demo_old.mkdir()
    return demo, demo_old


def test_status_query_dir_is_demo_and_the_only_candidate_is_demo_old(
        tmp_path, capsys):
    """``status`` in demo/ with only a demo-old thread: NO_THREAD, provably.

    Both facts the fix depends on are asserted, not assumed:
    1. the queried directory IS demo (cwd is passed to a real subprocess);
    2. a candidate EXISTS and comes from demo-old (the index is queried) —
       so the refusal is the strict scope working, not an empty index.
    Loose matching is also probed in-process to document what pre-fix code
    did (it adopted the sibling), so the difference is attributable to the
    flag and not to missing data.
    """
    from voyager.auto import resolve_work_thread

    demo, demo_old = _demo_and_sibling(tmp_path)
    db = tmp_path / "index.db"
    _seed(db, [("b", "sibling work", str(demo_old), 1000.0)])

    # fact 2: a candidate exists and its repo_root is the sibling directory
    store = Store(db)
    try:
        roots = [dict(t)["repo_root"] for t in store.thread_list("active")]
    finally:
        store.close()
    assert roots == [str(demo_old)]

    # what pre-fix code did (loose): the sibling matched — documents the bug
    store = Store(db)
    try:
        resolved = resolve_work_thread(store, repo=str(demo),
                                       repo_strict=False)
        assert resolved["status"] == "success"
        assert resolved["thread"]["repo_root"] == str(demo_old)
    finally:
        store.close()

    # fact 1 + the fix: a real process, cwd = the demo directory
    r = subprocess.run([sys.executable, "-m", "voyager.cli", "status",
                        "--db", str(db), "--json"],
                       capture_output=True, text=True, timeout=120,
                       cwd=str(demo),
                       env=dict(os.environ, PYTHONPATH=str(REPO_ROOT),
                                PYTHONNOUSERSITE="1", VOYAGER_NO_SYNC="1"))
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(r.stdout)
    assert data["status"] == "NO_THREAD", data
    assert data.get("active_thread") is None
    assert data["repo_root"] == str(demo).replace("\\", "/")          # the queried dir is demo


def test_prelaunch_does_not_adopt_the_sibling_directory(tmp_path):
    """launcher.prelaunch (cwd = demo) must not compile the sibling's bundle."""
    demo, demo_old = _demo_and_sibling(tmp_path)
    db = tmp_path / "index.db"
    _seed(db, [("b", "sibling work", str(demo_old), 1000.0)])

    result = prelaunch(provider="grok", cwd=str(demo), db=str(db))
    assert result["status"] == "no_thread", result
    assert result.get("thread_id") is None, result


def test_startup_handler_does_not_adopt_the_sibling_directory(tmp_path):
    """The provider session-start hook (cwd = demo) refuses the sibling too."""
    demo, demo_old = _demo_and_sibling(tmp_path)
    db = tmp_path / "index.db"
    _seed(db, [("b", "sibling work", str(demo_old), 1000.0)])

    result = startup_handler(provider="claude", cwd=str(demo),
                             db=Path(db))
    assert result["status"] == "no_thread", result
    assert result.get("thread") is None, result
