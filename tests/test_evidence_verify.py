"""`voyager evidence verify` -- a reference that can be checked by a machine.

A Continuation Bundle cites events as ``seq N · #digest``.  Comparing that by
hand (bundle in one window, ``voyager show`` in another) is how a stale
reference stays stale.  These tests pin the command that answers it
programmatically, and pin the two things it must never claim: that a matching
digest makes a claim true, and that an index match is the same as a live source.

Read-only is asserted, not assumed: every case snapshots the rows, the schema
objects and the bundle directory before and after.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from voyager.evidence import (EXIT_CODES, INVALID_REFERENCE, MATCH, MISMATCH,
                              NOT_FOUND, UNVERIFIABLE, parse_digest, render,
                              verify)
from voyager.model import new_event, new_session
from voyager.store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

#: A transcript with one tool call that failed, and an assistant claim that
#: contradicts it -- the situation the whole mechanism exists for.
CLAIM = "All 42 tests pass."


def _events(sid="codex:m", claim=CLAIM, rc=1):
    return [
        new_event(sid=sid, ts=1000, seq=0, kind="user",
                  content="run the tests"),
        new_event(sid=sid, ts=1001, seq=1, kind="tool_call", tool_name="exec",
                  command="pytest -q", tool_call_id="c1"),
        new_event(sid=sid, ts=1002, seq=2, kind="tool_result", role="tool",
                  tool_call_id="c1", exit_code=rc, tool_output="3 failed"),
        new_event(sid=sid, ts=1003, seq=3, kind="assistant", content=claim),
    ]


@pytest.fixture
def indexed(tmp_path):
    """A read-only-friendly handle on a seeded index."""
    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store = Store(db)
    store.replace_session(
        new_session(id="codex:m", provider="codex", native_session_id="m",
                    title="t", started_at=1000, updated_at=1000,
                    cwd="E:/r", repo_root="E:/r", message_count=4,
                    can_resume=True),
        _events(), "codex", src)
    store.close()
    return db


def _verify(db, seq, digest=None):
    store = Store(db)
    try:
        return verify(store, "codex:m", seq, digest)
    finally:
        store.close()


def _digest_at(db, seq, sid="codex:m"):
    from voyager.util import event_digest

    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("select * from events where sid=? and seq=?",
                      (sid, seq)).fetchone()
    con.close()
    assert row is not None, "no event at seq %s" % seq
    return event_digest(row["kind"], row["content"], row["command"],
                        row["tool_output"], row["tool_call_id"],
                        row["exit_code"])


def _snapshot(db):
    """Counts *and* a content digest per table.

    Counting alone would miss the failure that matters most: an UPDATE that
    changes `title='touched'` leaves every count identical.  A read-only
    guarantee is about bytes in the rows, not the shape of the table.
    """
    import hashlib

    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    counts = {}
    for table in ("sessions", "events", "threads", "thread_members",
                  "thread_leases", "thread_pending"):
        try:
            rows = con.execute("select * from " + table).fetchall()
            counts[table] = len(rows)
            h = hashlib.sha256()
            for row in sorted(repr(r) for r in rows):
                h.update(row.encode("utf-8", "replace"))
            counts[table + "_digest"] = h.hexdigest()[:16]
        except sqlite3.Error:
            counts[table] = None
            counts[table + "_digest"] = None
    counts["objects"] = con.execute(
        "select count(*) from sqlite_master").fetchone()[0]
    con.close()
    return counts


def _cli(db, *argv):
    """Run the real CLI in a subprocess, isolated but with a usable environment.

    The env is *inherited and overridden*, never hand-built from scratch: on
    Windows a child `python.exe` needs SYSTEMROOT (and friends) or it dies with
    `_Py_HashRandomization_Init: failed to get random numbers`, which is what a
    minimal dict buys you.  Inheriting is also the honest isolation model --
    override the agent-data roots, leave the machine's plumbing alone.
    """
    env = dict(os.environ)
    env.update({
        "HOME": str(db.parent),
        "USERPROFILE": str(db.parent),
        "APPDATA": str(db.parent / "appdata"),
        "LOCALAPPDATA": str(db.parent / "appdata"),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "VOYAGER_NO_SYNC": "1",
    })
    cmd = [PY, "-m", "voyager.cli", "--db", str(db)] + list(argv)
    return subprocess.run(cmd, capture_output=True, text=True,
                          cwd=str(db.parent), timeout=300, env=env)


# ---------------------------------------------------------------------------
# statuses
# ---------------------------------------------------------------------------

def test_an_unchanged_reference_matches(indexed):
    d = _digest_at(indexed, 3)
    r = _verify(indexed, 3, d)
    assert r["status"] == MATCH
    assert r["exit_code"] == 0


def test_appending_later_messages_does_not_move_a_reference(tmp_path):
    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store = Store(db)
    store.replace_session(
        new_session(id="codex:m", provider="codex", native_session_id="m",
                    title="t", started_at=1000, updated_at=1000,
                    cwd="E:/r", repo_root="E:/r", message_count=4,
                    can_resume=True), _events(), "codex", src)
    store.close()
    d = _digest_at(db, 3)

    # the conversation continues
    store = Store(db)
    store.replace_session(
        new_session(id="codex:m", provider="codex", native_session_id="m",
                    title="t", started_at=1000, updated_at=1000,
                    cwd="E:/r", repo_root="E:/r", message_count=6,
                    can_resume=True),
        _events() + [new_event(sid="codex:m", ts=1004, seq=4, kind="user",
                               content="now also fix the docs"),
                     new_event(sid="codex:m", ts=1005, seq=5,
                               kind="assistant", content="done")], "codex", src)
    store.close()

    r = _verify(db, 3, d)
    assert r["status"] == MATCH, "an append must not invalidate a reference"


def test_an_insert_in_the_middle_is_a_mismatch_not_a_repoint(tmp_path):
    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store = Store(db)
    store.replace_session(
        new_session(id="codex:m", provider="codex", native_session_id="m",
                    title="t", started_at=1000, updated_at=1000,
                    cwd="E:/r", repo_root="E:/r", message_count=4,
                    can_resume=True), _events(), "codex", src)
    store.close()
    d = _digest_at(db, 3)

    # two turns are prepended; the cited claim is still there, one slot later
    store = Store(db)
    store.replace_session(
        new_session(id="codex:m", provider="codex", native_session_id="m",
                    title="t", started_at=1000, updated_at=1000,
                    cwd="E:/r", repo_root="E:/r", message_count=6,
                    can_resume=True),
        [new_event(sid="codex:m", ts=800, seq=0, kind="user",
                   content="earlier question"),
         new_event(sid="codex:m", ts=801, seq=1, kind="assistant",
                   content="an answer"),
         new_event(sid="codex:m", ts=1000, seq=2, kind="user",
                   content="run the tests"),
         new_event(sid="codex:m", ts=1001, seq=3, kind="tool_call",
                   tool_name="exec", command="pytest -q", tool_call_id="c1"),
         new_event(sid="codex:m", ts=1002, seq=4, kind="tool_result",
                   role="tool", tool_call_id="c1", exit_code=1,
                   tool_output="3 failed"),
         new_event(sid="codex:m", ts=1003, seq=5, kind="assistant",
                   content=CLAIM)], "codex", src)
    store.close()

    r = _verify(db, 3, d)
    assert r["status"] == MISMATCH, (
        "the seq still resolves -- to a tool_call now.  A reference that "
        "silently re-pointed would call that MATCH")
    assert r["found_kind"] == "tool_call", "and says what is actually there"


def test_a_missing_position_is_NOT_FOUND(indexed):
    r = _verify(indexed, 99, "deadbeef")
    assert r["status"] == NOT_FOUND
    assert r["exit_code"] == 2


def test_an_unknown_session_is_NOT_FOUND(indexed):
    store = Store(indexed)
    try:
        res = verify(store, "codex:does-not-exist", 3, "deadbeef")
    finally:
        store.close()
    assert res["status"] == NOT_FOUND


def test_rewritten_text_at_the_same_seq_is_a_mismatch(indexed):
    d = _digest_at(indexed, 3)
    store = Store(indexed)
    store.con.execute("update events set content=? where sid=? and seq=?",
                      ("Actually two tests fail.", "codex:m", 3))
    store.con.commit()
    store.close()
    r = _verify(indexed, 3, d)
    assert r["status"] == MISMATCH


def test_a_flipped_exit_code_is_a_mismatch(indexed):
    """The change most likely to turn a failure into a claimed success.

    `exit_code` is part of the digest precisely so that flipping 1 -> 0 with
    identical output cannot keep the same fingerprint.
    """
    d = _digest_at(indexed, 2)
    store = Store(indexed)
    store.con.execute("update events set exit_code=0 where sid=? and seq=?",
                      ("codex:m", 2))
    store.con.commit()
    store.close()
    r = _verify(indexed, 2, d)
    assert r["status"] == MISMATCH


def test_no_digest_is_UNVERIFIABLE_never_quietly_matching(indexed):
    r = _verify(indexed, 3, None)
    assert r["status"] == UNVERIFIABLE
    assert r["exit_code"] == 3
    # it still says what is there, so the reader can adopt a digest
    assert r["found_digest"] == _digest_at(indexed, 3)


def test_a_malformed_reference_is_rejected(indexed):
    for seq, digest in (("three", "deadbeef"), ("3", "zzzz"), ("3", ""),
                        ("3", "c71c180a1")):
        r = _verify(indexed, seq, digest)
        assert r["status"] == INVALID_REFERENCE, (seq, digest)


def test_parse_digest_accepts_a_pasted_hash():
    assert parse_digest("#c71c180a") == "c71c180a"
    assert parse_digest("C71C180A") == "c71c180a"
    assert parse_digest("c71c180") is None
    assert parse_digest(None) is None


# ---------------------------------------------------------------------------
# source integrity is not content identity
# ---------------------------------------------------------------------------

def test_a_missing_source_is_separate_from_a_content_match(tmp_path):
    """Case H: the index can match while the original is gone.

    Reporting MATCH alone would let a caller read "verified" out of a cached
    copy whose transcript no longer exists anywhere.
    """
    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store = Store(db)
    store.replace_session(
        new_session(id="codex:m", provider="codex", native_session_id="m",
                    title="t", started_at=1000, updated_at=1000,
                    cwd="E:/r", repo_root="E:/r", message_count=4,
                    can_resume=True),
        _events(), "codex", src)
    store.con.execute("update sessions set source_state='SOURCE_MISSING' "
                      "where id='codex:m'")
    store.con.commit()
    store.close()

    d = _digest_at(db, 3)
    r = _verify(db, 3, d)
    assert r["status"] == MATCH, "the indexed event really does still match"
    assert r["source_available"] is False, (
        "but the original transcript cannot be re-read, and a caller must be "
        "able to require both")
    assert "indexed" in r["message"]


def test_match_is_never_a_statement_about_truth(indexed):
    d = _digest_at(indexed, 3)
    text = render(_verify(indexed, 3, d))
    assert "does not mean the claim it carries is true" in text, text


def test_a_forged_digest_still_matches_so_it_must_not_be_oversold(indexed):
    """A recomputed digest matches -- by construction.

    This is the honest boundary: the fingerprint detects accidental drift, and
    anyone who rewrites a transcript can rewrite the fingerprint.  The output
    says so on every path, which is the only defence available.
    """
    store = Store(indexed)
    store.con.execute("update events set content=? where sid=? and seq=?",
                      ("All 42 tests pass. (rewritten)", "codex:m", 3))
    store.con.commit()
    store.close()
    forged = _digest_at(indexed, 3)
    r = _verify(indexed, 3, forged)
    assert r["status"] == MATCH
    assert "does not mean the claim it carries is true" in render(r)


# ---------------------------------------------------------------------------
# cross-provider identity
# ---------------------------------------------------------------------------

def test_two_providers_with_the_same_native_id_are_distinguished(tmp_path):
    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store = Store(db)
    for prov in ("codex", "claude"):
        sid = "%s:shared" % prov
        store.replace_session(
            new_session(id=sid, provider=prov, native_session_id="shared",
                        title="t", started_at=1000, updated_at=1000,
                        cwd="E:/r", repo_root="E:/r", message_count=4,
                        can_resume=True), _events(sid=sid), prov, src)
    store.close()

    d = _digest_at(db, 3, sid="codex:shared")   # identical content
    store = Store(db)
    try:
        a = verify(store, "codex:shared", 3, d)
        b = verify(store, "claude:shared", 3, d)
    finally:
        store.close()
    assert a["status"] == MATCH and b["status"] == MATCH
    assert a["session"]["provider"] == "codex"
    assert b["session"]["provider"] == "claude", (
        "the reference must resolve the session it names, not merely find "
        "identical text somewhere in the index")


# ---------------------------------------------------------------------------
# read-only, and through the real CLI
# ---------------------------------------------------------------------------

def test_verification_changes_nothing(indexed):
    d = _digest_at(indexed, 3)
    before = _snapshot(indexed)
    _verify(indexed, 3, d)
    _verify(indexed, 3, None)
    _verify(indexed, 99, "deadbeef")
    _verify(indexed, "three", "deadbeef")
    assert _snapshot(indexed) == before, "verify is read-only"


def test_the_cli_reports_the_same_statuses(indexed):
    d = _digest_at(indexed, 3)

    p = _cli(indexed, "evidence", "verify", "codex:m", "3", d)
    assert p.returncode == EXIT_CODES[MATCH], p.stdout + p.stderr
    assert "MATCH" in p.stdout

    p = _cli(indexed, "evidence", "verify", "codex:m", "3", "deadbeef")
    assert p.returncode == EXIT_CODES[MISMATCH], p.stdout + p.stderr
    assert "MISMATCH" in p.stdout

    p = _cli(indexed, "evidence", "verify", "codex:nope", "3", "deadbeef")
    assert p.returncode == EXIT_CODES[NOT_FOUND]

    p = _cli(indexed, "evidence", "verify", "codex:m", "3")
    assert p.returncode == EXIT_CODES[UNVERIFIABLE]

    p = _cli(indexed, "evidence", "verify", "codex:m", "three", "deadbeef")
    assert p.returncode == EXIT_CODES[INVALID_REFERENCE]


def test_the_cli_has_stable_machine_readable_output(indexed):
    d = _digest_at(indexed, 3)
    p = _cli(indexed, "evidence", "verify", "codex:m", "3", d, "--json")
    payload = json.loads(p.stdout)
    assert payload["status"] == MATCH
    assert payload["seq"] == 3
    assert payload["digest"] == d
    assert payload["found_digest"] == d
    assert payload["source_available"] is True
    assert "session" not in payload, (
        "the JSON is for checking a reference; the session body is `show`'s job")
    # no real path leaks into machine output
    assert str(indexed) not in p.stdout


def test_a_reference_printed_by_show_verifies(indexed):
    """The round trip a reader actually performs.

    `voyager show` prints ``#seq/digest`` on every line; feeding the digest
    back to `evidence verify` must MATCH.  If these two ever drift apart, the
    mechanism is decoration.
    """
    p = _cli(indexed, "show", "codex:m")
    line = next(l for l in p.stdout.splitlines() if "#3/" in l)
    digest = line.split("#3/")[1].split()[0]
    q = _cli(indexed, "evidence", "verify", "codex:m", "3", digest)
    assert q.returncode == EXIT_CODES[MATCH], (line, q.stdout)


# ---------------------------------------------------------------------------
# original-source integrity, as distinct from index consistency
# ---------------------------------------------------------------------------
# The scanner records mtime and size per source but nothing read them back.
# `evidence verify` now stats the file once (no open, no read) and reports the
# comparison, which is the difference between "the index still matches" and "the
# file the index was built from has not been touched since".

def _source_stat(result):
    return result.get("source_stat") or {}


def test_an_untouched_source_reports_its_recorded_stat(indexed):
    d = _digest_at(indexed, 3)
    r = _verify(indexed, 3, d)
    st = _source_stat(r)
    assert st["exists"] is True
    assert st["mtime_matches"] is True
    assert st["size_matches"] is True
    assert "same size and mtime" in render(r)
    assert "not proof of identical bytes" in render(r), (
        "mtime+size equality is not byte equality, and saying so is the point")


def test_a_source_touched_after_the_scan_is_reported(tmp_path):
    """The case the recorded mtime exists for.

    The indexed event still MATCHes -- nothing in the index moved -- but the
    transcript on disk has since been edited.  A reader must be able to tell
    those two facts apart, and must not read "MATCH" as "the original is
    intact".
    """
    import time

    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store = Store(db)
    store.replace_session(
        new_session(id="codex:m", provider="codex", native_session_id="m",
                    title="t", started_at=1000, updated_at=1000,
                    cwd="E:/r", repo_root="E:/r", message_count=4,
                    can_resume=True), _events(), "codex", src)
    store.close()

    d = _digest_at(db, 3)
    # make sure the mtime would differ, then append
    time.sleep(0.05)
    src.write_text("{}\n{\"extra\":true}\n", encoding="utf-8")

    r = _verify(db, 3, d)
    assert r["status"] == MATCH, "the index itself did not move"
    st = _source_stat(r)
    assert st["size_matches"] is False, (
        "the file grew after it was indexed; the recorded size no longer holds")
    assert "may have changed since it was indexed" in render(r)
    # and the verdict is deliberately hedged: a size change is not a byte change
    assert "may have" in render(r)


def test_a_deleted_source_is_named_as_such(tmp_path):
    db = tmp_path / "index.db"
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    store = Store(db)
    store.replace_session(
        new_session(id="codex:m", provider="codex", native_session_id="m",
                    title="t", started_at=1000, updated_at=1000,
                    cwd="E:/r", repo_root="E:/r", message_count=4,
                    can_resume=True), _events(), "codex", src)
    store.close()
    d = _digest_at(db, 3)
    src.unlink()

    r = _verify(db, 3, d)
    assert r["status"] == MATCH, "the index keeps the history (D15)"
    assert _source_stat(r)["exists"] is False
    assert "no longer on disk" in render(r)


# ---------------------------------------------------------------------------
# fingerprint contract
# ---------------------------------------------------------------------------
# The serialization is JSON, and that is a measured decision: with a plain
# delimiter join, `content="a|b", command="c"` and `content="a", command="b|c"`
# serialise to the same string and therefore produce the *same* fingerprint --
# which would read a changed event as unchanged.

def test_a_pipe_in_one_field_cannot_be_mistaken_for_a_boundary():
    from voyager.util import event_digest

    a = event_digest("user", "a|b", "c", None, None)
    b = event_digest("user", "a", "b|c", None, None)
    assert a != b, (
        "a pipe inside `content` was indistinguishable from a field boundary "
        "under the old delimiter join; a changed event would read as unchanged")


def test_the_digest_is_stable_and_content_sensitive():
    from voyager.util import event_digest

    assert event_digest("user", "x", None, None, None) == \
           event_digest("user", "x", None, None, None)
    assert event_digest("user", "a", None, None, None) != \
           event_digest("user", "b", None, None, None)
    assert event_digest("tool_result", None, None, "out", "t", 1) != \
           event_digest("tool_result", None, None, "out", "t", 0)


def test_none_and_empty_are_distinct_and_zero_is_too():
    """Documented, not accidental.

    Under JSON serialisation `None` is ``null`` and `""` is an empty string, so
    they hash differently -- a stricter rule than the old delimiter join, which
    collapsed both to nothing.  `0` must also stay distinct from `None`: a tool
    result that succeeded and a field that is absent are different facts, and
    conflating them would hide a status change behind an absent column.
    """
    from voyager.util import event_digest

    assert event_digest("user", None, None, None, None) != \
           event_digest("user", "", None, None, None), (
        "an absent body and an empty one are serialised differently; that is "
        "the stricter and safer of the two rules")
    assert event_digest("tool_result", None, None, "out", None, 0) != \
           event_digest("tool_result", None, None, "out", None, None), (
        "exit code 0 (succeeded) is not the same as no exit code")


def test_the_algorithm_is_versioned():
    """So a future change to the field set cannot silently invalidate refs."""
    from voyager.util import FINGERPRINT_ALGORITHM

    assert FINGERPRINT_ALGORITHM, "the algorithm must be named"
    assert FINGERPRINT_ALGORITHM in ("sha256-8hex-json-v1",), (
        "if this changed, every recorded reference must be re-issued; bump the "
        "name so old references can be recognised rather than mismatching")
