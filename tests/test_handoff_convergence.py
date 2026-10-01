"""P9: handoff convergence.

`switch`, `continue`, `handoff` and `merge` are four dialects of ONE engine
(`continuity.handoff_thread`).  Before the convergence each carried its own
copy of the pipeline, so the *same* WorkThread behaved differently depending
on which command you typed:

* `switch T --to X` leased the thread and recorded a pending attach;
  `continue --thread T --to X` did neither — two agents could write the same
  thread concurrently, and the target agent's new session was never adopted
  by a later scan;
* `continue --thread T --to X` ignored D7 and always compiled a bundle, even
  when the thread already had a resumable member of that provider;
* `switch --bundle` was parsed but never handed to the engine, so forcing a
  bundle was silently a no-op;
* `switch <agent>` without `--thread` raised UnboundLocalError: the candidate
  comprehension's `t` never escapes in Python 3, so `thread=t` was unbound.

These tests pin the converged behaviour, plus the invariants that make the
engine safe to share with MCP: it must be **mute** (JSON-RPC rides stdout),
must return errors as values instead of raising, and must **never** create a
WorkThread as a side effect.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from voyager.cli import main
from voyager.store import Store


def _add(store, src, sid, prov, i, can_resume):
    s = {"id": sid, "provider": prov, "native_session_id": sid.split(":")[-1],
         "title": "work {0}".format(i),
         "started_at": 1000.0 + i, "updated_at": 2000.0 + i,
         "cwd": "E:/proj/demo", "repo_root": "E:/proj/demo",
         "message_count": 2, "tool_count": 1,
         "can_resume": can_resume, "can_fork": False,
         "resume_cmd": ("codex resume " + sid.split(":")[-1]) if can_resume else None,
         "metadata": {}, "raw_metadata": {}}
    store.replace_session(
        s, [{"sid": sid, "ts": 2000.0 + i, "seq": 0, "kind": "user",
             "content": "conv work {0}".format(i)}], prov, src)


@pytest.fixture
def hb(tmp_path):
    """(db_path, thread_id, store) — a thread with a resumable codex member."""
    db = tmp_path / "hb.db"
    store = Store(db)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    _add(store, src, "codex:m0", "codex", 0, can_resume=True)
    _add(store, src, "claude:m1", "claude", 1, can_resume=False)
    tid = store.thread_create(repo_root="E:/proj/demo", title="the task")
    for sid in ("codex:m0", "claude:m1"):
        store.thread_attach(tid, sid)
    yield str(db), tid, store
    store.close()


@pytest.fixture
def solo(tmp_path):
    """(db_path, store) — one session that belongs to NO WorkThread."""
    db = tmp_path / "solo.db"
    store = Store(db)
    src = tmp_path / "solo.jsonl"
    src.write_text("{}", encoding="utf-8")
    _add(store, src, "codex:solo", "codex", 0, can_resume=False)
    yield str(db), store
    store.close()


# ---------------------------------------------------------------------------
# continue --thread: the same safety as switch
# ---------------------------------------------------------------------------

def test_continue_thread_now_leases_and_records_pending(hb, tmp_path):
    """Before P9 `continue --thread T --to X` did neither, so the WorkThread
    had no single writer and the target session was never auto-adopted."""
    db, tid, store = hb
    rc = main(["--db", db, "continue", "--thread", tid, "--to", "claude",
               "--no-launch", "-o", str(tmp_path / "b.md")])
    assert rc == 0
    lease = store.thread_lease_get(tid)
    assert lease is not None, "continue must take the single-writer lease"
    assert lease["holder"] == "claude"
    pend = store.thread_pending_list(tid)
    assert len(pend) == 1 and pend[0]["provider"] == "claude"


def test_continue_thread_refuses_a_live_foreign_lease(hb, tmp_path):
    db, tid, store = hb
    assert store.thread_lease_acquire(tid, "grok")[0]
    rc = main(["--db", db, "continue", "--thread", tid, "--to", "claude",
               "--no-launch", "-o", str(tmp_path / "b.md")])
    assert rc == 1
    assert store.thread_lease_get(tid)["holder"] == "grok"


def test_continue_thread_same_provider_native_resumes_like_switch(hb, capsys):
    """D7 applies to `continue` too: a resumable member of the target
    provider is resumed, not turned into a bundle."""
    db, tid, store = hb
    rc = main(["--db", db, "continue", "--thread", tid, "--to", "codex",
               "--no-launch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "$ codex resume m0" in out
    assert "continuation bundle" not in out


def test_continue_thread_bundle_flag_beats_native_resume(hb, tmp_path, capsys):
    db, tid, store = hb
    out = tmp_path / "b.md"
    rc = main(["--db", db, "continue", "--thread", tid, "--to", "codex",
               "--bundle", "--no-launch", "-o", str(out)])
    assert rc == 0
    assert out.is_file()
    assert "$ codex resume m0" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# switch: two regressions the convergence exposed
# ---------------------------------------------------------------------------

def test_switch_bundle_flag_is_honoured(hb, tmp_path, capsys):
    """`--bundle` was parsed but never passed to the engine, so forcing a
    bundle for a same-provider member did nothing."""
    db, tid, store = hb
    out = tmp_path / "b.md"
    rc = main(["--db", db, "switch", "codex", "--thread", tid, "--bundle",
               "--no-launch", "-o", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "continuation bundle:" in printed
    assert "$ codex resume" not in printed
    assert out.is_file()


def test_switch_without_thread_resolves_the_repo_thread(hb, capsys):
    """`switch <agent>` (no --thread) used to raise UnboundLocalError."""
    db, tid, store = hb
    rc = main(["--db", db, "switch", "claude", "--repo", "E:/proj/demo",
               "--no-launch"])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "active thread: {0}".format(tid) in printed
    assert store.thread_lease_get(tid)["holder"] == "claude"


# ---------------------------------------------------------------------------
# handoff / merge: same engine, so the same guarantees
# ---------------------------------------------------------------------------

def test_handoff_of_threaded_session_takes_lease_and_pending(hb, tmp_path, capsys):
    db, tid, store = hb
    out = tmp_path / "pkg.md"
    rc = main(["--db", db, "handoff", "codex:m0", "--to", "claude",
               "-o", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    # the documented package contract is unchanged...
    assert "context package:" in printed
    assert '$ claude "<handoff prompt>"' in printed
    assert out.is_file()
    # ...and the thread-level safety now applies to handoff as well
    assert store.thread_lease_get(tid)["holder"] == "claude"
    pend = store.thread_pending_list(tid)
    assert len(pend) == 1 and pend[0]["provider"] == "claude"


def test_handoff_never_creates_a_workthread(solo, tmp_path):
    """An export must not mint schema.  No thread, no lease, no pending."""
    db, store = solo
    out = tmp_path / "pkg.md"
    rc = main(["--db", db, "handoff", "codex:solo", "--to", "claude",
               "-o", str(out)])
    assert rc == 0
    assert out.is_file()
    assert store.q("SELECT COUNT(*) n FROM threads")[0]["n"] == 0
    assert store.q("SELECT COUNT(*) n FROM thread_leases")[0]["n"] == 0
    assert store.q("SELECT COUNT(*) n FROM thread_pending")[0]["n"] == 0


def test_handoff_is_an_export_not_a_resume(hb, tmp_path, capsys):
    """`--to` names the agent that READS the package.  Even when the source
    provider equals the target, handoff compiles a package instead of
    native-resuming (that is `voyager resume` / `voyager continue`)."""
    db, tid, store = hb
    out = tmp_path / "pkg.md"
    rc = main(["--db", db, "handoff", "codex:m0", "--to", "codex",
               "-o", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert out.is_file()
    assert "context package:" in printed
    assert "$ codex resume" not in printed


def test_merge_leases_the_thread_and_records_pending(synthetic_trio, tmp_path):
    store, _rows = synthetic_trio
    out = tmp_path / "b.md"
    rc = main(["--db", str(store.db_path), "merge", "sess-1", "sess-2",
               "sess-3", "--to", "claude", "-o", str(out)])
    assert rc == 0
    tids = [r["id"] for r in store.q("SELECT id FROM threads")]
    assert len(tids) == 1
    lease = store.thread_lease_get(tids[0])
    assert lease is not None and lease["holder"] == "claude"
    pend = store.thread_pending_list(tids[0])
    assert len(pend) == 1 and pend[0]["provider"] == "claude"


def test_merge_without_a_target_takes_no_lease(synthetic_trio, tmp_path):
    """Compile-only: nothing changes hands, so nothing is leased."""
    store, _rows = synthetic_trio
    rc = main(["--db", str(store.db_path), "merge", "sess-1", "sess-2",
               "-o", str(tmp_path / "b.md")])
    assert rc == 0
    assert store.q("SELECT COUNT(*) n FROM thread_leases")[0]["n"] == 0
    assert store.q("SELECT COUNT(*) n FROM thread_pending")[0]["n"] == 0


def test_unsupported_target_releases_the_lease(hb, tmp_path, capsys):
    """No launch path means nothing changed hands.  Holding the lease would
    refuse the next handoff of this thread for a whole heartbeat timeout."""
    db, tid, store = hb
    out = tmp_path / "b.md"
    rc = main(["--db", db, "continue", "--thread", tid, "--to", "cursor",
               "--no-launch", "-o", str(out)])
    assert rc == 1
    assert out.is_file(), "the file must survive for a manual paste"
    assert "not supported for 'cursor'" in capsys.readouterr().out
    assert store.thread_lease_get(tid) is None


# ---------------------------------------------------------------------------
# engine invariants (shared with MCP, where stdout is the JSON-RPC channel)
# ---------------------------------------------------------------------------

def test_engine_is_mute(hb, tmp_path, capsys):
    from voyager.continuity import handoff_thread
    db, tid, store = hb
    res = handoff_thread(store, thread=tid, target="claude",
                         output=tmp_path / "b.md")
    assert capsys.readouterr().out == "", "the engine must never print"
    assert res["action"] == "bundle"
    assert res["pending_recorded"] is True
    assert res["scope"] == "thread"


def test_engine_returns_errors_as_values(hb, capsys):
    from voyager.continuity import handoff_thread
    db, tid, store = hb

    assert handoff_thread(store, source="nope")["action"] == "error"
    assert handoff_thread(store, source="nope")["exit_code"] == 2
    assert handoff_thread(store, thread="thr_missing",
                          target="claude")["action"] == "error"
    assert handoff_thread(store, sessions=[],
                          target="claude")["action"] == "refused"

    bad = handoff_thread(store, thread=tid, target="claude", budget="banana")
    assert bad["action"] == "invalid" and bad["exit_code"] == 2
    assert store.thread_lease_get(tid) is None, (
        "an invalid budget must not leave the thread leased")

    assert capsys.readouterr().out == ""


def test_style_follows_the_shape_of_the_work(hb, tmp_path):
    """A single session has a Context Package; several have a Continuation
    Bundle.  Asking for "package" with more than one member degrades to the
    bundle, and the engine reports the style it actually used — otherwise the
    caller's wording ("context package:" vs "continuation bundle:") would
    describe a file it did not get."""
    from voyager.continuity import handoff_thread
    db, tid, store = hb
    rows = store.thread_members(tid)

    one = handoff_thread(store, sessions=rows[:1], target=None,
                         style="package", output=tmp_path / "one.md")
    assert one["style"] == "package"

    many = handoff_thread(store, sessions=rows, target=None,
                          style="package", output=tmp_path / "many.md")
    assert many["style"] == "continuation"

    explicit = handoff_thread(store, sessions=rows, target=None,
                              style="continuation", output=tmp_path / "c.md")
    assert explicit["style"] == "continuation"


def test_source_resolution_adopts_the_owning_thread(hb):
    from voyager.continuity import resolve_handoff_source
    db, tid, store = hb

    by_session = resolve_handoff_source(store, source="codex:m0")
    assert by_session["thread_id"] == tid
    assert by_session["scope"] == "thread"
    assert [m["id"] for m in by_session["members"]] == ["codex:m0"]
    assert by_session["repo_root"] == "E:/proj/demo"
    assert by_session["source_provider"] == "codex"

    by_thread = resolve_handoff_source(store, thread=tid)
    assert sorted(m["id"] for m in by_thread["members"]) == [
        "claude:m1", "codex:m0"]

    missing = resolve_handoff_source(store, source="does-not-exist")
    assert missing["error"] and missing["members"] == []


def test_dirty_tree_warning_fires_on_native_resume_too(hb, tmp_path,
                                                       monkeypatch, capsys):
    """The warning used to be computed *after* the native-resume early
    return, so a same-provider resume never mentioned a dirty tree."""
    db, tid, store = hb
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    store.con.execute("UPDATE threads SET repo_root=? WHERE id=?",
                      (str(repo).replace("\\", "/"), tid))
    store.con.commit()
    monkeypatch.chdir(repo)

    rc = main(["--db", db, "continue", "--thread", tid, "--to", "codex",
               "--no-launch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "$ codex resume m0" in out
    assert "uncommitted change(s)" in out
