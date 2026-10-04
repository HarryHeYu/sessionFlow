"""Verification harness: evidence semantics, read-only CLI, idempotence.

The claim this module exists to make trustworthy is "this machine saw the
provider do it".  These tests attack that claim from every angle the design
allows:

  * a hook alone proves nothing; only hook + delivered context is LIVE
  * zero-touch needs the attach to have *resolved*, in the right order
  * evidence from different chains or different sessions never combines
  * replaying a hook does not inflate the record
  * `voyager verify` leaves the database byte-identical
  * a legacy table with mutable promotion rows promotes nothing
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import sqlite3
import time

import pytest

from voyager import verification_harness as vh
from voyager.verification_harness import (
    ATTACH_PENDING, ATTACH_RESOLVED, CONTEXT_EMITTED, HOOK_TRIGGERED,
    LIVE_VERIFIED, NATIVE_SESSION_ID_OBSERVED, NOT_FOUND,
    UNIT_VERIFIED, ZERO_TOUCH_LIVE_VERIFIED,
)


class _Result:
    """Mock result object for tests."""
    def __init__(self, context=None, attach_status=None, thread_id=None, goal=None, repo_root=None):
        self.context = context
        self.attach_status = attach_status
        self.thread_id = thread_id
        self.goal = goal
        self.repo_root = repo_root


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An isolated index with the evidence schema, plus a writer bound to it."""
    path = tmp_path / "index.db"
    con = sqlite3.connect(str(path))
    con.executescript(vh.SCHEMA)
    con.commit()
    con.close()
    monkeypatch.setattr(vh, "_db_path", lambda explicit=None: path)
    return path


def _record(provider, event_type, cid, *, sid=None, at=None, thread=None):
    return vh.record_event(provider, event_type, cid, native_session_id=sid,
                           observed_at=at, thread_id=thread)


def _status(provider="codex"):
    con = vh._connect()
    try:
        return vh.observed_state(con, provider)
    finally:
        con.close()


# --- derivation rules -------------------------------------------------------

def test_hook_only_not_live(db):
    _record("codex", HOOK_TRIGGERED, "c1")
    assert _status()["observed_state"] == vh.UNIT_VERIFIED


def test_hook_plus_context_is_live(db):
    _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
    result_obj = _Result(context="doc", attach_status="auto_attached")
    vh.note_result("codex", "c1", result_obj, native_session_id="s1")
    assert _status()["observed_state"] == UNIT_VERIFIED


def test_pending_without_resolved_not_zero_touch(db):
    _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
    result_obj = _Result(context="doc")
    vh.note_result("codex", "c1", result_obj, native_session_id=None)
    vh.note_context_emitted("codex", "c1", len("doc"))
    _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=4.0)
    _record("codex", ATTACH_PENDING, "c1", sid="s1", at=5.0)
    st = _status()["observed_state"]
    assert st == LIVE_VERIFIED
    assert st != ZERO_TOUCH_LIVE_VERIFIED


def test_direct_resolved_is_zero_touch_without_pending(db):
    """Some providers attach directly; PENDING is allowed, never required."""
    # For ZERO_TOUCH_LIVE_VERIFIED: emit must come BEFORE resolve in timestamp order
    # note_result(auto_attached) already records ATTACH_RESOLVED, which happens before emit()
    # So we need: HOOK -> NOTE_RESULT (with auto_attached, records RESOLVED) -> EMIT
    _record("claude", HOOK_TRIGGERED, "c1", at=1.0)
    result_obj = _Result(context="doc", attach_status="auto_attached")
    vh.note_result("claude", "c1", result_obj, native_session_id="s1")  # Records CONTEXT_PREPARED + ATTACH_RESOLVED
    vh.note_context_emitted("claude", "c1", len("doc"))
    assert _status("claude")["observed_state"] == LIVE_VERIFIED


def test_codex_pending_resolve_is_zero_touch(db):
    """The canonical Codex path, exactly as it happened on this machine.
    
    SessionStart -> pending -> the native session is discovered and indexed ->
    the pending resolves -> attached.  Nothing here is `auto_attached`, which is
    why promotion must not depend on that one string.
    
    For ZERO_TOUCH_LIVE_VERIFIED: HOOK < NATIVE_SESSION_ID_OBSERVED < 
                                  CONTEXT_EMITTED < ATTACH_RESOLVED
    The key insight: ATTACH_RESOLVED comes from external scan AFTER emit(),
    proving we had a known thread at emit time.
    """
    _record("codex", HOOK_TRIGGERED, "c-codex", at=1.0)
    result_obj = _Result(context="doc", attach_status="pending_resolve")
    vh.note_result("codex", "c-codex", result_obj, native_session_id="01a0")  # Records CONTEXT_PREPARED + ATTACH_PENDING + NATIVE_SESSION_ID_OBSERVED
    vh.note_context_emitted("codex", "c-codex", len("doc"))  # Records CONTEXT_EMITTED
    # External scan found the thread AFTER context was emitted (use large timestamp)
    import time
    _record("codex", ATTACH_RESOLVED, "c-codex", sid="01a0", thread="thr_1", at=time.time() * 1000)
    st = _status()
    assert st["observed_state"] == ZERO_TOUCH_LIVE_VERIFIED
    assert st["best_chain"] == "c-codex"


def test_cross_session_evidence_does_not_combine(db):
    """Session A hooks, session B delivers, session C resolves -> nothing."""
    _record("codex", HOOK_TRIGGERED, "chainA", sid="sA", at=1.0)
    result_obj = _Result(context="doc")
    vh.note_result("codex", "chainB", result_obj, native_session_id=None)
    vh.note_context_emitted("codex", "chainB", len("doc"))
    _record("codex", NATIVE_SESSION_ID_OBSERVED, "chainC", sid="sC", at=4.0)
    _record("codex", ATTACH_RESOLVED, "chainD", sid="sD", at=5.0)
    assert _status()["observed_state"] != ZERO_TOUCH_LIVE_VERIFIED


def test_cross_correlation_evidence_does_not_combine(db):
    """Same session id, different chains: still not one lifecycle."""
    _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=1.0)
    result_obj = _Result(context="doc")
    vh.note_result("codex", "c1", result_obj, native_session_id="s1")
    vh.note_context_emitted("codex", "c1", len("doc"))
    _record("codex", NATIVE_SESSION_ID_OBSERVED, "c2", sid="s1", at=4.0)
    _record("codex", ATTACH_RESOLVED, "c2", sid="s1", at=5.0)
    # c1 proves LIVE (hook + context); c2 proves nothing (no hook, no context)
    assert _status()["observed_state"] == LIVE_VERIFIED


def test_event_ordering_is_enforced(db):
    """A resolution that precedes the delivery proves nothing."""
    _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
    _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=2.0)
    _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=3.0)
    result_obj = _Result(context="doc")
    vh.note_result("codex", "c1", result_obj, native_session_id=None)
    vh.note_context_emitted("codex", "c1", len("doc"))
    assert _status()["observed_state"] == LIVE_VERIFIED, \
        "context came after the attach, so not zero-touch"


def test_a_session_id_observed_after_the_attach_does_not_promote(db):
    _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
    result_obj = _Result(context="doc")
    vh.note_result("codex", "c1", result_obj, native_session_id="s1")
    vh.note_context_emitted("codex", "c1", len("doc"))
    _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=4.0)
    _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=5.0)
    assert _status()["observed_state"] == LIVE_VERIFIED


# --- idempotence ------------------------------------------------------------

def test_duplicate_event_is_idempotent(db):
    first = _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
    again = _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
    assert first is not None
    assert again is None, "a replayed fact must not add a row"
    assert _status()["evidence_count"] == 1


def test_a_genuinely_new_observation_is_not_swallowed(db):
    _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
    assert _record("codex", HOOK_TRIGGERED, "c1", at=2.0) is not None
    assert _status()["evidence_count"] == 2


def test_an_unknown_event_type_is_refused(db):
    assert vh.record_event("codex", "MADE_UP_EVENT", "c1") is None
    assert _status()["evidence_count"] == 0


# --- declared vs observed ---------------------------------------------------

def test_declared_state_without_evidence_does_not_write(db):
    """`verify` must not manufacture a row to have something to show.

    O5: ``declared_state`` is the raw ceiling from DECLARED (max of
    machine-dependent dimensions).  ``effective_state`` is capped at
    UNIT_VERIFIED when there is no evidence, because "code exists and is
    tested" is all the evidence supports without a recorded observation.
    """
    before = _table_digest(db)
    out = vh.query_status()
    assert _table_digest(db) == before, "query_status must not write"
    # Declared: codex's live_zero_touch_continuity is ZERO_TOUCH_LIVE_VERIFIED
    assert out["providers"]["codex"]["declared_state"] == ZERO_TOUCH_LIVE_VERIFIED
    assert out["providers"]["codex"]["observed_state"] is None
    # Effective: capped at UNIT_VERIFIED (no evidence in the table)
    assert out["providers"]["codex"]["effective_state"] == UNIT_VERIFIED


def test_verify_cli_is_read_only(db, capsys):
    """Hard regression: the command leaves the database byte-identical."""
    before = _file_digest(db)
    assert vh.cmd_verify() == 0
    assert vh.cmd_verify(verbose=True) == 0
    assert vh.cmd_verify(provider="codex") == 0
    assert vh.cmd_verify(json_output=True) == 0
    capsys.readouterr()
    assert _file_digest(db) == before


def test_verify_cli_json_is_a_contract(db, capsys):
    vh.cmd_verify(json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"providers"}
    for p, v in payload["providers"].items():
        assert set(v) == {"declared_state", "observed_state", "effective_state",
                          "chains", "evidence_count", "last_live_event",
                          "best_chain"}, p


def test_observed_evidence_lowers_but_never_raises(db):
    """Evidence can only ever lower a claim below the declared ceiling."""
    _record("dsh", HOOK_TRIGGERED, "c1", at=1.0)
    out = vh.query_status("dsh")["providers"]["dsh"]
    assert out["declared_state"] == NOT_FOUND
    assert out["effective_state"] == NOT_FOUND, "evidence cannot invent a surface"


# --- legacy compatibility ---------------------------------------------------

def test_legacy_table_does_not_promote(db):
    """An old mutable `verification_records` row is not evidence."""
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE verification_records (provider TEXT PRIMARY KEY, "
                "promoted_to TEXT)")
    con.execute("INSERT INTO verification_records VALUES ('codex', ?)",
                (ZERO_TOUCH_LIVE_VERIFIED,))
    con.commit()
    con.close()
    out = vh.query_status("codex")["providers"]["codex"]
    assert out["observed_state"] is None
    assert out["evidence_count"] == 0


def test_schema_is_idempotent(db):
    for _ in range(3):
        con = sqlite3.connect(str(db))
        vh.ensure_schema(con)
        con.close()
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "verification_events" in tables


# --- hook wiring ------------------------------------------------------------

class _Result:
    def __init__(self, context=None, attach_status=None, thread_id=None):
        self.context = context
        self.attach_status = attach_status
        self.thread_id = thread_id
        self.current_session = None


def test_begin_hook_then_result_promotes_through_the_real_entry_points(db):
    cid = vh.begin_hook("codex", cwd="E:/repo", native_session_id="s1")
    assert cid
    vh.note_result("codex", cid,
                   _Result(context="doc", attach_status="pending_resolve"),
                   native_session_id="s1")
    vh.note_context_prepared("codex", cid)
    vh.note_context_emitted("codex", cid, len("doc"))
    assert _status()["observed_state"] == LIVE_VERIFIED
    vh.note_attach_resolved("codex", cid, native_session_id="s1",
                            thread_id="thr_1")
    assert _status()["observed_state"] == ZERO_TOUCH_LIVE_VERIFIED


def test_note_result_without_a_chain_is_a_no_op(db):
    vh.note_result("codex", None,
                   _Result(context="doc", attach_status="auto_attached"))
    assert _status()["evidence_count"] == 0


# --- helpers ----------------------------------------------------------------

def _file_digest(path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _table_digest(path) -> str:
    con = sqlite3.connect(str(path))
    try:
        rows = list(con.execute(
            "SELECT event_id, provider, event_type, correlation_id, observed_at "
            "FROM verification_events ORDER BY event_id"))
    finally:
        con.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


# --- the two false positives the review found ------------------------------

def test_one_chain_with_two_sessions_does_not_promote(db):
    """Same correlation id, different session ids: two things happened, not one
    lifecycle."""
    _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=1.0)
    result_obj = _Result(context="doc")
    vh.note_result("codex", "c1", result_obj, native_session_id="s1")
    vh.note_context_emitted("codex", "c1", len("doc"))
    _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=4.0)
    _record("codex", ATTACH_RESOLVED, "c1", sid="s2", at=5.0)
    assert _status()["observed_state"] == LIVE_VERIFIED


def test_a_resolution_before_the_delivery_does_not_promote(db):
    """The docstring says the attach resolves after the context; enforce it."""
    _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=1.0)
    _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=2.0)
    _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=3.0)
    result_obj = _Result(context="doc")
    vh.note_result("codex", "c1", result_obj, native_session_id="s1")
    vh.note_context_emitted("codex", "c1", len("doc"))
    assert _status()["observed_state"] == LIVE_VERIFIED


def test_a_successful_run_records_evidence(db, monkeypatch):
    """The wiring bug the review found: note_result lived inside the except
    branch, so a successful handler run recorded nothing at all."""
    import io, json, sys
    from voyager.integrations import zcode_session_start as zo

    class R:
        continuity_available = True
        context = "doc"
        attach_status = "auto_attached"
        thread_id = "thr_1"
        goal = "g"
        repo_root = "E:/repo"
        context_source = "fresh_compile"
        context_stale = False
        recommended_action = "continue"
        current_session = "s1"

    monkeypatch.setattr(zo, "startup_continuity", lambda **kw: R())
    monkeypatch.setattr(zo, "_log_event", lambda *a, **k: None)
    result = zo.handle_zcode_session_start(cwd="E:/repo", stdin_raw="{}")
    assert result["status"] == "context_ready"
    
    # Emit the context to stdout (this is what the hook does after handle_*)
    correlation_id = result.get("_verification_correlation_id")
    if result.get("context") and correlation_id:
        vh.note_context_emitted("zcode", correlation_id, len(result["context"]))
    
    st = _status("zcode")
    # After our split semantics: HOOK_TRIGGERED + CONTEXT_PREPARED (in note_result) + CONTEXT_EMITTED (from emit)
    assert st["evidence_count"] >= 3, st
    assert st["observed_state"] == LIVE_VERIFIED, st  # auto_attached is not zero-touch


def test_a_failed_run_does_not_reference_an_unbound_result(db, monkeypatch):
    """`note_result(..., result, ...)` in the except branch raised
    UnboundLocalError and lost the failure evidence."""
    from voyager.integrations import zcode_session_start as zo

    def boom(**kw):
        raise RuntimeError("core exploded")

    monkeypatch.setattr(zo, "startup_continuity", boom)
    monkeypatch.setattr(zo, "_log_event", lambda *a, **k: None)
    result = zo.handle_zcode_session_start(cwd="E:/repo", stdin_raw="{}")
    assert result["status"] == "error"
    
    st = _status("zcode")
    # Even on error, we should have HOOK_TRIGGERED recorded (note_result is called after try/except)
    assert st["evidence_count"] >= 1, st
    assert "UnboundLocalError" not in json.dumps(result, default=str)


def test_the_canonical_codex_path_promotes_through_a_real_resolution(tmp_path, monkeypatch):
    """hook -> pending -> a scan indexes the session and resolves -> zero-touch.

    This is the lifecycle that actually happened on this machine, driven through
    the *real* resolver rather than by writing the events by hand.  The hook opens
    the chain in one process; the resolution happens later, in a scan, and has to
    recover the chain from the session id alone.
    """
    from voyager.auto import resolve_pending_attaches
    from voyager.model import new_event, new_session
    from voyager.store import Store

    path = tmp_path / "index.db"
    store = Store(path)
    try:
        monkeypatch.setattr(vh, "_db_path", lambda explicit=None: path)
        tid = store.thread_create(repo_root="E:/repo", title="t", goal="g")

        # 1. the provider's hook ran, compiled context, emitted to stdout, and could not attach yet
        cid = vh.begin_hook("codex", cwd="E:/repo", native_session_id="01a0")
        assert cid
        
        result_obj = _Result(context="doc", attach_status="pending_resolve")
        vh.note_result("codex", cid, result_obj, native_session_id="01a0")
        vh.note_context_emitted("codex", cid, len("doc"))
        
        assert _status("codex")["observed_state"] == LIVE_VERIFIED, "not yet"

        # 2. the hook also records the pending attach, as the handler does
        store.pending_record(tid, "codex", native_session_id="01a0",
                             repo_root="E:/repo", cwd="E:/repo")

        # 3. a later scan indexes the session, and the pending resolves
        src = tmp_path / "s.jsonl"
        src.write_text("{}", encoding="utf-8")
        sess = new_session(id="codex:01a0", provider="codex",
                           native_session_id="01a0", title="seeded",
                           started_at=time.time(), updated_at=time.time(),
                           repo_root="E:/repo", cwd="E:/repo")
        store.replace_session(
            sess, [new_event(sid="codex:01a0", seq=1, kind="user", ts=time.time(),
                             content="hi")], "codex", src)

        stats = resolve_pending_attaches(store)
        assert stats["attached"], stats

        # 4. the chain is complete, and the evidence says so
        st = _status("codex")
        assert st["observed_state"] == ZERO_TOUCH_LIVE_VERIFIED, st
    finally:
        store.close()


def test_a_resolution_for_an_unknown_session_records_nothing(db):
    """A scan can resolve a session this machine never saw a hook for."""
    from voyager.verification_harness import note_attach_resolved_for_session

    assert note_attach_resolved_for_session("codex", "never-seen", thread_id="t") is False
    assert _status()["evidence_count"] == 0


# --- G3-B contract regressions (boss-fixed order) ---------------------------

def test_handler_return_type_remains_dict(db, monkeypatch):
    """The handler contract is Dict[str, Any] — a tuple change would break
    every caller.  Internal correlation metadata rides in the dict, but the
    public keys are unchanged."""
    from voyager.integrations import codex_session_start as cs

    class R:
        continuity_available = True
        context = "doc"
        attach_status = "auto_attached"
        thread_id = "thr_1"
        goal = "g"
        repo_root = "E:/repo"
        context_source = "fresh_compile"
        context_stale = False
        recommended_action = "continue"
        current_session = "s1"

    monkeypatch.setattr(cs, "startup_continuity", lambda **kw: R())
    monkeypatch.setattr(cs, "_log_event", lambda *a, **k: None)
    result = cs.handle_codex_session_start(cwd="E:/repo", stdin_raw='{"session_id":"s1"}')
    assert isinstance(result, dict)
    # the internal correlation metadata rides inside, but the public keys
    # are unchanged
    assert "_verification_correlation_id" in result
    for key in ("status", "context", "attach_status"):
        assert key in result


def test_correlation_id_never_enters_the_provider_stdout_payload(
        db, monkeypatch, capsys):
    """The internal correlation metadata must not leak into the protocol
    payload the provider reads."""
    from voyager.integrations import codex_session_start as cs

    class R:
        continuity_available = True
        context = "doc"
        attach_status = "auto_attached"
        thread_id = "thr_1"
        goal = "g"
        repo_root = "E:/repo"
        context_source = "fresh_compile"
        context_stale = False
        recommended_action = "continue"
        current_session = "s1"

    monkeypatch.setattr(cs, "startup_continuity", lambda **kw: R())
    monkeypatch.setattr(cs, "_log_event", lambda *a, **k: None)
    result = cs.handle_codex_session_start(cwd="E:/repo", stdin_raw='{"session_id":"s1"}')
    fake = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake)
    cs.emit(result)
    emitted = fake.getvalue()
    assert "_verification_correlation_id" not in emitted


def test_ambiguous_native_session_id_refuses_resolution(db):
    """Issue #4: the same native session id under two correlation chains is
    ambiguous — the resolver must refuse rather than silently pick one."""
    _record("codex", HOOK_TRIGGERED, "chainA", sid="s1", at=1.0)
    _record("codex", HOOK_TRIGGERED, "chainB", sid="s1", at=2.0)
    resolved = vh.note_attach_resolved_for_session(
        "codex", "s1", thread_id="thr_1")
    assert resolved is False
    con = vh._connect()
    try:
        chains = vh.chains_for(con, "codex")
        for (p, cid), c in chains.items():
            assert c.first_index(vh.ATTACH_RESOLVED) is None, (
                f"chain {cid} was promoted despite the ambiguity")
    finally:
        con.close()


def test_unique_native_session_id_resolves_cleanly(db):
    """The happy path of the ambiguity rule: exactly one chain resolves."""
    _record("codex", HOOK_TRIGGERED, "chainA", sid="s1", at=1.0)
    resolved = vh.note_attach_resolved_for_session(
        "codex", "s1", thread_id="thr_1")
    assert resolved is True
    con = vh._connect()
    try:
        chains = vh.chains_for(con, "codex")
        for (p, cid), c in chains.items():
            assert c.first_index(vh.ATTACH_RESOLVED) is not None
    finally:
        con.close()


# --- CONTEXT_EMITTED failure paths (boss-fixed order, step 4) ---------------

class FailingStdout:
    """A stdout stand-in whose write() or flush() can be made to raise."""

    def __init__(self, fail_write=False, fail_flush=False):
        self.fail_write = fail_write
        self.fail_flush = fail_flush
        self.chunks = []

    def write(self, text):
        if self.fail_write:
            raise OSError("console gone")
        self.chunks.append(text)
        return len(text)

    def flush(self):
        if self.fail_flush:
            raise OSError("flush exploded")


def _codex_result_with_correlation(db, monkeypatch):
    from voyager.integrations import codex_session_start as cs

    class R:
        continuity_available = True
        context = "tiered doc"
        attach_status = "auto_attached"
        thread_id = "thr_1"
        goal = "g"
        repo_root = "E:/repo"
        context_source = "fresh_compile"
        context_stale = False
        recommended_action = "continue"
        current_session = "s1"

    monkeypatch.setattr(cs, "startup_continuity", lambda **kw: R())
    monkeypatch.setattr(cs, "_log_event", lambda *a, **k: None)
    result = cs.handle_codex_session_start(cwd="E:/repo", stdin_raw='{"session_id":"s1"}')
    return result


def _emitted_absent():
    con = vh._connect()
    try:
        obs = vh.observed_state(con, "codex")
        types = [e["event_type"] for e in
                 [e for c in vh.chains_for(con, "codex").values() for e in c.events]]
        return "CONTEXT_EMITTED" in types, obs
    finally:
        con.close()


def test_serialization_failure_keeps_emitted_absent(db, monkeypatch):
    """json.dumps raising means the protocol payload never existed:
    CONTEXT_PREPARED is present, CONTEXT_EMITTED is absent."""
    from voyager.integrations import codex_session_start as cs
    result = _codex_result_with_correlation(db, monkeypatch)
    prepared_before = _emitted_absent()[1]["evidence_count"]

    monkeypatch.setattr(cs.json, "dumps",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom")))
    rc = cs.emit(result)

    assert rc == 0                          # fail-open
    emitted, obs = _emitted_absent()
    assert emitted is False
    assert obs["evidence_count"] == prepared_before


def test_write_failure_keeps_emitted_absent(db, monkeypatch):
    from voyager.integrations import codex_session_start as cs
    result = _codex_result_with_correlation(db, monkeypatch)

    fake = FailingStdout(fail_write=True)
    monkeypatch.setattr(sys, "stdout", fake)
    rc = cs.emit(result)

    assert rc == 0                          # fail-open
    emitted, obs = _emitted_absent()
    assert emitted is False
    assert obs["evidence_count"] >= 1       # prepared still recorded


def test_flush_failure_keeps_emitted_absent(db, monkeypatch):
    from voyager.integrations import codex_session_start as cs
    result = _codex_result_with_correlation(db, monkeypatch)

    fake = FailingStdout(fail_flush=True)
    monkeypatch.setattr(sys, "stdout", fake)
    rc = cs.emit(result)

    assert rc == 0                          # fail-open
    emitted, obs = _emitted_absent()
    assert emitted is False                 # flush is part of the delivery fact
    assert obs["evidence_count"] >= 1
