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
import os
import sqlite3

import pytest

from voyager import doctor
from voyager.model import new_event, new_session
from voyager.store import Store

HEAD = "continue the work"


# --- fixtures ---------------------------------------------------------------

def _seed(store: Store, tmp_path, sid: str, provider: str, contents, src_name: str,
          repo=None):
    """Write one synthetic session and return the source path the store recorded."""
    src = tmp_path / src_name
    src.write_text("{}\n", encoding="utf-8")
    events = [
        new_event(sid=sid, seq=i, kind="assistant", ts=1000.0 + i, content=text)
        for i, text in enumerate(contents, start=1)
    ]
    store.replace_session(
        new_session(id=sid, provider=provider, native_session_id=sid.split(":")[-1],
                    title=sid, started_at=1000.0, updated_at=2000.0,
                    repo_root=str(repo or tmp_path), cwd=str(repo or tmp_path)),
        events, provider, src)
    # The store records the path as a string; hand back exactly what it stored,
    # so `prune_missing_sessions` compares like with like (a Path never matches).
    return store.q("SELECT path FROM sources WHERE sid=?", (sid,))[0]["path"]


def _pair(tmp_path, retained_texts, live_texts, db="i.db",
          retained_sid="codex:aaaa", live_sid="codex:bbbb",
          retained_provider="codex", live_provider="codex",
          retained_repo=None, live_repo=None):
    """A retained session (its source is gone) next to a live one."""
    store = Store(tmp_path / db)
    _seed(store, tmp_path, retained_sid, retained_provider,
          retained_texts, retained_sid.split(":")[-1] + ".jsonl",
          repo=retained_repo)
    src_b = _seed(store, tmp_path, live_sid, live_provider,
                  live_texts, live_sid.split(":")[-1] + ".jsonl",
                  repo=live_repo)
    store.prune_missing_sessions(retained_provider, {src_b})
    return store


def _digest_index(store: Store) -> str:
    """A content digest of the index, so "read-only" is not just "no crash"."""
    h = hashlib.sha256()
    for table in ("sessions", "events", "threads", "thread_sessions", "sources"):
        for row in store.q(f"SELECT * FROM {table} ORDER BY rowid"):
            h.update(repr(tuple(row)).encode("utf-8"))
    return h.hexdigest()


def _only(report):
    assert report["available"] is True, report
    assert len(report["candidates"]) == 1, report
    return report["candidates"][0]


# --- 1. prefix semantics ----------------------------------------------------

def test_retained_is_a_prefix_of_live(tmp_path):
    """Renamed, then the agent kept writing: the old file is the shorter one."""
    store = _pair(tmp_path, ["a", "b"], ["a", "b", "c"])
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()
    c = _only(rpt)
    assert c["direction"] == "retained_is_prefix"
    assert c["confidence"] == "strong"
    assert (c["shared_events"], c["retained_events"], c["candidate_events"]) == (2, 2, 3)


def test_live_is_a_prefix_of_retained(tmp_path):
    """The new file is shorter but starts identically — a truncated copy/fork.

    Kept as a candidate (still a plausible migration) but labelled weaker,
    because the live side holds *less* than the retained side.
    """
    store = _pair(tmp_path, ["a", "b", "c"], ["a", "b"])
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()
    c = _only(rpt)
    assert c["direction"] == "live_is_prefix"
    assert c["confidence"] == "weak"
    assert (c["shared_events"], c["retained_events"], c["candidate_events"]) == (2, 3, 2)


def test_identical_sequences(tmp_path):
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()
    c = _only(rpt)
    assert c["direction"] == "identical"
    assert c["confidence"] == "strong"
    assert c["shared_events"] == 2


def test_identical_content_in_another_repo_is_weaker(tmp_path):
    """Same bytes, different repository: still a candidate, but not a strong one.

    A copy into another project is a plausible migration and also exactly what a
    deliberate fork looks like, so the label has to say so.
    """
    store = _pair(tmp_path, ["a", "b"], ["a", "b"],
                  retained_repo=tmp_path / "repo-a",
                  live_repo=tmp_path / "repo-b")
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()
    c = _only(rpt)
    assert c["direction"] == "identical"
    assert c["same_repo"] is False
    assert c["confidence"] == "weak"


def test_only_the_first_event_agreeing_is_not_a_candidate(tmp_path):
    """The classic false positive: two sessions both start with "continue…"."""
    store = _pair(tmp_path, [HEAD, "x"], [HEAD, "y"])
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()
    assert rpt["candidates"] == [], rpt


def test_diverging_at_the_third_event_is_not_a_candidate(tmp_path):
    store = _pair(tmp_path, ["a", "b", "x", "d"], ["a", "b", "y", "d"])
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()
    assert rpt["candidates"] == [], rpt


def test_a_single_event_session_is_never_a_candidate(tmp_path):
    """One event cannot distinguish a rename from a coincidence."""
    store = _pair(tmp_path, ["a"], ["a"])
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()
    assert rpt["candidates"] == [], rpt


def test_a_different_provider_is_not_a_candidate(tmp_path):
    store = _pair(tmp_path, ["a", "b"], ["a", "b"], live_provider="claude",
                  live_sid="claude:bbbb")
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()
    assert rpt["candidates"] == [], rpt


def test_many_candidates_pointing_at_one_retained_session(tmp_path):
    """Three live copies of one retained session: three candidates, stable order."""
    store = Store(tmp_path / "i.db")
    try:
        _seed(store, tmp_path, "codex:aaaa", "codex", ["a", "b"], "aaaa.jsonl")
        live_paths = set()
        for sid in ("codex:bbbb", "codex:cccc", "codex:dddd"):
            live_paths.add(_seed(store, tmp_path, sid, "codex", ["a", "b"],
                                 sid.split(":")[-1] + ".jsonl"))
        store.prune_missing_sessions("codex", live_paths)
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        store.close()
    assert [c["candidate_session"] for c in rpt["candidates"]] == [
        "codex:bbbb", "codex:cccc", "codex:dddd"]
    assert all(c["retained_session"] == "codex:aaaa" for c in rpt["candidates"])


# --- 2. read-only, structurally ---------------------------------------------

def test_the_check_never_creates_a_directory(tmp_path, monkeypatch):
    """`Store()` mkdirs its parent; this check must not open one."""
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    store.close()

    import pathlib

    def boom(self, *a, **kw):
        raise AssertionError("the diagnosis created a directory: %s" % self)

    monkeypatch.setattr(pathlib.Path, "mkdir", boom)
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        monkeypatch.undo()
    assert len(rpt["candidates"]) == 1


def test_the_check_never_opens_a_writable_store(tmp_path, monkeypatch):
    """No Store means no schema script, no journal switch, no migration."""
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    store.close()

    import voyager.store as store_mod

    def boom(*a, **kw):
        raise AssertionError("the diagnosis opened a writable Store")

    monkeypatch.setattr(store_mod.Store, "__init__", boom)
    rpt = doctor.check_source_migration(tmp_path / "i.db")
    monkeypatch.undo()
    assert len(rpt["candidates"]) == 1


def test_the_index_bytes_are_unchanged(tmp_path):
    """Strong form: the main database file is byte-identical afterwards."""
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    store.close()
    db = tmp_path / "i.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    doctor.check_source_migration(db)

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_the_only_files_it_can_create_are_sqlite_sidecars(tmp_path):
    """A WAL index needs -wal/-shm to be read; nothing else may appear.

    This is the explicitly marked exception: SQLite creates those two when it
    opens a WAL database, and there is no way to read one without them.  Any
    other new file would be a real defect.
    """
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    store.close()
    before = {p.name for p in tmp_path.iterdir()}

    doctor.check_source_migration(tmp_path / "i.db")

    new = {p.name for p in tmp_path.iterdir()} - before
    assert new <= {"i.db-wal", "i.db-shm"}, new


def test_a_missing_index_reports_unavailable_and_creates_nothing(tmp_path):
    before = {p.name for p in tmp_path.iterdir()}
    rpt = doctor.check_source_migration(tmp_path / "nope.db")
    assert rpt["available"] is False
    assert rpt["candidates"] == []
    assert {p.name for p in tmp_path.iterdir()} == before


def test_an_index_without_the_column_is_reported_not_raised(tmp_path):
    """An older index has no `source_state`; the check degrades, it does not throw."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, provider TEXT)")
    con.execute("INSERT INTO sessions VALUES ('codex:aaaa','codex')")
    con.commit()
    con.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    rpt = doctor.check_source_migration(db)

    assert rpt["available"] is False
    assert rpt["candidates"] == []
    assert "error" in rpt and rpt["error"]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_a_corrupt_file_is_reported_not_raised(tmp_path):
    db = tmp_path / "junk.db"
    db.write_bytes(b"this is not a database" * 100)
    rpt = doctor.check_source_migration(db)
    assert rpt["available"] is False
    assert "error" in rpt


@pytest.mark.skipif(os.name == "nt",
                    reason="chmod does not deny writes on Windows; covered on POSIX CI")
def test_a_read_only_directory_does_not_raise(tmp_path):
    """POSIX: the index directory cannot be written, so no sidecar can be made.

    The answer may be 'unavailable' — what must not happen is a traceback out of
    a diagnostic, or a silent empty result that reads as "no migration".
    """
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    store.close()
    tmp_path.chmod(0o555)
    try:
        rpt = doctor.check_source_migration(tmp_path / "i.db")
    finally:
        tmp_path.chmod(0o755)
    assert "candidates" in rpt
    assert rpt["available"] is False or isinstance(rpt["candidates"], list)


# --- 3. the finding is a diagnosis, not a repair ----------------------------

def test_the_check_changes_nothing(tmp_path):
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
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
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    try:
        issues = doctor.collect_issues(db_path=tmp_path / "i.db")
    finally:
        store.close()
    found = [i for i in issues if i.code == "SUSPECTED_SOURCE_MIGRATION"]
    assert len(found) == 1, [i.code for i in issues]
    issue = found[0]
    assert issue.auto_fixable is False
    assert issue.repair_kind == doctor.READ_ONLY_DIAGNOSIS
    assert issue.severity == doctor.INFO, "a candidate must not be promoted to warning"
    assert "codex:aaaa" in issue.evidence and "codex:bbbb" in issue.evidence


def test_one_issue_per_retained_session_not_per_pair(tmp_path):
    """500 candidates must not become 500 findings — they are one finding's evidence."""
    store = Store(tmp_path / "i.db")
    try:
        _seed(store, tmp_path, "codex:aaaa", "codex", ["a", "b"], "aaaa.jsonl")
        live = set()
        for sid in ("codex:bbbb", "codex:cccc", "codex:dddd"):
            live.add(_seed(store, tmp_path, sid, "codex", ["a", "b"],
                           sid.split(":")[-1] + ".jsonl"))
        store.prune_missing_sessions("codex", live)
        issues = [i for i in doctor.collect_issues(db_path=tmp_path / "i.db")
                  if i.code == "SUSPECTED_SOURCE_MIGRATION"]
    finally:
        store.close()
    assert len(issues) == 1, [i.message for i in issues]
    assert "3 live session(s)" in issues[0].message
    assert "+0 more" not in issues[0].evidence
    assert "codex:dddd" in issues[0].evidence


def test_the_diagnosis_is_computed_once_per_doctor_run(tmp_path, monkeypatch):
    """`run()` and `collect_issues()` must not each pay for the same scan."""
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    store.close()

    calls = []
    real = doctor.check_source_migration

    def counting(db_path=None):
        calls.append(db_path)
        return real(db_path)

    monkeypatch.setattr(doctor, "check_source_migration", counting)
    report = doctor.run(db_path=tmp_path / "i.db")
    monkeypatch.undo()

    assert len(calls) == 1, "the diagnosis ran %d times in one doctor run" % len(calls)
    assert report["source_migration"]["candidates"]
    assert any(i["code"] == "SUSPECTED_SOURCE_MIGRATION" for i in report["issues"])


def test_an_info_finding_never_lands_in_the_warning_bucket(tmp_path):
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    store.close()
    report = doctor.run(db_path=tmp_path / "i.db")
    assert not [i for i in report["warnings"]
                if i["code"] == "SUSPECTED_SOURCE_MIGRATION"]
    assert not [i for i in report["blocking"]
                if i["code"] == "SUSPECTED_SOURCE_MIGRATION"]
    hits = [i for i in report["non_blocking"]
            if i["code"] == "SUSPECTED_SOURCE_MIGRATION"]
    assert len(hits) == 1 and hits[0]["auto_fixable"] is False


def test_a_thread_keeps_its_member_and_gains_none(tmp_path):
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    try:
        tid = store.thread_create(repo_root=str(tmp_path), title="t", goal=None)
        store.thread_attach(tid, "codex:aaaa")
        doctor.check_source_migration(tmp_path / "i.db")
        assert [r["id"] for r in store.thread_members(tid)] == ["codex:aaaa"], \
            "the diagnosis re-bound or extended a WorkThread"
    finally:
        store.close()


def test_the_report_is_deterministic_across_runs(tmp_path):
    store = _pair(tmp_path, ["a", "b"], ["a", "b"])
    store.close()
    first = doctor.check_source_migration(tmp_path / "i.db")
    second = doctor.check_source_migration(tmp_path / "i.db")
    assert first == second
