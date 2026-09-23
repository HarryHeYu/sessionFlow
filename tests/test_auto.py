"""Automatic Continuity tests (Phase A/B/C/D/I).

Contracts:
- pending attach auto-resolution: unique match attaches, ambiguity never
  attaches, wrong repo / old sessions never match, TTL marks stale,
  restart recovery works, repeated scans are idempotent
- native SessionStart leaves a resumable pending record: the reported
  "pending_resolve" is backed by a real open row, a later scan resolves it
  into a thread membership, and a pending that names a native session matches
  by identity rather than by the uniqueness heuristic
- discovery: deterministic resolution (explicit thread > repo > none),
  continuity_available=False when nothing persists
- safe auto attach: only when exact repo match + active thread + session
  unattached + no ambiguity; "same repo" alone is not enough
- get_continuation_context: provenance-bound bundle under budget,
  provider files untouched
"""

from __future__ import annotations

import os
import subprocess
import time
from unittest import mock

import pytest

from voyager.auto import (
    PENDING_TTL,
    discover_continuity,
    get_continuation_context,
    resolve_pending_attaches,
)
from voyager.cli import main, run_scan
from voyager.model import new_event, new_session
from voyager.startup import startup_continuity
from voyager.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "auto.db")
    yield s
    s.close()


def _seed_session(store, sid, provider, native, repo="E:/proj/demo",
                  born=None, content="work"):
    src = store.db_path.parent / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    s = new_session(id=sid, provider=provider, native_session_id=native,
                    title=native, started_at=born or 1000.0,
                    updated_at=born or 1000.0, cwd=repo,
                    repo_root=repo, message_count=1)
    store.replace_session(s, [new_event(sid=sid, ts=born or 1000.0, seq=0,
                                        kind="user", content=content)],
                          provider, src)
    return sid


def _mk_thread(store, repo="E:/proj/demo", title="the task", goal=None):
    tid = store.thread_create(repo_root=repo, title=title, goal=goal)
    return tid


def _attach_member(store, tid, provider, native, repo="E:/proj/demo",
                   born=1000.0):
    sid = f"{provider}:{native}"
    _seed_session(store, sid, provider, native, repo=repo, born=born)
    store.thread_attach(tid, sid)
    return sid


# ---------------------------------------------------------------------------
# Phase A — pending auto-resolution
# ---------------------------------------------------------------------------

def test_pending_unique_match_auto_attaches(store, tmp_path):
    tid = _mk_thread(store)
    store.pending_record(tid, "claude", repo_root="E:/proj/demo",
                         cwd="E:/proj/demo", created_at=3000.0,
                         source_provider="codex", goal="demo")
    # the target agent's new session appears in the index, born after launch
    _seed_session(store, "claude:new1", "claude", "new1", born=3001.0)

    stats = resolve_pending_attaches(store, now=4000.0)
    assert len(stats["attached"]) == 1
    assert stats["attached"][0]["thread"] == tid
    # attached + pending resolved
    assert "claude:new1" in store.thread_member_ids(tid)
    row = store.q("SELECT status, resolved_sid FROM thread_pending "
                  "WHERE thread_id=?", (tid,))[0]
    assert row["status"] == "resolved"
    assert row["resolved_sid"] == "claude:new1"
    store._continuity_log  # audit method exists


def test_pending_two_candidates_stay_open(store):
    tid = _mk_thread(store)
    store.pending_record(tid, "claude", repo_root="E:/proj/demo",
                         created_at=3000.0)
    _seed_session(store, "claude:new1", "claude", "new1", born=3001.0)
    _seed_session(store, "claude:new2", "claude", "new2", born=3002.0)

    stats = resolve_pending_attaches(store, now=4000.0)
    assert stats["attached"] == []
    # ambiguous: stays open (not guessed), sessions not attached
    assert store.thread_member_ids(tid) == []


def test_pending_different_repo_not_attached(store):
    tid = _mk_thread(store, repo="E:/proj/demo")
    store.pending_record(tid, "claude", repo_root="E:/proj/demo",
                         created_at=3000.0)
    _seed_session(store, "claude:elsewhere", "claude", "e1",
                  repo="E:/other/repo", born=3001.0)
    resolve_pending_attaches(store, now=4000.0)
    assert "claude:elsewhere" not in store.thread_member_ids(tid)
    assert "claude:elsewhere" not in [r["session_id"] for r in
                                      store.q("SELECT session_id FROM thread_sessions")]


def test_pending_old_session_not_attached(store):
    tid = _mk_thread(store)
    store.pending_record(tid, "claude", repo_root="E:/proj/demo",
                         created_at=3000.0)
    # born BEFORE the switch launched → pre-existing session, not the target
    _seed_session(store, "claude:old1", "claude", "old1", born=1000.0)
    resolve_pending_attaches(store, now=4000.0)
    assert "claude:old1" not in store.thread_member_ids(tid)


def test_pending_already_attached_not_duplicated(store):
    tid = _mk_thread(store)
    store.pending_record(tid, "claude", repo_root="E:/proj/demo",
                         created_at=3000.0)
    _seed_session(store, "claude:new1", "claude", "new1", born=3001.0)
    store.thread_attach(tid, "claude:new1")
    resolve_pending_attaches(store, now=4000.0)
    assert store.thread_member_ids(tid).count("claude:new1") == 1


def test_pending_scan_idempotent(store):
    tid = _mk_thread(store)
    store.pending_record(tid, "claude", repo_root="E:/proj/demo",
                         created_at=3000.0)
    _seed_session(store, "claude:new1", "claude", "new1", born=3001.0)

    for _ in range(3):
        resolve_pending_attaches(store, now=4000.0)
    assert store.thread_member_ids(tid).count("claude:new1") == 1
    assert store.q("SELECT COUNT(*) n FROM thread_sessions")[0]["n"] == 1


def test_pending_ttl_marks_stale(store, monkeypatch):
    tid = _mk_thread(store)
    store.pending_record(tid, "claude", repo_root="E:/proj/demo",
                         created_at=3000.0)
    monkeypatch.setattr("voyager.auto.PENDING_TTL", 500.0)
    stats = resolve_pending_attaches(store, now=3000.0 + 501.0)
    assert len(stats["stale"]) == 1
    rows = store.q("SELECT status FROM thread_pending WHERE thread_id=?",
                   (tid,))
    assert rows[0]["status"] == "stale"


def test_pending_survives_restart(store, tmp_path):
    """Crash/restart recovery: the pending record lives in the index, so a
    fresh Store instance still resolves it."""
    tid = _mk_thread(store)
    store.pending_record(tid, "claude", repo_root="E:/proj/demo",
                         created_at=3000.0)
    store.close()

    # "restart": new Store instance, new session appears, resolve
    store2 = Store(tmp_path / "auto.db")
    _seed_session(store2, "claude:new1", "claude", "new1", born=3001.0)
    stats = resolve_pending_attaches(store2, now=4000.0)
    assert len(stats["attached"]) == 1
    assert "claude:new1" in store2.thread_member_ids(tid)
    store2.close()


# ---------------------------------------------------------------------------
# Phase B — discovery
# ---------------------------------------------------------------------------

def test_discover_finds_active_thread(store):
    tid = _mk_thread(store, title="the task")
    _seed_session(store, "codex:m0", "codex", "m0")
    store.thread_attach(tid, "codex:m0")
    disc = discover_continuity(store, cwd="E:/proj/demo", provider="codex")
    assert disc["continuity_available"] is True
    assert disc["active_thread"]["id"] == tid
    assert disc["latest_holder"] == "codex"
    assert disc["recommended_action"] == "native-resume"
    assert disc["lease_state"]["held"] is False


def test_discover_no_thread_means_unavailable(store):
    disc = discover_continuity(store, cwd="E:/nowhere")
    assert disc["continuity_available"] is False
    assert disc["recommended_action"] == "none"


def test_discover_explicit_thread_wins(store):
    _mk_thread(store, repo="E:/other")
    tid = _mk_thread(store, repo="E:/proj/demo")
    store.thread_attach(tid, "codex:m0")
    disc = discover_continuity(store, cwd="E:/somewhere/else",
                               thread_id=tid)
    assert disc["continuity_available"] is True
    assert disc["active_thread"]["id"] == tid


# ---------------------------------------------------------------------------
# safe auto attach (part C / part 9)
# ---------------------------------------------------------------------------

def test_safe_attach_needs_exact_repo_and_active_thread(store):
    tid = _mk_thread(store, repo="E:/proj/demo")
    store.thread_attach(tid, "codex:m0")
    # session in the index, same provider, but a DIFFERENT repo
    _seed_session(store, "claude:other-repo", "claude", "or1",
                  repo="E:/other/repo", born=3001.0)
    disc = discover_continuity(store, cwd="E:/other/repo", provider="claude",
                               native_session_id="or1")
    # a thread exists for E:/other? no → continuity unavailable, no attach
    assert disc["continuity_available"] is False
    assert "claude:other-repo" not in store.thread_member_ids(tid)
    store.close()


def test_no_silent_clustering_same_repo_two_threads(store):
    """Two active threads in the same repo: a session must NOT be silently
    attached to either — discovery reports AMBIGUOUS, attach stays explicit."""
    tid_a = _mk_thread(store, repo="E:/shared", title="A")
    store.thread_attach(tid_a, "codex:a1")
    tid_b = _mk_thread(store, repo="E:/shared", title="B")
    _seed_session(store, "claude:loose", "claude", "loose",
                  repo="E:/shared", born=3001.0)

    disc = discover_continuity(store, cwd="E:/shared", provider="claude",
                               native_session_id="loose")
    # SECURITY: two active threads → AMBIGUOUS, not continuity available
    assert disc["continuity_available"] is False
    assert disc.get("status") == "ambiguous"
    assert len(disc.get("candidate_threads", [])) == 2
    # Session not attached to either thread (ambiguity prevents auto-attach)
    assert "claude:loose" not in store.thread_member_ids(tid_a)
    assert "claude:loose" not in store.thread_member_ids(tid_b)


# ---------------------------------------------------------------------------
# get_continuation_context (shared core surface)
# ---------------------------------------------------------------------------

def test_continuation_context_compiles_with_provenance(store):
    tid = _mk_thread(store, title="the task", goal="ship the demo")
    _attach_member(store, tid, "codex", "c1", born=1000.0)
    _attach_member(store, tid, "claude", "c2", born=2000.0)

    res = get_continuation_context(store=store, cwd="E:/proj/demo",
                                   provider="claude",
                                   native_session_id="c2",
                                   goal="ship the demo", budget="compact",
                                   sync=False)
    assert res["continuity_available"] is True
    assert res["thread"]["id"] == tid
    ctx = res["context"]
    assert "## Goal" in ctx
    # provenance: member session ids appear in the bundle
    assert "codex:c1" in ctx or "claude:c2" in ctx
    from voyager.budget import estimate_tokens
    assert estimate_tokens(ctx) <= 4000


def test_continuation_context_no_goal_compatible(store):
    tid = _mk_thread(store)
    _attach_member(store, tid, "codex", "c1", born=1000.0)
    res = get_continuation_context(store=store, cwd="E:/proj/demo",
                                   sync=False)
    ctx = res["context"]
    assert "## Goal-ranked evidence" not in ctx   # no goal → no ranked section
    assert "## Current verified state" in ctx      # core sections still there


def test_continuation_context_auto_attaches_own_session(store):
    tid = _mk_thread(store, repo="E:/proj/demo")
    store.thread_attach(tid, "codex:c0")
    _seed_session(store, "claude:new9", "claude", "new9", born=3001.0)

    res = get_continuation_context(store=store, cwd="E:/proj/demo",
                                   provider="claude",
                                   native_session_id="new9", sync=False)
    assert res["auto_attach"] == "claude:new9"
    assert "claude:new9" in store.thread_member_ids(res["thread"]["id"])


# ---------------------------------------------------------------------------
# Phase I — voyager status
# ---------------------------------------------------------------------------

def test_status_command(tmp_path, store, capsys):
    tid = _mk_thread(store, title="the task")
    store.thread_attach(tid, "codex:m0")
    store.close()
    assert main(["status", "--db", str(store.db_path), "--repo",
                 "E:/proj/demo"]) == 0
    out = capsys.readouterr().out
    assert "continuity: READY" in out
    assert "thread :" in out or "thread" in out


# ---------------------------------------------------------------------------
# A native SessionStart must leave a *resumable* pending record
#
# `startup_continuity()` used to report `attach_status="pending_resolve"` and
# write nothing at all. `resolve_pending_attaches()` only walks *open pending
# rows*, and the sole other production writer was the explicit switch flow — so
# the state the primitive reported could never be reached. A natively started
# session was therefore never auto-attached to its WorkThread, which is exactly
# the zero-manual-command path this design exists for.
#
# The tests below pin the whole chain the docstring promises:
#
#   native start → open pending row (carrying native_session_id)
#   → scan indexes the session → resolver attaches it → pending resolved
#
# The identity tests matter because a native start *knows* which session it is
# waiting for, whereas a switch launch does not: the filter must narrow the
# native case without touching the switch case.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def git_repo(tmp_path_factory):
    """A real git repo, so `git_info()` yields the production `repo_root`."""
    repo = tmp_path_factory.mktemp("nativestart") / "workrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)],
                   capture_output=True, timeout=60)
    return repo


def _native_start(store, repo, native, provider="claude", auto_attach=True):
    """Drive the primitive the way a SessionStart hook does.

    The native session has just been born, so it cannot be in the index yet —
    that is what makes the auto-attach safety check fail. The compiler is
    stubbed out (as in `test_context_cache.py`) because context compilation is
    a separate concern and would otherwise dominate the runtime.
    """
    fake = {"continuity_available": True, "context": "BUNDLE"}
    with mock.patch("voyager.auto.get_continuation_context",
                    lambda **_kw: fake):
        return startup_continuity(provider=provider, cwd=str(repo),
                                  native_session_id=native,
                                  auto_attach=auto_attach, store=store)


def test_native_start_records_an_open_pending_attach(store, git_repo):
    """The reported `pending_resolve` must be backed by a real record.

    Before the fix this branch returned the status string and nothing else, so
    `pending_open()` was empty and there was nothing for a later scan to
    consume."""
    tid = _mk_thread(store, repo=str(git_repo), title="native start")

    result = _native_start(store, git_repo, "brand-new-0001")

    assert result.attach_status == "pending_resolve"
    rows = store.pending_open(thread_id=tid)
    assert len(rows) == 1, "pending_resolve must correspond to an open record"
    pend = rows[0]
    assert pend["provider"] == "claude"
    assert pend["native_session_id"] == "brand-new-0001"
    assert pend["status"] == "open"
    assert pend["created_at"] and pend["created_at"] > 0
    assert pend["repo_root"], "resolver matches on repo; it must be recorded"
    assert pend["note"], "the record should say why it is waiting"
    # the session genuinely is not indexed yet — that is *why* it is pending
    assert store.q("SELECT 1 FROM sessions WHERE id=?",
                   ("claude:brand-new-0001",)) == []


def test_scan_indexes_the_session_and_resolves_the_pending(store, tmp_path,
                                                           git_repo):
    """The full chain, driven the way it actually happens.

    The SessionStart hook builds its own short-lived Store and closes it on the
    way out, and the scan that later picks the session up runs in a different
    process — so the pending must survive a store restart, and the resolver must
    be reached through `run_scan()` (what `cmd_scan` runs) rather than called
    directly. This covers the wiring, not just the resolver in isolation."""
    tid = _mk_thread(store, repo=str(git_repo), title="native start")
    _native_start(store, git_repo, "brand-new-0002")
    assert store.thread_member_ids(tid) == []
    store.close()   # the hook's process exits

    # a later process: fresh Store, the session the hook had only just seen is
    # now on disk and gets indexed
    store2 = Store(tmp_path / "auto.db")
    _seed_session(store2, "claude:brand-new-0002", "claude", "brand-new-0002",
                  repo=str(git_repo), born=time.time())

    # only the grok adapter runs: the isolated test env gives it nothing to
    # discover, and it must not prune the claude session seeded above
    run_scan(store2, providers=["grok"], quiet=True)

    assert store2.thread_member_ids(tid) == ["claude:brand-new-0002"]
    row = store2.q("SELECT status, resolved_sid FROM thread_pending "
                   "WHERE thread_id=?", (tid,))[0]
    assert row["status"] == "resolved"
    assert row["resolved_sid"] == "claude:brand-new-0002"
    assert store2.pending_open(thread_id=tid) == []
    # exactly one pending ever existed for this thread/provider
    assert store2.q("SELECT COUNT(*) AS n FROM thread_pending WHERE thread_id=?",
                    (tid,))[0]["n"] == 1


def test_pending_matches_its_native_session_not_just_a_unique_candidate(
        store, git_repo):
    """Identity beats the uniqueness heuristic.

    Two sessions are born after the pending, so the "exactly one candidate"
    rule alone would call this AMBIGUOUS and attach nothing. The pending knows
    which native session it is waiting for, so the attach stays deterministic."""
    tid = _mk_thread(store, repo=str(git_repo), title="native start")
    _native_start(store, git_repo, "wanted-0003")

    born = time.time()
    _seed_session(store, "claude:decoy", "claude", "decoy",
                  repo=str(git_repo), born=born)
    _seed_session(store, "claude:wanted-0003", "claude", "wanted-0003",
                  repo=str(git_repo), born=born)

    stats = resolve_pending_attaches(store, now=time.time() + 1)

    assert [a["session"] for a in stats["attached"]] == ["claude:wanted-0003"]
    assert store.thread_member_ids(tid) == ["claude:wanted-0003"]
    assert store.pending_open(thread_id=tid) == []


def test_pending_waits_for_its_own_native_session(store, git_repo):
    """A single plausible candidate is NOT enough when the pending names a
    different session: the resolver must not guess."""
    tid = _mk_thread(store, repo=str(git_repo), title="native start")
    _native_start(store, git_repo, "wanted-0004")

    _seed_session(store, "claude:someone-else", "claude", "someone-else",
                  repo=str(git_repo), born=time.time())

    stats = resolve_pending_attaches(store, now=time.time() + 1)

    assert stats["attached"] == []
    assert store.thread_member_ids(tid) == []
    assert len(store.pending_open(thread_id=tid)) == 1, "still waiting"


def test_native_start_attaches_directly_when_the_session_is_indexed(
        store, git_repo):
    """Guard: the new pending write must not shadow the normal attach path.

    When the session *is* already indexed the safety check passes, the attach
    happens inline, and no pending is left behind to be resolved later."""
    tid = _mk_thread(store, repo=str(git_repo), title="native start")
    _seed_session(store, "claude:known-0005", "claude", "known-0005",
                  repo=str(git_repo), born=time.time())

    result = _native_start(store, git_repo, "known-0005")

    assert result.attach_status == "auto_attached"
    assert store.thread_member_ids(tid) == ["claude:known-0005"]
    assert store.pending_open(thread_id=tid) == []


def test_switch_pending_without_a_native_id_still_resolves(store):
    """Guard: the switch flow does not know the target's native session id.

    Its pending carries no identity, so it must keep resolving by the
    uniqueness rule. A filter that demanded a native id would silently break
    every explicit handoff."""
    tid = _mk_thread(store)
    store.pending_record(tid, "claude", repo_root="E:/proj/demo",
                         created_at=3000.0, source_provider="codex")
    assert store.pending_open(thread_id=tid)[0]["native_session_id"] is None

    _seed_session(store, "claude:new1", "claude", "new1", born=3001.0)

    stats = resolve_pending_attaches(store, now=4000.0)

    assert [a["session"] for a in stats["attached"]] == ["claude:new1"]
    assert store.thread_member_ids(tid) == ["claude:new1"]
