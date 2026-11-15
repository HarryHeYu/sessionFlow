"""Q8-R2: a renamed provider file looks like "a session whose source vanished"
plus "a brand-new session".  Measured on v1.5.0: codex and claude derive the
native id from the path, so a rename changes the SID; the old session is
retained as SOURCE_MISSING and the new one is never attached (D15/O2).

This module pins the **read-only** diagnosis that names such a pair as a
*candidate*:

* the retained session is not deleted, re-keyed or re-bound;
* no thread membership changes, and nothing is auto-attached;
* Evidence References are not rewritten;
* the finding is explicitly a candidate, not a confirmed link.

Nothing here scans a real provider directory: every fixture is synthetic.
"""

from __future__ import annotations

import hashlib

from voyager import doctor
from voyager.model import new_event, new_session
from voyager.store import Store


def _seed(store: Store, tmp_path, sid: str, provider: str, contents, src_name: str):
    """Write one synthetic session and return the path of its source file."""
    src = tmp_path / src_name
    src.write_text("{}\n", encoding="utf-8")
    events = [
        new_event(sid=sid, seq=i, kind="assistant", ts=1000.0 + i, content=text)
        for i, text in enumerate(contents, start=1)
    ]
    store.replace_session(
        new_session(id=sid, provider=provider, native_session_id=sid.split(":")[-1],
                    title=sid, started_at=1000.0, updated_at=2000.0,
                    repo_root=str(tmp_path), cwd=str(tmp_path)),
        events, provider, src)
    # The store records the path as a string; hand back exactly what it stored,
    # so `prune_missing_sessions` compares like with like (a Path never matches).
    return store.q("SELECT path FROM sources WHERE sid=?", (sid,))[0]["path"]


def _digest_index(store: Store) -> str:
    """A content digest of the index, so "read-only" is not just "no crash"."""
    h = hashlib.sha256()
    for table in ("sessions", "events", "threads", "thread_sessions", "sources"):
        for row in store.q(f"SELECT * FROM {table} ORDER BY rowid"):
            h.update(repr(tuple(row)).encode("utf-8"))
    return h.hexdigest()


def _renamed_pair(tmp_path):
    """old session (source gone) + the same events under a new id (source live)."""
    store = Store(tmp_path / "i.db")
    src_a = _seed(store, tmp_path, "codex:aaaa", "codex",
                  ["fix the parser", "parser fixed"], "rollout-a.jsonl")
    src_b = _seed(store, tmp_path, "codex:bbbb", "codex",
                  ["fix the parser", "parser fixed"], "rollout-b.jsonl")
    # the rename: A's file is gone from disk, B's is there
    store.prune_missing_sessions("codex", {src_b})
    return store, src_a


def test_a_renamed_source_is_reported_as_a_candidate(tmp_path):
    store, _ = _renamed_pair(tmp_path)
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()

    assert rpt["available"] is True
    assert len(rpt["candidates"]) == 1, rpt
    c = rpt["candidates"][0]
    assert c["retained_session"] == "codex:aaaa"
    assert c["candidate_session"] == "codex:bbbb"
    assert c["shared_events"] == 2
    # it must say "candidate", not "linked"
    assert rpt["confirmed"] is False
    assert rpt["auto_linked"] is False
    assert "candidate" in c["note"].lower()


def test_unrelated_sessions_are_not_candidates(tmp_path):
    store = Store(tmp_path / "j.db")
    try:
        _seed(store, tmp_path, "codex:aaaa", "codex",
              ["fix the parser", "parser fixed"], "rollout-a.jsonl")
        src_c = _seed(store, tmp_path, "codex:cccc", "codex",
                      ["unrelated work", "something else entirely"],
                      "rollout-c.jsonl")
        store.prune_missing_sessions("codex", {src_c})
        rpt = doctor.check_source_migration(tmp_path / "j.db")
    finally:
        store.close()
    assert rpt["candidates"] == [], rpt


def test_a_different_provider_is_not_a_candidate(tmp_path):
    store = Store(tmp_path / "k.db")
    try:
        src_a = _seed(store, tmp_path, "codex:aaaa", "codex",
                      ["fix the parser", "parser fixed"], "rollout-a.jsonl")
        _seed(store, tmp_path, "claude:bbbb", "claude",
              ["fix the parser", "parser fixed"], "b.jsonl")
        store.prune_missing_sessions("codex", {src_a})
        rpt = doctor.check_source_migration(tmp_path / "k.db")
    finally:
        store.close()
    assert rpt["candidates"] == [], rpt


def test_the_check_changes_nothing(tmp_path):
    """Read-only in the strong sense: the index content digest is identical."""
    store, _ = _renamed_pair(tmp_path)
    try:
        before = _digest_index(store)
        members_before = [r["id"] for r in store.q(
            "SELECT id FROM sessions ORDER BY id")]
        states_before = {r["id"]: r["source_state"] for r in store.q(
            "SELECT id, source_state FROM sessions")}
        threads_before = store.q("SELECT COUNT(*) n FROM threads")[0]["n"]

        doctor.check_source_migration(tmp_path / "i.db")

        assert _digest_index(store) == before, "the diagnosis wrote to the index"
        assert [r["id"] for r in store.q("SELECT id FROM sessions ORDER BY id")] == members_before
        assert {r["id"]: r["source_state"] for r in store.q(
            "SELECT id, source_state FROM sessions")} == states_before
        assert store.q("SELECT COUNT(*) n FROM threads")[0]["n"] == threads_before
    finally:
        store.close()


def test_the_finding_is_a_read_only_diagnosis_not_a_repair(tmp_path):
    store, _ = _renamed_pair(tmp_path)
    try:
        issues = doctor.collect_issues(db_path=tmp_path / "i.db")
    finally:
        store.close()
    found = [i for i in issues if i.code == "SUSPECTED_SOURCE_MIGRATION"]
    assert len(found) == 1, [i.code for i in issues]
    issue = found[0]
    assert issue.auto_fixable is False
    assert issue.repair_kind == doctor.READ_ONLY_DIAGNOSIS
    assert "codex:aaaa" in issue.evidence and "codex:bbbb" in issue.evidence


def test_a_thread_keeps_its_member_and_gains_none(tmp_path):
    store, _ = _renamed_pair(tmp_path)
    try:
        tid = store.thread_create(repo_root=str(tmp_path), title="t", goal=None)
        store.thread_attach(tid, "codex:aaaa")
        doctor.check_source_migration(tmp_path / "i.db")
        assert [r["id"] for r in store.thread_members(tid)] == ["codex:aaaa"], \
            "the diagnosis re-bound or extended a WorkThread"
    finally:
        store.close()
