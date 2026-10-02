"""O2 — Source Rotation / Historical Retention.

The invariant this file exists to protect:

    absence of a provider source is not proof that the user wants the history
    deleted.

Before O2, `store.prune_missing_sessions` **deleted** the session, its events,
its file rows and its FTS rows as soon as every one of its source files had
vanished from disk. Because the provider file is gone at that point, the index
held the only normalized copy — so a provider rotating its own storage (Codex
rolling old rollouts, `~/.claude/projects` cleared, ZCode/Cursor swapping a
database) silently erased history the user never asked to remove.

The contract now:

* `sessions.source_state` is NULL/`LIVE` or `SOURCE_MISSING`. There is no
  separate `RETAINED` state: nothing in O2 behaves differently for it, and a
  state that exists only to look tidy is a state that will be set wrongly.
* only **all** sources missing marks a session — one surviving source keeps it
  `LIVE`;
* a marked session keeps its events, files and FTS rows, so search, timeline
  and thread summaries still find it;
* it is excluded from *continuity* (context compilation, resume candidates);
* re-ingesting the same native session id reconciles back to one `LIVE` row;
* there is **no** automatic purge.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from voyager.store import Store


def _session(sid: str, needle: str, *, native: str | None = None,
             can_resume: bool = False) -> dict:
    return {
        "id": sid, "provider": "codex", "native_session_id": native or sid.split(":")[-1],
        "title": "work " + needle, "started_at": 1000.0, "updated_at": 2000.0,
        "cwd": "E:/proj/demo", "repo_root": "E:/proj/demo",
        "message_count": 1, "tool_count": 0,
        "can_resume": can_resume, "can_fork": False,
        "resume_cmd": "codex resume " + (native or sid) if can_resume else None,
        "metadata": {}, "raw_metadata": {},
    }


def _events(sid: str, needle: str) -> list:
    return [{"sid": sid, "ts": 2000.0, "seq": 0, "kind": "user",
             "content": needle}]


def _src(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("{}", encoding="utf-8")
    return p


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "o2.db")
    yield s
    s.close()


# --- 1. the bug itself -----------------------------------------------------

def test_source_rotation_keeps_history_searchable(store, tmp_path):
    """The regression: rotate the source, and the history must survive."""
    src = _src(tmp_path, "rollout-a.jsonl")
    store.replace_session(_session("codex:a", "unique-needle-alpha"),
                          _events("codex:a", "unique-needle-alpha"),
                          "codex", src)
    assert store.search("unique-needle-alpha"), "precondition: searchable"

    src.unlink()                      # the provider rotated its storage
    store.prune_missing_sessions("codex", set())      # nothing left on disk

    assert store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 1, \
        "a vanished source must not delete the canonical session"
    assert store.q("SELECT COUNT(*) n FROM events")[0]["n"] == 1
    assert store.search("unique-needle-alpha"), \
        "retained history must stay searchable"
    assert store.q("SELECT source_state FROM sessions WHERE id='codex:a'")[0][0] \
        == "SOURCE_MISSING"


def test_retained_session_keeps_its_files_and_fts_rows(store, tmp_path):
    src = _src(tmp_path, "rollout-b.jsonl")
    s = _session("codex:b", "needle-beta")
    s["_files"] = [{"path": "E:/proj/demo/x.py", "backup": None,
                    "versions": 1, "versions_json": []}]
    store.replace_session(s, _events("codex:b", "needle-beta"), "codex", src)
    src.unlink()
    store.prune_missing_sessions("codex", set())

    assert store.q("SELECT COUNT(*) n FROM files")[0]["n"] == 1
    assert store.q("SELECT COUNT(*) n FROM event_fts")[0]["n"] == 1


# --- 2. multi-source: only ALL missing counts ------------------------------

def test_one_of_multiple_sources_missing_stays_live(store, tmp_path):
    keep = _src(tmp_path, "keep.jsonl")
    drop = _src(tmp_path, "drop.jsonl")
    store.replace_session(_session("codex:m", "needle-multi"),
                          _events("codex:m", "needle-multi"),
                          "codex", keep, extra_sources=[drop])
    assert store.q("SELECT COUNT(*) n FROM sources")[0]["n"] == 2

    drop.unlink()
    store.prune_missing_sessions("codex", {str(keep)})

    row = store.q("SELECT source_state FROM sessions WHERE id='codex:m'")[0]
    assert row[0] in (None, "LIVE"), "one surviving source means still LIVE"


def test_all_sources_missing_marks_the_session(store, tmp_path):
    a = _src(tmp_path, "a.jsonl")
    b = _src(tmp_path, "b.jsonl")
    store.replace_session(_session("codex:n", "needle-all"),
                          _events("codex:n", "needle-all"),
                          "codex", a, extra_sources=[b])
    a.unlink()
    b.unlink()
    store.prune_missing_sessions("codex", set())

    assert store.q("SELECT source_state FROM sessions WHERE id='codex:n'")[0][0] \
        == "SOURCE_MISSING"
    assert store.q("SELECT source_missing_since FROM sessions "
                   "WHERE id='codex:n'")[0][0] is not None


def test_sessions_without_source_rows_are_never_touched(store, tmp_path):
    """Manually seeded sessions have no lifecycle the caller can know."""
    store.replace_session(_session("codex:manual", "needle-manual"),
                          _events("codex:manual", "needle-manual"),
                          "codex", _src(tmp_path, "manual.jsonl"))
    store.con.execute("DELETE FROM sources WHERE sid='codex:manual'")
    store.con.commit()
    store.prune_missing_sessions("codex", set())
    assert store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 1
    assert store.q("SELECT source_state FROM sessions WHERE id='codex:manual'")[0][0] \
        in (None, "LIVE")


# --- 3. source returns: reconcile, no duplicate ----------------------------

def test_source_return_reconciles_to_one_live_session(store, tmp_path):
    src = _src(tmp_path, "come-back.jsonl")
    store.replace_session(_session("codex:r", "needle-return"),
                          _events("codex:r", "needle-return"), "codex", src)
    src.unlink()
    store.prune_missing_sessions("codex", set())
    assert store.q("SELECT source_state FROM sessions WHERE id='codex:r'")[0][0] \
        == "SOURCE_MISSING"

    # the provider writes the same native session again
    src.write_text("{}", encoding="utf-8")
    store.replace_session(_session("codex:r", "needle-return"),
                          _events("codex:r", "needle-return"), "codex", src)
    store.prune_missing_sessions("codex", {str(src)})

    assert store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 1, "no duplicate"
    assert store.q("SELECT COUNT(*) n FROM events")[0]["n"] == 1, "events refreshed"
    row = store.q("SELECT source_state, source_missing_since FROM sessions "
                  "WHERE id='codex:r'")[0]
    assert row[0] in (None, "LIVE")
    assert row[1] is None, "the missing marker must be cleared on return"


# --- 4. continuity must not use retained history ---------------------------

def test_live_thread_members_excludes_retained(store, tmp_path):
    live_src = _src(tmp_path, "live.jsonl")
    gone_src = _src(tmp_path, "gone.jsonl")
    store.replace_session(_session("codex:live", "needle-live"),
                          _events("codex:live", "needle-live"), "codex", live_src)
    store.replace_session(_session("codex:gone", "needle-gone"),
                          _events("codex:gone", "needle-gone"), "codex", gone_src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="t")
    store.thread_attach(tid, "codex:live")
    store.thread_attach(tid, "codex:gone")

    gone_src.unlink()
    store.prune_missing_sessions("codex", {str(live_src)})

    # display keeps both (marked, not hidden) ...
    assert len(store.thread_members(tid)) == 2
    # ... but continuity only sees the live one.
    ids = [r["id"] for r in store.live_thread_members(tid)]
    assert ids == ["codex:live"]


def test_continuity_context_excludes_retained_members(store, tmp_path):
    from voyager.auto import get_continuation_context

    live_src = _src(tmp_path, "clive.jsonl")
    gone_src = _src(tmp_path, "cgone.jsonl")
    store.replace_session(_session("codex:cl", "needle-context-live"),
                          _events("codex:cl", "needle-context-live"),
                          "codex", live_src)
    store.replace_session(_session("codex:cg", "needle-context-gone"),
                          _events("codex:cg", "needle-context-gone"),
                          "codex", gone_src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="t")
    store.thread_attach(tid, "codex:cl")
    store.thread_attach(tid, "codex:cg")

    gone_src.unlink()
    store.prune_missing_sessions("codex", {str(live_src)})

    res = get_continuation_context(store=store, thread_id=tid)
    ctx = res.get("context") or ""
    assert "needle-context-live" in ctx
    # Without this the test passes for the wrong reason: a *deleted* session is
    # also absent from the context.
    assert store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 2, \
        "the retained session must still exist — exclusion, not deletion"
    assert "needle-context-gone" not in ctx, \
        "a retained session must not feed active continuity"


# --- 5. doctor observability ----------------------------------------------

def test_doctor_reports_retained_sessions(store, tmp_path):
    from voyager import doctor

    src = _src(tmp_path, "doc.jsonl")
    store.replace_session(_session("codex:d", "needle-doc"),
                          _events("codex:d", "needle-doc"), "codex", src)
    src.unlink()
    store.prune_missing_sessions("codex", set())
    store.close()

    report = doctor.run(db_path=tmp_path / "o2.db")
    blob = json.dumps(report, default=str)
    assert "retained" in blob.lower()
    assert "RETENTION" in blob or "retained" in blob.lower()


def test_doctor_retention_is_not_blocking(store, tmp_path):
    from voyager import doctor

    src = _src(tmp_path, "doc2.jsonl")
    store.replace_session(_session("codex:d2", "needle-doc2"),
                          _events("codex:d2", "needle-doc2"), "codex", src)
    src.unlink()
    store.prune_missing_sessions("codex", set())
    store.close()

    report = doctor.run(db_path=tmp_path / "o2.db")
    assert not [i for i in report["blocking"] if "RETENTION" in str(i)]
    assert report["blocking"] == []


# --- 7. the marker travels: brief + API (O3 renders it) --------------------

def test_thread_brief_marks_retained_contributions(store, tmp_path):
    """Marked, not hidden: the agent still contributed to the thread."""
    from voyager.thread_brief import activity, render, summarize

    live_src = _src(tmp_path, "blive.jsonl")
    gone_src = _src(tmp_path, "bgone.jsonl")
    store.replace_session(_session("codex:bl", "needle-brief-live"),
                          _events("codex:bl", "needle-brief-live"),
                          "codex", live_src)
    store.replace_session(_session("codex:bg", "needle-brief-gone"),
                          _events("codex:bg", "needle-brief-gone"),
                          "codex", gone_src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="t")
    store.thread_attach(tid, "codex:bl")
    store.thread_attach(tid, "codex:bg")

    gone_src.unlink()
    store.prune_missing_sessions("codex", {str(live_src)})

    data = activity(store, tid)
    assert len(data["contributions"]) == 2, "the retained one is still listed"
    states = {c["sid"]: c["source_state"] for c in data["contributions"]}
    assert states["codex:bg"] == "SOURCE_MISSING"
    assert states["codex:bl"] in (None, "LIVE")

    out = render(summarize(store, tid))
    assert "[source missing]" in out, "the brief must mark it, not hide it"


def test_api_surfaces_the_retention_state(store, tmp_path):
    from voyager import api

    src = _src(tmp_path, "api.jsonl")
    store.replace_session(_session("codex:api", "needle-api"),
                          _events("codex:api", "needle-api"), "codex", src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="t")
    store.thread_attach(tid, "codex:api")
    src.unlink()
    store.prune_missing_sessions("codex", set())
    db = store.db_path
    store.close()

    detail = api.thread_detail(db=db, thread_id=tid)
    assert detail["members"][0]["source_state"] == "SOURCE_MISSING"
    ov = api.overview(db=db, hours=10 ** 6)
    assert ov["recent_sessions"][0]["source_state"] == "SOURCE_MISSING"


# --- 8. the cost side is measured, not assumed ----------------------------

def test_retained_stats_counts_and_sizes(store, tmp_path):
    """A NULL-content event must not zero the byte estimate.

    `LENGTH(NULL)` is NULL and NULL propagates through `+`, so a single
    tool-call row without content would have made the whole SUM NULL and the
    retained size read as 0.
    """
    src = _src(tmp_path, "size.jsonl")
    store.replace_session(
        _session("codex:sz", "needle-size"),
        [{"sid": "codex:sz", "ts": 2000.0, "seq": 0, "kind": "user",
          "content": "x" * 100},
         {"sid": "codex:sz", "ts": 2001.0, "seq": 1, "kind": "tool_call",
          "tool_name": "shell", "content": None, "tool_input": "y" * 50}],
        "codex", src)
    src.unlink()
    store.prune_missing_sessions("codex", set())

    stats = store.retained_stats()
    assert stats["sessions"] == 1
    assert stats["events"] == 2
    assert stats["bytes"] >= 150, stats["bytes"]
    assert stats["providers"][0]["provider"] == "codex"
    assert stats["oldest"] is not None


def test_retained_stats_is_zero_when_nothing_is_retained(store, tmp_path):
    src = _src(tmp_path, "live.jsonl")
    store.replace_session(_session("codex:lz", "needle-live-z"),
                          _events("codex:lz", "needle-live-z"), "codex", src)
    store.prune_missing_sessions("codex", {str(src)})
    stats = store.retained_stats()
    assert stats["sessions"] == 0 and stats["bytes"] == 0
    assert stats["oldest"] is None


# --- 9. the handoff engine must obey the same rule -------------------------

def _thread_with_one_retained(store, tmp_path, *, resume: bool = False):
    """A thread with one live member and one retained member."""
    live_src = _src(tmp_path, "hlive.jsonl")
    gone_src = _src(tmp_path, "hgone.jsonl")
    store.replace_session(_session("codex:hl", "needle-handoff-live"),
                          _events("codex:hl", "needle-handoff-live"),
                          "codex", live_src)
    store.replace_session(
        _session("codex:hg", "needle-handoff-gone", can_resume=resume),
        _events("codex:hg", "needle-handoff-gone"), "codex", gone_src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="t")
    store.thread_attach(tid, "codex:hl")
    store.thread_attach(tid, "codex:hg")
    gone_src.unlink()
    store.prune_missing_sessions("codex", {str(live_src)})
    return tid


def test_handoff_thread_excludes_retained_members(store, tmp_path):
    """The engine compiled retained history into the bundle before this."""
    from voyager.continuity import handoff_thread

    tid = _thread_with_one_retained(store, tmp_path)
    out = tmp_path / "bundle.md"
    res = handoff_thread(store, thread=tid, target="claude", output=out)

    assert res["action"] == "bundle"
    assert res["member_count"] == 1, "only the live member is handed off"
    text = out.read_text(encoding="utf-8")
    assert "needle-handoff-live" in text
    assert "needle-handoff-gone" not in text, \
        "a retained session must not be compiled into a continuation bundle"


def test_handoff_never_native_resumes_a_retained_session(store, tmp_path):
    """Its source is gone — the provider has nothing to resume."""
    from voyager.continuity import handoff_thread

    src = _src(tmp_path, "r.jsonl")
    store.replace_session(
        _session("codex:rr", "needle-resume", can_resume=True),
        _events("codex:rr", "needle-resume"), "codex", src)
    src.unlink()
    store.prune_missing_sessions("codex", set())

    row = store.session("codex:rr")[0]
    res = handoff_thread(store, source=row, target="codex",
                         output=tmp_path / "p.md")
    assert res["action"] == "bundle", \
        "a retained session must fall through to a bundle, not native-resume"


def test_handoff_refuses_a_thread_with_only_retained_members(store, tmp_path):
    from voyager.continuity import handoff_thread

    src = _src(tmp_path, "only.jsonl")
    store.replace_session(_session("codex:only", "needle-only"),
                          _events("codex:only", "needle-only"), "codex", src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="t")
    store.thread_attach(tid, "codex:only")
    src.unlink()
    store.prune_missing_sessions("codex", set())

    res = handoff_thread(store, thread=tid, target="claude",
                         output=tmp_path / "x.md")
    assert res["action"] == "refused"
    assert "retained" in res["error"], "say WHY there is nothing to continue"


def test_discover_continuity_latest_holder_ignores_retained(store, tmp_path):
    from voyager.auto import discover_continuity

    tid = _thread_with_one_retained(store, tmp_path)
    disc = discover_continuity(store, thread_id=tid)
    assert disc["continuity_available"]
    assert disc["latest_session"]["id"] == "codex:hl"
    assert disc["retained_members"] == 1


def test_continue_thread_refuses_when_only_retained_remains(store, tmp_path,
                                                            capsys):
    from voyager.cli import main

    src = _src(tmp_path, "c.jsonl")
    store.replace_session(
        _session("codex:cr", "needle-continue", can_resume=True),
        _events("codex:cr", "needle-continue"), "codex", src)
    tid = store.thread_create(repo_root="E:/proj/demo", title="t")
    store.thread_attach(tid, "codex:cr")
    db = store.db_path
    src.unlink()
    store.prune_missing_sessions("codex", set())
    store.close()

    rc = main(["--db", str(db), "continue", "--thread", tid, "--no-launch"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no live member sessions" in err
    assert "retained" in err


def test_list_and_show_mark_retained(store, tmp_path, capsys):
    """Otherwise a retained session is indistinguishable from a live one."""
    from voyager.cli import main

    src = _src(tmp_path, "ls.jsonl")
    store.replace_session(_session("codex:ls", "needle-list"),
                          _events("codex:ls", "needle-list"), "codex", src)
    db = store.db_path
    src.unlink()
    store.prune_missing_sessions("codex", set())
    store.close()

    assert main(["--db", str(db), "list"]) == 0
    out = capsys.readouterr().out
    assert "[source missing]" in out
    assert "retained history" in out

    assert main(["--db", str(db), "show", "codex:ls"]) == 0
    out = capsys.readouterr().out
    assert "MISSING since" in out
    assert "not resumable" in out


# --- 6. migration is additive ---------------------------------------------

def test_migration_adds_columns_without_touching_rows(tmp_path):
    db = tmp_path / "old.db"
    s = Store(db)
    src = _src(tmp_path, "old.jsonl")
    s.replace_session(_session("codex:old", "needle-old"),
                      _events("codex:old", "needle-old"), "codex", src)
    before = dict(s.q("SELECT * FROM sessions WHERE id='codex:old'")[0])
    s.close()

    # simulate a pre-O2 database by dropping the new columns (the index has to
    # go first: SQLite refuses to drop a column an index still references)
    con = sqlite3.connect(db)
    con.execute("DROP INDEX IF EXISTS idx_sessions_source_state")
    con.execute("ALTER TABLE sessions DROP COLUMN source_state")
    con.execute("ALTER TABLE sessions DROP COLUMN source_missing_since")
    con.commit()
    cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
    assert "source_state" not in cols
    con.close()

    s = Store(db)                       # migrates additively
    after = dict(s.q("SELECT * FROM sessions WHERE id='codex:old'")[0])
    assert after["source_state"] is None, "existing rows keep NULL (== LIVE)"
    for k, v in before.items():
        if k not in ("source_state", "source_missing_since"):
            assert after[k] == v, k
    s.close()

    # repeated open must be idempotent
    s = Store(db)
    assert s.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 1
    s.close()


def test_migration_on_a_fresh_database_is_a_no_op(tmp_path):
    db = tmp_path / "fresh.db"
    Store(db).close()
    s = Store(db)
    cols = [r[1] for r in s.con.execute("PRAGMA table_info(sessions)")]
    assert "source_state" in cols and "source_missing_since" in cols
    src_cols = [r[1] for r in s.con.execute("PRAGMA table_info(sources)")]
    assert "last_seen" in src_cols and "missing_since" in src_cols
    s.close()
