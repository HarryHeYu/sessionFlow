"""WorkThread briefs: several agents' work as one continuous task.

The claim being tested is that a brief is a *derivation* over the canonical
thread, not a concatenation of transcripts.  So: the authoritative fields come
from the thread itself and never from the latest assistant message; the work is
grouped per agent; the result is deterministic; and nothing is invented -- an
open item exists only because a checkpoint recorded one.
"""

from __future__ import annotations

import json
import time

import pytest

from voyager import thread_brief as tb
from voyager.checkpoint import checkpoint_create, init_checkpoint_schema
from voyager.model import new_event, new_session
from voyager.store import Store


@pytest.fixture
def world(tmp_path):
    """One thread with two agents, plus a third that never joined it."""
    path = tmp_path / "index.db"
    store = Store(path)
    tid = store.thread_create(repo_root="E:/repo", title="Design then build",
                              goal="ship the thing")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")

    def seed(sid, provider, native, texts, started):
        sess = new_session(id=sid, provider=provider, native_session_id=native,
                           title=sid, started_at=started, updated_at=started + 5,
                           repo_root="E:/repo", cwd="E:/repo")
        events = []
        for i, (kind, text) in enumerate(texts, start=1):
            events.append(new_event(sid=sid, seq=i, kind=kind,
                                    ts=started + i, content=text))
        store.replace_session(sess, events, provider, src)
        store.thread_attach(tid, sid)

    # two human turns so the first agent is WEAK rather than UNKNOWN
    seed("claude:design", "claude", "design",
         [("user", "design the continuity layer"),
          ("assistant", "here is the design"),
          ("user", "adjust it"),
          ("assistant", "adjusted")], time.time() - 500)
    seed("codex:build", "codex", "build",
         [("user", "implement it"), ("assistant", "implemented")],
         time.time() - 100)
    yield store, tid
    store.close()


# --- activity ---------------------------------------------------------------

def test_activity_groups_by_agent(world):
    store, tid = world
    data = tb.activity(store, tid)
    assert data["thread_id"] == tid
    assert data["providers"] == ["claude", "codex"]
    assert data["member_count"] == 2
    for c in data["contributions"]:
        assert c["events"] > 0
        assert c["band"] in ("STRONG", "WEAK", "UNKNOWN", "BOOTSTRAP_ONLY")
        assert c["native_session_id"]


def test_activity_is_newest_first(world):
    store, tid = world
    rows = tb.activity(store, tid)["contributions"]
    assert rows[0]["provider"] == "codex", "the most recent agent leads"
    assert [r["last_ts"] for r in rows] == sorted(
        [r["last_ts"] for r in rows], reverse=True)


def test_activity_on_an_unknown_thread_is_an_error_not_a_crash(world):
    store, _ = world
    data = tb.activity(store, "thr_nope")
    assert "error" in data
    assert data["error"].startswith("no such WorkThread")


# --- summarize --------------------------------------------------------------

def test_the_authoritative_fields_come_from_the_thread_not_a_message(world):
    """A brief must never take its goal from the latest thing an agent said."""
    store, tid = world
    b = tb.summarize(store, tid)
    assert b.title == "Design then build"
    assert b.goal == "ship the thing"
    assert b.repo_root == "E:/repo"
    assert b.status == "active"


def test_the_brief_is_grouped_per_agent(world):
    store, tid = world
    b = tb.summarize(store, tid)
    assert {c.provider for c in b.contributions} == {"claude", "codex"}
    assert {e["provider"] for e in b.recent} == {"claude", "codex"}


def test_recent_turns_are_oldest_first(world):
    store, tid = world
    b = tb.summarize(store, tid)
    ts = [e["ts"] for e in b.recent]
    assert ts == sorted(ts), "a reader follows the thread forwards"


def test_the_brief_is_deterministic(world):
    store, tid = world
    a = json.dumps(tb.summarize(store, tid).to_dict(), sort_keys=True, default=str)
    b = json.dumps(tb.summarize(store, tid).to_dict(), sort_keys=True, default=str)
    assert a == b


def test_retrieval_pointers_name_real_sessions(world):
    store, tid = world
    b = tb.summarize(store, tid)
    sids = {c.sid for c in b.contributions}
    for ptr in b.open_items["retrieval"]:
        assert ptr["sid"] in sids
        assert ptr["how"].startswith("voyager show ")


def test_nothing_is_invented_without_a_checkpoint(world):
    store, tid = world
    b = tb.summarize(store, tid)
    assert b.checkpoint is None
    assert b.open_items["blockers"] == []
    assert b.open_items["pending_decisions"] == []
    assert b.open_items["next_actions"] == []


def test_a_checkpoint_supplies_the_open_items(world):
    store, tid = world
    init_checkpoint_schema(store)
    checkpoint_create(
        store, thread_id=tid, goal="ship the thing", phase="implementation",
        milestones=[{"title": "design agreed", "status": "completed"},
                    {"title": "build it", "status": "in_progress"}],
        blockers=[{"description": "waiting on review", "severity": "high"}],
        decisions=[{"topic": "branching", "decision": "merge or rebase",
                    "rationale": "one line of history", "made_by": "user"}],
        next_actions=["write the docs"],
        changed_files=["voyager/thread_brief.py"],
        head_commit="deadbeef")
    b = tb.summarize(store, tid)
    assert b.checkpoint["phase"] == "implementation"
    assert b.checkpoint["milestones"] == ["design agreed"], "only completed ones"
    assert b.open_items["blockers"] == ["waiting on review"]
    assert b.open_items["pending_decisions"] == ["merge or rebase"]
    assert b.open_items["next_actions"] == ["write the docs"]
    assert b.checkpoint["changed_files"] == ["voyager/thread_brief.py"]


def test_json_is_serialisable_and_ascii_safe(world):
    store, tid = world
    payload = json.dumps(tb.summarize(store, tid).to_dict(), ensure_ascii=False,
                         default=str)
    json.loads(payload)
    tb.render(tb.summarize(store, tid)).encode("ascii")


def test_an_empty_thread_is_reported_not_crashed(tmp_path):
    store = Store(tmp_path / "e.db")
    try:
        tid = store.thread_create(repo_root="E:/empty", title="empty", goal="g")
        b = tb.summarize(store, tid)
        assert b.contributions == []
        assert "no agents" in tb.render(b) or "(none attached)" in tb.render(b)
    finally:
        store.close()


def test_an_unknown_thread_summarizes_to_an_error(tmp_path):
    store = Store(tmp_path / "e2.db")
    try:
        b = tb.summarize(store, "thr_missing")
        assert "error" in b.open_items
    finally:
        store.close()


# --- the brief must not be an N+1 query loop -------------------------------

def test_activity_uses_a_constant_number_of_queries(tmp_path):
    """Counting queries rather than milliseconds: a timing assertion would be
    flaky, but the shape of the query pattern is not.

    The per-member form cost ~38 ms a query on a 1.6 GB index with a cold page
    cache and made a brief take two seconds; batching is what fixed it, and this
    is what keeps it fixed.  The only per-member query left is `session_band`,
    which needs that session's events to classify it.
    """
    from voyager.model import new_event, new_session
    from voyager.store import Store

    store = Store(tmp_path / "many.db")
    try:
        tid = store.thread_create(repo_root="E:/many", title="many", goal="g")
        src = tmp_path / "s.jsonl"
        src.write_text("{}", encoding="utf-8")
        for i in range(20):
            sid = "codex:m%d" % i
            sess = new_session(id=sid, provider="codex", native_session_id="m%d" % i,
                               title=sid, started_at=1.0 + i, updated_at=2.0 + i,
                               repo_root="E:/many", cwd="E:/many")
            store.replace_session(sess, [
                new_event(sid=sid, seq=1, kind="user", ts=1.0 + i,
                          content="hello %d" % i, origin="human"),
                new_event(sid=sid, seq=2, kind="assistant", ts=1.1 + i,
                          content="reply %d" % i)], "codex", src)
            store.thread_attach(tid, sid)

        calls = []
        real_q = store.q

        def counting(sql, args=()):
            calls.append(sql)
            return real_q(sql, args)

        store.q = counting
        try:
            data = tb.activity(store, tid)
        finally:
            store.q = real_q

        assert len(data["contributions"]) == 20
        # one query per member (session_band) plus a small constant for the set
        # queries -- nowhere near the three-per-member shape this replaced
        assert len(calls) < 20 + 12, len(calls)
        assert len(calls) < 3 * 20, "the N+1 form would be at least 60"
    finally:
        store.close()
