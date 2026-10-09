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
def story_thread(store, tmp_path, monkeypatch):
    """Claude → checkpoint → handoff to Codex → work → source rotates →
    retained → source returns.  Returns (thread_id, codex_src).

    The steps run through an explicit clock rather than the wall clock.  On a
    fast machine they all land in the same tick, and a timeline can only order
    same-tick events by an arbitrary source priority -- the rows carry no
    causal order to recover.  That made the ordering assertion below a
    machine-speed race (green on the Ubuntu legs, red on Windows).  Driving
    the clock makes the fixture's sequence the timeline's sequence everywhere.
    """
    import voyager.checkpoint as checkpoint_mod

    # `checkpoint_mod.time` is the shared `time` module object, and store.py
    # resolves it through a function-local `import time as _time`, so patching
    # the module's attribute once drives every clock in this fixture.
    ticks = iter(range(1000, 1400))
    monkeypatch.setattr(checkpoint_mod.time, "time", lambda: float(next(ticks)))

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

    A thread with 100 sessions and 10,000 events still produces a timeline whose
    cost is its own lifecycle rows — so the query count stays flat instead of
    growing with transcript size.
    """
    tid = store.thread_create(repo_root="E:/proj/demo", title="big")
    for i in range(100):
        src = _src(tmp_path, "s%d.jsonl" % i)
        sid = "codex:big%02d" % i
        store.replace_session(
            _session(sid, "codex", "big %d" % i),
            [{"sid": sid, "ts": 1000.0 + j, "seq": j, "kind": "user",
              "content": "line %d of session %d" % (j, i)} for j in range(100)],
            "codex", src)
        store.thread_attach(tid, sid)

    assert store.q("SELECT COUNT(*) n FROM events")[0]["n"] == 10000

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

    assert len(tl["events"]) == 101, "100 attachments + the thread itself"
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


# --- O3 final: ordering, provenance, episodes, read-only -------------------

def test_same_timestamp_order_is_deterministic_and_numeric(store):
    """Test 1: many events sharing one timestamp still come out in a fixed,
    sane order on every call — persisted rows in insertion order (numeric,
    not the lexical log:9 < log:12 trap)."""
    tid = store.thread_create(repo_root="E:/proj/demo", title="tie")
    for _ in range(12):
        store.thread_event_record(tid, "THREAD_STATUS", ts=500.0,
                                  detail={"note": "same second"})
    first = timeline.build_thread_timeline(store, tid)
    second = timeline.build_thread_timeline(store, tid)
    ids1 = [e["id"] for e in first["events"] if e["timestamp"] == 500.0]
    ids2 = [e["id"] for e in second["events"] if e["timestamp"] == 500.0]
    assert ids1 == ids2, "same DB + same query must give the same order"
    assert ids1 == ["log:%d" % i for i in range(1, 13)], \
        "same-ts persisted rows sort by physical row id, not lexically"


def test_thread_created_derived_exactly_once(store):
    """Test 2: THREAD_CREATED derives from threads.created_at and is never
    double-written, no matter how often the timeline is queried."""
    tid = store.thread_create(repo_root="E:/proj/demo", title="once")
    for _ in range(3):
        tl = timeline.build_thread_timeline(store, tid)
        created = [e for e in tl["events"]
                   if e["event_type"] == timeline.THREAD_CREATED]
        assert len(created) == 1
        assert created[0]["source"] == "threads"
    assert store.q("SELECT COUNT(*) n FROM thread_events"
                   " WHERE kind='THREAD_CREATED'")[0]["n"] == 0, \
        "a derivable fact must not be persisted"


def test_session_attach_derived_once_and_writes_nothing(store):
    """Test 3: SESSION_ATTACHED derives from thread_sessions.attached_at;
    repeated timeline queries add no database rows."""
    tid = store.thread_create(repo_root="E:/proj/demo", title="att")
    src = store.db_path.parent / "att.jsonl"
    src.write_text("{}", encoding="utf-8")
    store.replace_session(_session("codex:a1", "codex", "att"),
                          _events("codex:a1", "att"), "codex", src)
    store.thread_attach(tid, "codex:a1")

    before = store.q("SELECT COUNT(*) n FROM thread_events")[0]["n"]
    rows1 = [e for e in timeline.build_thread_timeline(store, tid)["events"]
             if e["event_type"] == timeline.SESSION_ATTACHED]
    rows2 = [e for e in timeline.build_thread_timeline(store, tid)["events"]
             if e["event_type"] == timeline.SESSION_ATTACHED]
    assert len(rows1) == len(rows2) == 1
    assert rows1[0]["source"] == "thread_sessions"
    assert store.q("SELECT COUNT(*) n FROM thread_events")[0]["n"] == before, \
        "deriving the timeline must not write"


def test_repeated_prune_keeps_one_missing_episode(store, tmp_path):
    """Test 4: repeated scans over a missing source do not stack up
    SOURCE_MISSING events."""
    src = _src(tmp_path, "rp.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="rp")
    store.replace_session(_session("codex:rp", "codex", "rp"), _events("codex:rp", "rp"),
                          "codex", src)
    store.thread_attach(tid, "codex:rp")
    src.unlink()
    for _ in range(3):
        store.prune_missing_sessions("codex", set())
        n = [e for e in timeline.build_thread_timeline(store, tid)["events"]
             if e["event_type"] == timeline.SOURCE_MISSING]
        assert len(n) == 1


def test_missing_return_missing_episodes_all_present(store, tmp_path):
    """Tests 5+6: ACTIVE→MISSING→ACTIVE→MISSING keeps all three transitions;
    nothing is compressed into the current state."""
    src = _src(tmp_path, "ep.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="ep")
    store.replace_session(_session("codex:ep", "codex", "ep"), _events("codex:ep", "ep"),
                          "codex", src)
    store.thread_attach(tid, "codex:ep")

    src.unlink()
    store.prune_missing_sessions("codex", set())          # missing #1
    src.write_text("{}", encoding="utf-8")
    store.replace_session(_session("codex:ep", "codex", "ep"),
                          _events("codex:ep", "ep"), "codex", src)
    store.prune_missing_sessions("codex", {str(src)})     # returned
    src.unlink()
    store.prune_missing_sessions("codex", set())          # missing #2

    kinds = [e["event_type"] for e in
             timeline.build_thread_timeline(store, tid)["events"]]
    tail = [k for k in kinds if k in (timeline.SOURCE_MISSING,
                                      timeline.SOURCE_RETURNED)]
    assert tail == [timeline.SOURCE_MISSING, timeline.SOURCE_RETURNED,
                    timeline.SOURCE_MISSING]


def test_archive_transition_recorded_once(store, tmp_path):
    """Test 7: an explicit archive writes SOURCE_ARCHIVED with the state it
    came from; archiving again records nothing."""
    src = _src(tmp_path, "ar.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="ar")
    store.replace_session(_session("codex:ar", "codex", "ar"), _events("codex:ar", "ar"),
                          "codex", src)
    store.thread_attach(tid, "codex:ar")

    assert store.mark_canonical_archived("codex:ar") is True
    assert store.mark_canonical_archived("codex:ar") is False, \
        "a repeat archive changes nothing"
    evs = [e for e in timeline.build_thread_timeline(store, tid)["events"]
           if e["event_type"] == timeline.SOURCE_ARCHIVED]
    assert len(evs) == 1
    assert evs[0]["metadata"]["from"] == "ACTIVE_SOURCE"
    assert evs[0]["source_state"] == "ARCHIVED_CANONICAL"

    # a retained session archived later reports its true previous state
    src2 = _src(tmp_path, "ar2.jsonl")
    store.replace_session(_session("codex:ar2", "codex", "ar2"),
                          _events("codex:ar2", "ar2"), "codex", src2)
    store.thread_attach(tid, "codex:ar2")
    src2.unlink()
    store.prune_missing_sessions("codex", set())
    store.mark_canonical_archived("codex:ar2")
    evs = [e for e in timeline.build_thread_timeline(store, tid)["events"]
           if e["event_type"] == timeline.SOURCE_ARCHIVED]
    assert len(evs) == 2
    assert {e["metadata"]["from"] for e in evs} == {
        "ACTIVE_SOURCE", "SOURCE_MISSING"}


def test_logged_and_derived_missing_never_double_count(store, tmp_path):
    """Test 8: a SOURCE_MISSING that is both logged and visible in the
    current state appears exactly once."""
    src = _src(tmp_path, "dc.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="dc")
    store.replace_session(_session("codex:dc", "codex", "dc"), _events("codex:dc", "dc"),
                          "codex", src)
    store.thread_attach(tid, "codex:dc")
    src.unlink()
    store.prune_missing_sessions("codex", set())   # logs AND leaves the mark

    evs = [e for e in timeline.build_thread_timeline(store, tid)["events"]
           if e["event_type"] == timeline.SOURCE_MISSING]
    assert len(evs) == 1
    assert store.q("SELECT COUNT(*) n FROM thread_events"
                   " WHERE kind='SOURCE_MISSING'")[0]["n"] == 1


def test_timeline_query_is_read_only(store, story_thread):
    """Test 9: building a timeline leaves the database byte-identical."""
    import hashlib
    tid, _ = story_thread

    def digest():
        h = hashlib.sha256()
        for suffix in ("", "-wal"):
            p = Path(str(store.db_path) + suffix)
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()

    before = digest()
    for _ in range(3):
        timeline.build_thread_timeline(store, tid)
        timeline.build_thread_timeline(store, tid, state="retained")
    assert digest() == before


def test_current_state_marker_is_current_not_event_time(store, tmp_path):
    """Test 10: a retained session's non-retention events carry the CURRENT
    source_state without their own timestamps being touched."""
    src = _src(tmp_path, "cm.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="cm")
    store.replace_session(_session("codex:cm", "codex", "cm"), _events("codex:cm", "cm"),
                          "codex", src)
    store.thread_attach(tid, "codex:cm")
    attached_at = store.q("SELECT attached_at a FROM thread_sessions"
                          " WHERE session_id='codex:cm'")[0]["a"]
    src.unlink()
    store.prune_missing_sessions("codex", set())

    att = [e for e in timeline.build_thread_timeline(store, tid)["events"]
           if e["event_type"] == timeline.SESSION_ATTACHED][0]
    assert att["source_state"] == "SOURCE_MISSING", \
        "the current retention state is visible on the item"
    assert att["timestamp"] == attached_at, \
        "the current state must not rewrite the event's own time"


def test_archived_visible_in_timeline_excluded_from_continuity(store, tmp_path):
    """Test 11: ARCHIVED_CANONICAL shows historically but never feeds live
    continuity — the O2 invariant survives the timeline work."""
    from voyager.auto import get_continuation_context

    live_src = _src(tmp_path, "av-live.jsonl")
    arch_src = _src(tmp_path, "av-arch.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="av")
    store.replace_session(_session("codex:avl", "codex", "avl live"),
                          _events("codex:avl", "avl live"), "codex", live_src)
    store.replace_session(_session("codex:ava", "codex", "avl gone"),
                          _events("codex:ava", "avl gone"), "codex", arch_src)
    store.thread_attach(tid, "codex:avl")
    store.thread_attach(tid, "codex:ava")
    store.mark_canonical_archived("codex:ava")

    tl = timeline.build_thread_timeline(store, tid)
    kinds = [e["event_type"] for e in tl["events"]]
    assert timeline.SOURCE_ARCHIVED in kinds, "archived stays visible"
    archived_items = [e for e in tl["events"]
                      if e["session_id"] == "codex:ava"]
    assert archived_items, "the archived session's history remains"
    assert all(e["source_state"] == "ARCHIVED_CANONICAL"
               for e in archived_items)

    res = get_continuation_context(store=store, thread_id=tid)
    ctx = res.get("context") or ""
    assert "avl live" in ctx
    assert "avl gone" not in ctx, "archived must not feed continuity"


def test_cli_and_api_share_the_canonical_semantics(store, story_thread, capsys):
    """Test 12: the CLI's --json events and the API op agree on kinds and
    order for the same thread (wording may differ, semantics may not)."""
    import json as _json
    from voyager import api
    from voyager.cli import main

    tid, _ = story_thread
    db = store.db_path
    store.close()

    assert main(["--db", str(db), "thread", "timeline", tid, "--json"]) == 0
    cli_tl = _json.loads(capsys.readouterr().out)
    api_tl = api.thread_timeline(db=db, thread_id=tid)
    cli_pairs = [(e["event_type"], e["id"]) for e in cli_tl["events"]]
    api_pairs = [(e["event_type"], e["id"]) for e in api_tl["events"]]
    assert cli_pairs == api_pairs, "one model, two surfaces"


def test_dashboard_consumes_the_canonical_builder():
    """Test 13: the dashboard imports and calls the canonical builder; it
    aggregates nothing of its own."""
    src = (Path(__file__).parent.parent / "voyager" / "dashboard.py").read_text(
        encoding="utf-8")
    assert "from .timeline import build_thread_timeline" in src
    assert "build_thread_timeline(store" in src, "actually called"


def test_timeline_on_a_database_without_checkpoints(tmp_path):
    """Test 14: a pre-checkpoint database (core schema only) opens and reads
    a timeline without crashing."""
    s = Store(tmp_path / "old.db")     # init_checkpoint_schema NOT called
    try:
        tid = s.thread_create(repo_root="E:/proj/demo", title="old")
        tl = timeline.build_thread_timeline(s, tid)
        assert "error" not in tl
        assert [e["event_type"] for e in tl["events"]] == \
            [timeline.THREAD_CREATED]
    finally:
        s.close()


def test_status_noop_restatements_record_nothing(store):
    """O3 §12: active→active is a restatement, not a transition; and the
    transition detail carries from → to."""
    tid = store.thread_create(repo_root="E:/proj/demo", title="noop")
    store.thread_set_status(tid, "active")            # already active
    store.thread_set_status(tid, "closed")
    store.thread_set_status(tid, "closed")            # repeat: no event
    evs = timeline.build_thread_timeline(store, tid)["events"]
    status_evs = [e for e in evs if e["event_type"] in
                  (timeline.THREAD_CLOSED, timeline.THREAD_REOPENED,
                   timeline.THREAD_ARCHIVED, "THREAD_STATUS")]
    assert len(status_evs) == 1
    assert status_evs[0]["metadata"] == {"from": "active", "to": "closed"}


def test_provenance_names_the_canonical_table(store, story_thread):
    """O3 §18: every event names where its fact lives."""
    tid, _ = story_thread
    tl = timeline.build_thread_timeline(store, tid)
    known = {"threads", "thread_sessions", "thread_pending", "checkpoints",
             "thread_events", "sessions:source_state"}
    for e in tl["events"]:
        assert e["source"] in known, e["id"]
    by_kind = {e["event_type"]: e["source"] for e in tl["events"]}
    assert by_kind[timeline.THREAD_CREATED] == "threads"
    assert by_kind[timeline.SESSION_ATTACHED] == "thread_sessions"
    assert by_kind[timeline.CHECKPOINT_CREATED] == "checkpoints"
    assert by_kind[timeline.SOURCE_MISSING] in (
        "thread_events", "sessions:source_state")
    assert by_kind[timeline.SOURCE_RETURNED] == "thread_events"


def test_pending_resolution_is_derived_from_resolved_at(store):
    """O3 §15: a resolved pending row yields PENDING_ATTACH_RESOLVED at the
    real resolution time, with the outcome in the detail."""
    tid = store.thread_create(repo_root="E:/proj/demo", title="pend")
    store.pending_record(tid, "codex", source_provider="claude",
                         source_session="claude:c1", note="switch")
    rid = store.q("SELECT rowid rid FROM thread_pending"
                  " WHERE thread_id=?", (tid,))[0]["rid"]
    store.pending_mark(rid, "resolved", "codex:x1")

    tl = timeline.build_thread_timeline(store, tid)
    evs = [e for e in tl["events"]
           if e["event_type"] == timeline.PENDING_ATTACH_RESOLVED]
    assert len(evs) == 1
    assert evs[0]["session_id"] == "codex:x1"
    assert evs[0]["metadata"]["status"] == "resolved"
    assert evs[0]["timestamp"] == store.q(
        "SELECT resolved_at a FROM thread_pending WHERE rowid=?", (rid,)
    )[0]["a"]


# --- O3 patch: filter contract + adversarial ordering ----------------------

def test_state_filter_partitions_live_retained_archived(store, tmp_path):
    """state=live admits only ACTIVE_SOURCE (plus stateless thread-level
    events); retained/archived never bleed into it and never take each
    other's events."""
    live_src = _src(tmp_path, "fp-live.jsonl")
    gone_src = _src(tmp_path, "fp-gone.jsonl")
    arch_src = _src(tmp_path, "fp-arch.jsonl")
    tid = store.thread_create(repo_root="E:/proj/demo", title="fp")
    for sid, src, needle in (("codex:fpl", live_src, "fp live"),
                             ("codex:fpg", gone_src, "fp gone"),
                             ("codex:fpa", arch_src, "fp arch")):
        store.replace_session(_session(sid, "codex", needle),
                              _events(sid, needle), "codex", src)
        store.thread_attach(tid, sid)
    gone_src.unlink()
    store.prune_missing_sessions("codex", {str(live_src), str(arch_src)})
    store.mark_canonical_archived("codex:fpa")

    live = timeline.build_thread_timeline(store, tid, state="live")
    retained = timeline.build_thread_timeline(store, tid, state="retained")
    archived = timeline.build_thread_timeline(store, tid, state="archived")

    live_sessions = {e["session_id"] for e in live["events"]
                     if e["session_id"]}
    assert live_sessions == {"codex:fpl"}, live_sessions
    assert all(e["source_state"] in (None, "ACTIVE_SOURCE")
               for e in live["events"]), \
        "no historical state may pass state=live"
    # thread-level lifecycle (no session state) stays visible in 'live'
    assert any(e["event_type"] == timeline.THREAD_CREATED
               for e in live["events"])
    assert any(e["event_type"] == timeline.SESSION_ATTACHED
               and e["session_id"] == "codex:fpl"
               for e in live["events"])

    assert {e["session_id"] for e in retained["events"]
            if e["session_id"]} == {"codex:fpg"}
    assert {e["session_id"] for e in archived["events"]
            if e["session_id"]} == {"codex:fpa"}
    # and the three sets share nothing
    live_s = {e["id"] for e in live["events"] if e["session_id"]}
    ret_s = {e["id"] for e in retained["events"]}
    arc_s = {e["id"] for e in archived["events"]}
    assert not (live_s & ret_s) and not (live_s & arc_s) and not (ret_s & arc_s)


def test_pending_same_timestamp_order_is_stable_across_reconnects(store):
    """Two pendings created in the same second keep one fixed order —
    derived from the (created_at, provider) SQL order, not query luck —
    and the order survives a full close/reopen of the database."""
    tid = store.thread_create(repo_root="E:/proj/demo", title="tiep")
    store.pending_record(tid, "zcode", source_provider="claude",
                         source_session="claude:c1", note="to zcode")
    store.pending_record(tid, "codex", source_provider="claude",
                         source_session="claude:c1", note="to codex")
    store.con.execute("UPDATE thread_pending SET created_at=500.0"
                      " WHERE thread_id=?", (tid,))
    store.con.commit()                              # force the tie (durably)

    def pend_ids():
        return [e["id"] for e in
                timeline.build_thread_timeline(store, tid)["events"]
                if e["id"].startswith("pending")]

    first = pend_ids()
    second = pend_ids()
    assert first == second
    assert first == ["pending:%s:codex:500.0" % tid,
                     "pending:%s:zcode:500.0" % tid], \
        "alphabetical provider order, independent of insert order"

    db = store.db_path
    store.close()
    s = Store(db)
    try:
        reopened = [e["id"] for e in
                    timeline.build_thread_timeline(s, tid)["events"]
                    if e["id"].startswith("pending")]
        assert reopened == first, "order survives a reconnect"
    finally:
        s.close()


def test_checkpoint_same_timestamp_order_is_stable_across_reconnects(store):
    """Two checkpoints written in the same second keep one fixed order —
    the ORDER BY created_at, id makes the SQL itself deterministic — and
    the order survives a close/reopen."""
    tid = store.thread_create(repo_root="E:/proj/demo", title="tiec")
    checkpoint_create(store, tid, goal="first", phase="alpha")
    checkpoint_create(store, tid, goal="second", phase="beta")
    store.con.execute("UPDATE checkpoints SET created_at=900.0"
                      " WHERE thread_id=?", (tid,))
    store.con.commit()                           # force the tie (durably)

    def chk_ids():
        return [e["id"] for e in
                timeline.build_thread_timeline(store, tid)["events"]
                if e["id"].startswith("chk:")]

    first = chk_ids()
    assert len(first) == 2
    assert first == sorted(first), "stable id order, not insertion luck"
    assert chk_ids() == first

    db = store.db_path
    store.close()
    s = Store(db)
    try:
        assert [e["id"] for e in
                timeline.build_thread_timeline(s, tid)["events"]
                if e["id"].startswith("chk:")] == first
    finally:
        s.close()


def test_checkpoints_created_in_the_same_tick_do_not_collide(store, monkeypatch):
    """A checkpoint id must be unique even when the clock does not move.

    `checkpoint_create` built its primary key from `time.time()`.  That is not
    a unique key: on Windows its resolution is about 15.6 ms, so two
    checkpoints created back to back landed on the same value and the second
    died with `UNIQUE constraint failed: checkpoints.id` -- which is what the
    Windows CI legs hit, twice per run.

    Freezing the clock makes that deterministic instead of a race: the ids have
    to differ even when the timestamp does not.
    """
    import voyager.checkpoint as checkpoint_mod

    monkeypatch.setattr(checkpoint_mod.time, "time", lambda: 1700000000.0)

    tid = store.thread_create(repo_root="E:/proj/demo", title="same-tick")
    first = checkpoint_create(store, tid, goal="first", phase="alpha")
    second = checkpoint_create(store, tid, goal="second", phase="beta")

    assert first != second, "two checkpoints in the same tick share an id"
    rows = store.q("SELECT id FROM checkpoints WHERE thread_id=?", (tid,))
    assert len(rows) == 2, f"expected both checkpoints to persist, got {rows}"
