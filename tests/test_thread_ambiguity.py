"""Issue #17 — more than one active WorkThread for a repo is AMBIGUOUS.

Recency may suggest a default; it never establishes intent.  A caller that has
to act on a single thread must be told there is a choice and refused, not handed
the newest one as if someone had picked it.

These tests pin that at every surface a caller can reach -- the shared resolver,
the bridge ops, and the CLI -- because the defect was precisely that one surface
(``auto.discover_continuity``) got this right while the others silently took the
newest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import voyager
from voyager.api import handle_request
from voyager.auto import MAX_THREAD_CANDIDATES, resolve_work_thread
from voyager.model import new_event, new_session
from voyager.store import Store

REPO_ROOT = Path(voyager.__file__).resolve().parent.parent
REPO = "E:/proj/ambig"
OTHER = "E:/proj/other"


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
        # `Store.q()` is a bare execute and leaves the transaction open, which
        # makes the *next* replace_session fail with "cannot start a transaction
        # within a transaction".  Commit explicitly.
        store.con.execute("UPDATE threads SET updated_at=? WHERE id=?",
                          (updated, tid))
        store.con.commit()
    store.close()


def _op(db, op, **params):
    res = handle_request({"id": 1, "op": op, "params": {"db": str(db), **params}})
    assert res.get("error") is None, res
    return res["result"]


def _cli(*argv):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["VOYAGER_NO_SYNC"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-m", "voyager.cli", *argv],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


@pytest.fixture
def two(tmp_path):
    """Two active threads in REPO: A is newer, B is older."""
    path = tmp_path / "two.db"
    _seed(path, [("a", "Work A", REPO, 2000.0), ("b", "Work B", REPO, 1000.0)])
    return path


@pytest.fixture
def one(tmp_path):
    path = tmp_path / "one.db"
    _seed(path, [("a", "Work A", REPO, 2000.0)])
    return path


# --- the shared resolver ----------------------------------------------------

def test_one_thread_is_not_reported_as_ambiguous(one):
    store = Store(one)
    try:
        r = resolve_work_thread(store, repo=REPO)
    finally:
        store.close()
    assert r["status"] == "success"
    assert r["thread"]["title"] == "Work A"
    assert len(r["candidates"]) == 1
    assert r["explicit"] is False


def test_two_threads_are_ambiguous_and_no_candidate_is_chosen(two):
    store = Store(two)
    try:
        r = resolve_work_thread(store, repo=REPO)
    finally:
        store.close()
    assert r["status"] == "ambiguous"
    assert [c["title"] for c in r["candidates"]] == ["Work A", "Work B"]
    # `thread` is offered as a default, so it is the newest -- but the status
    # says nothing chose it.
    assert r["thread"]["title"] == "Work A"


def test_candidates_order_is_stable_for_equal_timestamps(tmp_path):
    """SQLite's GROUP BY order is not a contract; (updated_at, id) is."""
    path = tmp_path / "tie.db"
    _seed(path, [("a", "A", REPO, 1000.0), ("b", "B", REPO, 1000.0),
                 ("c", "C", REPO, 1000.0)])
    orders = []
    for _ in range(3):
        store = Store(path)
        try:
            orders.append([c["id"] for c in resolve_work_thread(store, repo=REPO)["candidates"]])
        finally:
            store.close()
    assert orders[0] == orders[1] == orders[2], orders
    assert orders[0] == sorted(orders[0], reverse=True), orders[0]


def test_archived_threads_are_not_candidates(tmp_path):
    path = tmp_path / "arch.db"
    _seed(path, [("a", "Work A", REPO, 2000.0), ("b", "Work B", REPO, 1000.0)])
    store = Store(path)
    try:
        older = [t for t in store.thread_list("active") if t["title"] == "Work B"][0]
        store.thread_set_status(older["id"], "archived")
        r = resolve_work_thread(store, repo=REPO)
    finally:
        store.close()
    assert r["status"] == "success"
    assert r["thread"]["title"] == "Work A"


def test_another_repos_threads_are_not_candidates(tmp_path):
    path = tmp_path / "cross.db"
    _seed(path, [("a", "Work A", REPO, 1000.0), ("z", "Elsewhere", OTHER, 9000.0)])
    store = Store(path)
    try:
        r = resolve_work_thread(store, repo=REPO)
    finally:
        store.close()
    # The much newer thread in another repo must not even appear.
    assert r["status"] == "success"
    assert r["thread"]["title"] == "Work A"


def test_candidates_are_truncated_with_a_flag(tmp_path):
    path = tmp_path / "many.db"
    _seed(path, [("{0:02d}".format(i), "W{0}".format(i), REPO, float(i))
                 for i in range(MAX_THREAD_CANDIDATES + 3)])
    store = Store(path)
    try:
        r = resolve_work_thread(store, repo=REPO)
    finally:
        store.close()
    assert r["status"] == "ambiguous"
    assert len(r["candidates"]) == MAX_THREAD_CANDIDATES
    assert r["candidates_truncated"] is True


# --- explicit thread_id -----------------------------------------------------

def test_an_explicit_thread_is_honoured_even_when_it_is_not_the_newest(two):
    store = Store(two)
    try:
        older = resolve_work_thread(store, repo=REPO)["candidates"][1]["id"]
        r = resolve_work_thread(store, repo=REPO, thread_id=older)
    finally:
        store.close()
    assert r["status"] == "success"
    assert r["thread"]["id"] == older
    assert r["explicit"] is True
    assert r["not_found"] is False
    assert r["out_of_scope"] is False


def test_a_thread_id_that_does_not_exist_is_reported(two):
    store = Store(two)
    try:
        r = resolve_work_thread(store, repo=REPO, thread_id="thr_nope")
    finally:
        store.close()
    assert r["status"] == "no_thread"
    assert r["thread"] is None
    assert r["not_found"] is True


def test_a_thread_from_another_repo_is_honoured_but_flagged(tmp_path):
    """The caller named it, so it is used -- and the mismatch is reported."""
    path = tmp_path / "oos.db"
    _seed(path, [("a", "Work A", REPO, 1000.0), ("z", "Elsewhere", OTHER, 9000.0)])
    store = Store(path)
    try:
        elsewhere = resolve_work_thread(store, repo=OTHER)["thread"]["id"]
        r = resolve_work_thread(store, repo=REPO, thread_id=elsewhere)
    finally:
        store.close()
    assert r["thread"]["id"] == elsewhere
    assert r["out_of_scope"] is True


# --- the bridge op ----------------------------------------------------------

def test_current_work_stays_quiet_with_one_thread(one):
    out = _op(one, "current_work", repo=REPO)
    assert out["has_thread"] is True
    assert "thread_ambiguous" not in out
    assert "thread_candidates" not in out


def test_current_work_reports_ambiguity_with_two(two):
    out = _op(two, "current_work", repo=REPO)
    assert out["thread_ambiguous"] is True
    assert [c["title"] for c in out["thread_candidates"]] == ["Work A", "Work B"]
    assert out["thread"]["title"] == "Work A"
    # ...and it must say that the one it returned is only a default.
    assert "default" in out["thread_default_reason"]
    assert "not a choice" in out["thread_default_reason"]
    # each candidate carries enough to pick from
    for c in out["thread_candidates"]:
        assert set(c) >= {"id", "title", "updated_at", "goal", "repo_root"}


def test_current_work_reports_no_thread_without_one(tmp_path):
    path = tmp_path / "empty.db"
    _seed(path, [])
    out = _op(path, "current_work", repo=REPO)
    assert out["has_thread"] is False
    assert "thread_ambiguous" not in out


def test_current_work_honours_an_explicit_thread(two):
    older = _op(two, "current_work", repo=REPO)["thread_candidates"][1]["id"]
    out = _op(two, "current_work", repo=REPO, thread_id=older)
    assert out["thread"]["id"] == older
    assert out["thread_requested"] == older
    # an explicit choice is not an ambiguity
    assert "thread_ambiguous" not in out


def test_current_work_does_not_fall_back_from_a_bad_thread_id(two):
    """Naming a thread that is not there is not a cue to use a different one."""
    out = _op(two, "current_work", repo=REPO, thread_id="thr_nope")
    assert out["thread_not_found"] is True
    assert out["has_thread"] is False
    assert "thread" not in out
    assert out["scope"] == []


def test_current_work_flags_a_thread_from_another_repo(tmp_path):
    path = tmp_path / "oos2.db"
    _seed(path, [("a", "Work A", REPO, 1000.0), ("z", "Elsewhere", OTHER, 9000.0)])
    elsewhere = _op(path, "current_work", repo=OTHER)["thread"]["id"]
    out = _op(path, "current_work", repo=REPO, thread_id=elsewhere)
    assert out["thread_out_of_scope"] is True
    assert out["thread"]["id"] == elsewhere


# --- continue_context -------------------------------------------------------

def test_continue_context_reports_ambiguity(two):
    out = _op(two, "continue_context", repo=REPO)
    assert "bundle" in out
    assert out["thread_ambiguous"] is True
    assert len(out["thread_candidates"]) == 2
    assert "default" in out["thread_default_reason"]


def test_continue_context_with_a_bad_thread_is_an_error(two):
    """The bridge convention for an op-level failure is `result.error`, and it
    must name the thread rather than quietly continuing from another one."""
    res = handle_request({"id": 1, "op": "continue_context",
                          "params": {"db": str(two), "thread_id": "thr_nope"}})
    assert res.get("result", {}).get("error"), res
    assert "no such WorkThread" in res["result"]["error"]
    assert res["result"]["thread_not_found"] is True


def test_continue_context_by_session_ref_is_unaffected(two):
    out = _op(two, "continue_context", session_refs=["codex:a"])
    assert "thread_ambiguous" not in out


# --- the same semantics behind discover_continuity ---------------------------

def test_discover_continuity_agrees_with_the_resolver(two):
    from voyager.auto import discover_continuity

    store = Store(two)
    try:
        disc = discover_continuity(store, repo=REPO)
        res = resolve_work_thread(store, repo=REPO)
    finally:
        store.close()
    assert disc["status"] == "ambiguous" == res["status"]
    assert ([c["id"] for c in disc["candidate_threads"]]
            == [c["id"] for c in res["candidates"]])
    assert disc["recommended_action"] == "resolve-ambiguity"
    assert disc["continuity_available"] is False


# --- the CLI ----------------------------------------------------------------

def test_cli_continue_refuses_to_guess(two):
    r = _cli("continue", "--db", str(two), "--repo", REPO)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "refusing to guess" in r.stderr
    # every candidate is named, and so is the way out
    for title in ("Work A", "Work B"):
        assert title in r.stderr
    assert "--thread" in r.stderr
    assert "not a choice you made" in r.stderr


def test_cli_continue_with_an_explicit_thread_still_works(two):
    store = Store(two)
    try:
        older = [t for t in store.thread_list("active")
                 if t["title"] == "Work B"][0]["id"]
    finally:
        store.close()
    r = _cli("continue", "--db", str(two), "--repo", REPO, "--thread", older)
    assert r.returncode == 0, r.stdout + r.stderr
    assert older in r.stdout
    # the ambiguity refusal must not fire when a thread was named
    assert "refusing to guess" not in r.stderr


def test_cli_switch_refuses_to_guess(two):
    """A handoff writes a package and takes a lease: the wrong thread matters."""
    r = _cli("switch", "codex", "--db", str(two), "--repo", REPO)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "refusing to guess" in r.stderr


def test_cli_continue_with_one_thread_is_unchanged(one):
    r = _cli("continue", "--db", str(one), "--repo", REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "active thread: " in r.stdout


def test_cli_status_says_ambiguous_not_no_thread(two):
    """`continuity_available` is False for both, so NO_THREAD used to be
    printed for a repo that simply has two threads — which reads as "your
    threads are gone" when the truth is "you have a choice to make"."""
    r = _cli("status", "--db", str(two), "--repo", REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "AMBIGUOUS" in r.stdout
    assert "NO_THREAD" not in r.stdout
    for title in ("Work A", "Work B"):
        assert title in r.stdout

    j = _cli("status", "--db", str(two), "--repo", REPO, "--json")
    assert json.loads(j.stdout)["status"] == "AMBIGUOUS"


# --- the MCP surface --------------------------------------------------------

def test_mcp_current_reports_ambiguity_not_absence(two, monkeypatch):
    from voyager import mcp_server

    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    monkeypatch.setattr(mcp_server, "Store", lambda *a, **k: Store(two))
    out = mcp_server.voyager_current(cwd=REPO)
    payload = json.loads(out)
    assert payload["status"] == "ambiguous"
    assert len(payload["candidate_threads"]) == 2


def test_mcp_context_reports_ambiguity_not_absence(two, monkeypatch):
    from voyager import mcp_server

    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    monkeypatch.setattr(mcp_server, "Store", lambda *a, **k: Store(two))
    out = mcp_server.voyager_context(cwd=REPO)
    # "no active WorkThread" would be false: there are two.
    assert "no active WorkThread" not in out
    assert "2 active WorkThreads" in out
    assert "Work A" in out and "Work B" in out


# --- the MCP handoff surface ------------------------------------------------
# `voyager_switch` takes a lease and records a pending attach, so it must not
# pick a thread on the caller's behalf.  It used to answer "no active
# WorkThread — nothing to switch" for a repo that has two, because
# `continuity_available` is False for both states -- and it had no way to name
# one, so the ambiguity was a dead end rather than a choice.

def _aim_default_store(monkeypatch, db, tmp_path):
    """voyager_switch opens the default index; point it at the fixture.

    The bundle directory is redirected too: a handoff writes a bundle, and a test
    must never drop one into the real ~/.voyager/bundles.
    """
    import voyager.store as store_mod

    monkeypatch.setattr(store_mod, "default_db_path", lambda: db)
    monkeypatch.setenv("VOYAGER_BUNDLES_DIR", str(tmp_path / "bundles"))


def test_mcp_switch_names_candidates_instead_of_denying_them(two, monkeypatch, tmp_path):
    from voyager import mcp_server

    _aim_default_store(monkeypatch, two, tmp_path)
    r = mcp_server.voyager_switch(target="codex", cwd=REPO)

    assert "nothing to switch" not in r, (
        "the repo has two threads; saying there is nothing is false: %r" % r
    )
    assert "pass thread_id" in r
    assert r.count("thr_") == 2, "both candidates must be named: %r" % r
    assert "Work A" in r and "Work B" in r


def test_mcp_switch_with_explicit_thread_proceeds(two, monkeypatch, tmp_path):
    from voyager import mcp_server

    _aim_default_store(monkeypatch, two, tmp_path)
    store = Store(two)
    try:
        tid = store.con.execute(
            "select id from threads order by updated_at desc limit 1").fetchone()[0]
    finally:
        store.close()

    r = mcp_server.voyager_switch(target="claude", cwd=REPO, thread_id=tid)
    assert "pass thread_id" not in r, "an explicit choice must not be asked again"
    assert "no such WorkThread" not in r


def test_mcp_switch_unknown_thread_is_named(two, monkeypatch, tmp_path):
    from voyager import mcp_server

    _aim_default_store(monkeypatch, two, tmp_path)
    r = mcp_server.voyager_switch(target="claude", cwd=REPO, thread_id="thr_nope")
    assert "no such WorkThread: thr_nope" in r


def test_mcp_switch_single_thread_still_works(one, monkeypatch, tmp_path):
    """The guard must not fire when there is exactly one obvious thread."""
    from voyager import mcp_server

    _aim_default_store(monkeypatch, one, tmp_path)
    r = mcp_server.voyager_switch(target="codex", cwd=REPO)
    assert "pass thread_id" not in r
    assert "nothing to switch" not in r



def _seed_loose(db_path: Path, sessions) -> None:
    """Sessions with no WorkThread -- the state P18's repo fallback covers."""
    store = Store(db_path)
    src = db_path.parent / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    for suffix, title, repo, updated in sessions:
        sid = "codex:{0}".format(suffix)
        store.replace_session(
            new_session(id=sid, provider="codex", native_session_id=suffix,
                        title=title, started_at=updated - 100, updated_at=updated,
                        cwd=repo, repo_root=repo, message_count=2, can_resume=True),
            [new_event(sid=sid, ts=updated - 100, seq=0, kind="user",
                       content="work on " + title),
             new_event(sid=sid, ts=updated - 50, seq=1, kind="assistant",
                       content=title + ": still open")],
            "codex", src)
    store.close()

# --- voyager_continue: the same selection, on the MCP surface ---------------
# It used to expose no `thread_id`, so a caller that was told to "pick one" had
# no way to -- and with no WorkThread at all it answered "nothing to continue"
# while `voyager continue` compiled a bundle from the very same index.

def test_mcp_continue_names_candidates_and_how_to_pick_one(two, monkeypatch, tmp_path):
    from voyager import mcp_server

    _aim_default_store(monkeypatch, two, tmp_path)
    r = mcp_server.voyager_continue(cwd=REPO)
    assert "nothing was chosen" in r
    assert "thread_id" in r, "naming the parameter is what makes it choosable"
    store = Store(two)
    try:
        ids = [row["id"] for row in
               store.con.execute("select id from threads").fetchall()]
    finally:
        store.close()
    assert all(tid in r for tid in ids), "every candidate must be named"


def test_mcp_continue_with_thread_id_uses_only_that_thread(two, monkeypatch, tmp_path):
    from voyager import mcp_server

    _aim_default_store(monkeypatch, two, tmp_path)
    store = Store(two)
    try:
        tid = store.con.execute(
            "select id from threads order by updated_at desc limit 1").fetchone()[0]
    finally:
        store.close()

    r = mcp_server.voyager_continue(cwd=REPO, thread_id=tid)
    assert "nothing was chosen" not in r
    assert "pass thread_id" not in r
    # One thread's members only: the bundle exists and is about that work.
    assert "# Continuation Bundle" in r or "# Context" in r


def test_mcp_continue_rejects_an_unknown_thread_by_name(two, monkeypatch, tmp_path):
    from voyager import mcp_server

    _aim_default_store(monkeypatch, two, tmp_path)
    r = mcp_server.voyager_continue(cwd=REPO, thread_id="thr_nope")
    assert r == "no such WorkThread: thr_nope"
    # Not a silent fall back to the most recent one.
    assert "Continuation Bundle" not in r


def test_mcp_continue_falls_back_to_repo_scope_like_the_cli(
        tmp_path, monkeypatch):
    """With no WorkThread anywhere, MCP and the CLI must not disagree.

    `voyager continue` compiled from the newest sessions; this path answered
    "nothing to continue" on the same index.  It now shares that selection and
    says in the text that recency is a default.
    """
    from voyager import mcp_server

    db = tmp_path / "loose.db"
    _seed_loose(db, [("l1", "Loose one", REPO, 1000.0),
                     ("l2", "Loose two", REPO, 2000.0)])
    _aim_default_store(monkeypatch, db, tmp_path)

    r = mcp_server.voyager_continue(cwd=REPO)
    assert "nothing to continue" not in r, (
        "the CLI produces a bundle from this index; MCP must not report a vacuum"
    )
    assert "selection:" in r
    assert "recency is a default" in r


def test_mcp_context_thread_id_is_accepted_too(tmp_path, monkeypatch):
    from voyager import mcp_server

    db = tmp_path / "loose.db"
    _seed(db, [("l1", "Loose one", REPO, 1000.0)])
    _aim_default_store(monkeypatch, db, tmp_path)
    store = Store(db)
    try:
        tid = store.thread_create(repo_root=REPO, title="Loose one")
        store.thread_attach(tid, "codex:l1")
    finally:
        store.close()

    r = mcp_server.voyager_context(cwd=REPO, thread_id=tid)
    assert "# Continuation Bundle" in r or "# Context" in r
    r2 = mcp_server.voyager_context(cwd=REPO, thread_id="thr_nope")
    assert r2 == "no such WorkThread: thr_nope"
