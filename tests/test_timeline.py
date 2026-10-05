"""O3 — the canonical WorkThread timeline.

The timeline is the one place that answers "what happened, in what order,
across which agents".  Two things are pinned here:

* **evidence only** — every event must come from a canonical column that
  carries a time, or from the append-only `thread_events` log.  An assistant
  message that *sounds* like a milestone must produce nothing;
* **one model** — the CLI, the dashboard and the webview all consume
  `build_thread_timeline`; a second aggregation would drift invisibly.

`story_thread` is the fixture the spec asks for: Claude → checkpoint → handoff
to Codex → Codex work → source rotates → retained → source returns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voyager import timeline
from voyager.checkpoint import checkpoint_create, init_checkpoint_schema
from voyager.store import Store


def _session(sid: str, provider: str, needle: str, *, can_resume=False,
             ts: float = 1000.0) -> dict:
    return {
        "id": sid, "provider": provider, "native_session_id": sid.split(":")[-1],
        "title": needle, "started_at": ts, "updated_at": ts + 10,
        "cwd": "E:/proj/demo", "repo_root": "E:/proj/demo",
        "message_count": 1, "tool_count": 0,
        "can_resume": can_resume, "can_fork": False,
        "resume_cmd": f"{provider} resume {sid}" if can_resume else None,
        "metadata": {}, "raw_metadata": {},
    }


def _events(sid: str, needle: str, kind: str = "user") -> list:
    return [{"sid": sid, "ts": 1000.0, "seq": 0, "kind": kind,
             "content": needle}]


def _src(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("{}", encoding="utf-8")
    return p


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "tl.db")
    # `checkpoints` is created by checkpoint.init_checkpoint_schema, not by the
    # core SCHEMA (voyager/cli.py does this in cmd_checkpoint).  The timeline
    # itself tolerates the table being absent; this fixture wants it present.
    init_checkpoint_schema(s)
    yield s
    s.close()


@pytest.fixture
def story_thread(store, tmp_path):
    """Claude → checkpoint → handoff to Codex → work → source rotates →
    retained → source returns.  Returns (thread_id, codex_src)."""
    claude_src = _src(tmp_path, "claude.jsonl")
    codex_src = _src(tmp_path, "codex.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="story")

    store.replace_session(_session("claude:c1", "claude", "claude work"),
                          _events("claude:c1", "claude work"), "claude", claude_src)
    store.thread_attach(tid, "claude:c1")

    checkpoint_create(store, tid, goal="ship O3", phase="implementation",
                      milestones=[{"title": "timeline model drafted",
                                   "status": "completed"}],
                      blockers=[{"description": "needs an index",
                                 "severity": "medium"}],
                      tests=["pytest -q"], head_commit="abc1234def")

    # the handoff: exactly what handoff_thread records
    store.pending_record(tid, "codex",
                         note="handoff continuation: bundle.md",
                         source_provider="claude", source_session="claude:c1",
                         goal="ship O3", repo_root="E:/proj/demo")

    store.replace_session(_session("codex:x1", "codex", "codex work"),
                          _events("codex:x1", "codex work"), "codex", codex_src)
    store.thread_attach(tid, "codex:x1")

    # the provider rotates its storage
    codex_src.unlink()
    store.prune_missing_sessions("codex", set())

    # ... and later it comes back
    codex_src.write_text("{}", encoding="utf-8")
    store.replace_session(_session("codex:x1", "codex", "codex work"),
                          _events("codex:x1", "codex work"), "codex", codex_src)
    store.prune_missing_sessions("codex", {str(codex_src)})
    return tid, codex_src


# --- the story -------------------------------------------------------------

def test_story_timeline_is_complete_and_ordered(store, story_thread):
    tid, _ = story_thread
    tl = timeline.build_thread_timeline(store, tid)

    kinds = [e["event_type"] for e in tl["events"]]
    for expected in (timeline.THREAD_CREATED, timeline.SESSION_ATTACHED,
                     timeline.CHECKPOINT_CREATED, timeline.PROVIDER_SWITCHED,
                     timeline.SOURCE_MISSING, timeline.SOURCE_RETURNED):
        assert expected in kinds, "%s missing from %s" % (expected, kinds)

    # chronological, and stable
    stamps = [e["timestamp"] for e in tl["events"]]
    assert stamps == sorted(stamps)
    assert kinds == [e["event_type"] for e in
                     timeline.build_thread_timeline(store, tid)["events"]], \
        "the order must be reproducible"

    # the story reads in the right sequence
    assert kinds.index(timeline.CHECKPOINT_CREATED) < \
        kinds.index(timeline.PROVIDER_SWITCHED) < \
        kinds.index(timeline.SOURCE_MISSING) < \
        kinds.index(timeline.SOURCE_RETURNED)

    # both providers are represented
    provs = {e["provider"] for e in tl["events"] if e["provider"]}
    assert provs == {"claude", "codex"}


def test_no_duplicate_events(store, story_thread):
    tid, _ = story_thread
    tl = timeline.build_thread_timeline(store, tid)
    ids = [e["id"] for e in tl["events"]]
    assert len(ids) == len(set(ids)), "event ids must be unique"
    assert tl["total"] == len(tl["events"])


def test_retained_is_described_as_retained_not_deleted(store, story_thread):
    """O3.3: the wording matters.  A rotated source is not a deletion."""
    tid, _ = story_thread
    tl = timeline.build_thread_timeline(store, tid)
    missing = [e for e in tl["events"]
               if e["event_type"] == timeline.SOURCE_MISSING]
    assert len(missing) == 1
    e = missing[0]
    assert e["source_state"] == "SOURCE_MISSING"
    assert "retained" in e["summary"].lower()
    blob = " ".join((x["summary"] or "") for x in tl["events"]).lower()
    for wrong in ("deleted", "lost", "removed"):
        assert wrong not in blob, "must not describe retention as %r" % wrong

    returned = [e for e in tl["events"]
                if e["event_type"] == timeline.SOURCE_RETURNED]
    assert returned and "restored" in returned[0]["summary"].lower()
    assert returned[0]["source_state"] == "ACTIVE_SOURCE"


# --- evidence discipline ---------------------------------------------------

def test_transcript_prose_never_becomes_an_event(store, tmp_path):
    """"This sentence looks like a milestone" is a guess, not evidence."""
    src = _src(tmp_path, "p.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="prose")
    store.replace_session(
        _session("codex:p1", "codex", "prose"),
        [{"sid": "codex:p1", "ts": 1000.0, "seq": 0, "kind": "assistant",
          "content": "Milestone: handoff to Claude complete. Committed abc123. "
                     "Blocker resolved. Tests passed."}],
        "codex", src)
    store.thread_attach(tid, "codex:p1")

    tl = timeline.build_thread_timeline(store, tid)
    kinds = {e["event_type"] for e in tl["events"]}
    assert kinds == {timeline.THREAD_CREATED, timeline.SESSION_ATTACHED}, \
        "assistant prose must not be mined for milestones"


def test_checkpoint_events_come_from_explicit_records(store, tmp_path):
    tid = store.thread_create(repo_root="E:/proj/demo", title="cp")
    checkpoint_create(store, tid, goal="g", phase="build",
                      blockers=[{"description": "flaky test",
                                 "severity": "high"}],
                      tests=["pytest -q"], head_commit="deadbeefcafe")
    tl = timeline.build_thread_timeline(store, tid)
    kinds = [e["event_type"] for e in tl["events"]]
    assert timeline.CHECKPOINT_CREATED in kinds
    assert timeline.BLOCKER_ADDED in kinds
    assert timeline.TEST_GATE in kinds
    assert timeline.COMMIT_OBSERVED in kinds
    blocker = next(e for e in tl["events"]
                   if e["event_type"] == timeline.BLOCKER_ADDED)
    assert "flaky test" in blocker["summary"]
    commit = next(e for e in tl["events"]
                  if e["event_type"] == timeline.COMMIT_OBSERVED)
    assert "deadbeefcafe"[:12] in commit["summary"]


def test_blocker_resolved_needs_explicit_evidence(store):
    tid = store.thread_create(repo_root="E:/proj/demo", title="b")
    # blocker disappears but nothing records it as done -> no RESOLVED event
    checkpoint_create(store, tid, goal="g",
                      blockers=[{"description": "X", "severity": "high"}])
    checkpoint_create(store, tid, goal="g", blockers=[])
    kinds = [e["event_type"] for e in
             timeline.build_thread_timeline(store, tid)["events"]]
    assert timeline.BLOCKER_RESOLVED not in kinds

    # recorded as a milestone -> RESOLVED
    tid2 = store.thread_create(repo_root="E:/proj/demo", title="b2")
    checkpoint_create(store, tid2, goal="g",
                      blockers=[{"description": "Y", "severity": "high"}],
                      milestones=[{"title": "Y", "status": "completed"}])
    kinds2 = [e["event_type"] for e in
              timeline.build_thread_timeline(store, tid2)["events"]]
    assert timeline.BLOCKER_RESOLVED in kinds2


def test_status_transitions_are_logged(store, tmp_path):
    """`threads.status` has no timestamp and `updated_at` is also written by
    thread_touch, so the transition has to be logged when it happens."""
    tid = store.thread_create(repo_root="E:/proj/demo", title="lifecycle")
    store.thread_set_status(tid, "closed")
    store.thread_set_status(tid, "active")
    store.thread_set_status(tid, "archived")

    kinds = [e["event_type"] for e in
             timeline.build_thread_timeline(store, tid)["events"]]
    assert kinds[-3:] == [timeline.THREAD_CLOSED, timeline.THREAD_REOPENED,
                          timeline.THREAD_ARCHIVED]


def test_handoff_same_provider_is_not_a_switch(store):
    tid = store.thread_create(repo_root="E:/proj/demo", title="h")
    store.pending_record(tid, "codex", source_provider="codex",
                         source_session="codex:z", note="forced bundle")
    kinds = [e["event_type"] for e in
             timeline.build_thread_timeline(store, tid)["events"]]
    assert timeline.HANDOFF in kinds
    assert timeline.PROVIDER_SWITCHED not in kinds


# --- contract + filters ----------------------------------------------------

def test_json_contract_fields(store, story_thread):
    import json
    tid, _ = story_thread
    tl = timeline.build_thread_timeline(store, tid)
    required = {"id", "timestamp", "event_type", "thread_id", "provider",
                "session_id", "title", "summary", "source_state", "metadata"}
    for e in tl["events"]:
        assert required <= set(e), required - set(e)
    json.dumps(tl)                     # serialisable as-is
    assert set(tl) >= {"thread", "events", "total", "counts", "filters"}


def test_filters(store, story_thread):
    tid, _ = story_thread
    by_kind = timeline.build_thread_timeline(
        store, tid, kinds=["HANDOFF", "PROVIDER_SWITCHED"])
    assert {e["event_type"] for e in by_kind["events"]} <= {
        timeline.HANDOFF, timeline.PROVIDER_SWITCHED}

    by_prov = timeline.build_thread_timeline(store, tid, provider="claude")
    assert by_prov["events"]
    assert {e["provider"] for e in by_prov["events"]} == {"claude"}

    live = timeline.build_thread_timeline(store, tid, state="live")
    assert all(e["source_state"] != "SOURCE_MISSING" for e in live["events"])

    retained = timeline.build_thread_timeline(store, tid, state="retained")
    assert retained["events"], "the retained episode must be findable"
    assert all(e["source_state"] == "SOURCE_MISSING"
               for e in retained["events"])


def test_limit_keeps_the_newest_and_restores_order(store, story_thread):
    tid, _ = story_thread
    full = timeline.build_thread_timeline(store, tid)
    capped = timeline.build_thread_timeline(store, tid, limit=2)
    assert capped["shown"] == 2 and capped["total"] == full["total"]
    assert [e["id"] for e in capped["events"]] == \
        [e["id"] for e in full["events"]][-2:]
    stamps = [e["timestamp"] for e in capped["events"]]
    assert stamps == sorted(stamps), "still chronological after the cap"


def test_unknown_thread_is_an_error_not_a_crash(store):
    tl = timeline.build_thread_timeline(store, "thr_nope")
    assert "error" in tl and "no such WorkThread" in tl["error"]


# --- performance sanity ----------------------------------------------------

def test_large_thread_timeline_is_bounded(store, tmp_path):
    """O3.9: the timeline must not scan the events table.

    A thread with 60 sessions and 6,000 events still produces a timeline whose
    cost is its own lifecycle rows — so the query count stays flat instead of
    growing with transcript size.
    """
    tid = store.thread_create(repo_root="E:/proj/demo", title="big")
    for i in range(60):
        src = _src(tmp_path, "s%d.jsonl" % i)
        sid = "codex:big%02d" % i
        store.replace_session(
            _session(sid, "codex", "big %d" % i),
            [{"sid": sid, "ts": 1000.0 + j, "seq": j, "kind": "user",
              "content": "line %d of session %d" % (j, i)} for j in range(100)],
            "codex", src)
        store.thread_attach(tid, sid)

    assert store.q("SELECT COUNT(*) n FROM events")[0]["n"] == 6000

    counted = {"n": 0}
    real_q = store.q

    def counting_q(sql, *a):
        counted["n"] += 1
        return real_q(sql, *a)

    store.q = counting_q
    try:
        tl = timeline.build_thread_timeline(store, tid)
    finally:
        store.q = real_q

    assert len(tl["events"]) == 61, "60 attachments + the thread itself"
    # a fixed handful of thread-scoped queries, not one per event/session
    assert counted["n"] <= 10, "query count must not scale with transcript size"


# --- dashboard consumer + XSS ---------------------------------------------

def test_dashboard_renders_the_timeline(store, story_thread):
    from voyager import dashboard

    tid, _ = story_thread
    html = dashboard.render_html(dashboard.build(store))
    assert "WorkThread timeline" in html
    assert "THREAD_CREATED" in html
    assert "SOURCE_RETURNED" in html
    # the O3.4 filters are present and drive a client-side function
    for el in ("tlProv", "tlKind", "tlState", "filterTimeline"):
        assert el in html, el


def test_dashboard_marks_retained_history(store, tmp_path):
    """O3.3: the retained state is first-class on the page, and the wording is
    'retained' — never 'deleted' or 'lost'."""
    from voyager import dashboard

    src = _src(tmp_path, "d.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="d")
    store.replace_session(_session("codex:d1", "codex", "d work"),
                          _events("codex:d1", "d work"), "codex", src)
    store.thread_attach(tid, "codex:d1")
    src.unlink()
    store.prune_missing_sessions("codex", set())

    html = dashboard.render_html(dashboard.build(store))
    assert "SOURCE_MISSING" in html
    assert "retained" in html.lower()
    assert "source unavailable" in html.lower()
    low = html.lower()
    for wrong in ("deleted", "lost"):
        assert wrong not in low, "must not describe retention as %r" % wrong


def test_dashboard_escapes_untrusted_timeline_text(store, tmp_path):
    """Every field on a timeline row is untrusted: it came from an agent."""
    from voyager import dashboard

    nasty = "<script>alert(1)</script>"
    src = _src(tmp_path, "x.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title=nasty)
    store.replace_session(_session("codex:x", "codex", nasty),
                          _events("codex:x", nasty), "codex", src)
    store.thread_attach(tid, "codex:x")

    html = dashboard.render_html(dashboard.build(store))
    assert "<script>alert(1)</script>" not in html, "raw payload must not survive"
    assert "&lt;script&gt;" in html


def test_dashboard_stays_self_contained_with_a_timeline(store, story_thread):
    from voyager import dashboard

    html = dashboard.render_html(dashboard.build(store))
    assert "http://" not in html and "https://" not in html
    assert "<link" not in html
    assert html.count("<script") == 1


def test_api_exposes_the_timeline_op(store, story_thread):
    """The UIs consume this op; it must be the canonical model, not a copy."""
    from voyager import api
    from voyager.timeline import build_thread_timeline

    tid, _ = story_thread
    db = store.db_path
    store.close()

    assert "thread_timeline" in api._OPS, "the stdio bridge must offer it"
    tl = api.thread_timeline(db=db, thread_id=tid, limit=5)
    assert tl["thread"]["id"] == tid
    assert tl["shown"] <= 5

    s = Store(db)
    try:
        direct = build_thread_timeline(s, tid)
    finally:
        s.close()
    assert tl["counts"] == direct["counts"], "one model, not two"


# --- CLI ------------------------------------------------------------------

def test_cli_timeline_text_and_json(store, story_thread, capsys):
    from voyager.cli import main

    tid, _ = story_thread
    db = store.db_path
    store.close()

    assert main(["--db", str(db), "thread", "timeline", tid]) == 0
    out = capsys.readouterr().out
    assert "THREAD_CREATED" in out
    assert "PROVIDER_SWITCHED" in out
    assert "retained" in out.lower()

    import json
    assert main(["--db", str(db), "thread", "timeline", tid, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["thread"]["id"] == tid
    assert any(e["event_type"] == "SOURCE_RETURNED" for e in payload["events"])

    assert main(["--db", str(db), "thread", "timeline", "thr_missing"]) == 1
    assert "no such WorkThread" in capsys.readouterr().err
