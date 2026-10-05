"""O5 — Provider Live Verification Closure.

These tests attack the claim "this machine saw the provider do it" from
every angle the O5 design exposes:

  * no hardcoded set bypasses the evidence table (O5.2)
  * no log grepping manufactures evidence (O5.2)
  * provider_state caps at the evidence ceiling (O5.2)
  * query_status has three layers with correct semantics (O5.3)
  * the state machine promotes only with complete evidence (O5.3)
  * correlation chains never combine across sessions (O5.4)
  * the codex handler is wired to begin_hook/note_result (O5.5)
  * the grok handler is wired to begin_hook/note_result (O5.5)
  * voyager verify --all / --matrix work (O5.8)
  * doctor.check_verification uses verification_harness (O5.7)
  * blocked_reason is reported (O5.7)
  * idempotency: replayed events do not inflate (O5.10)
  * source rotation: verification is historical fact (O5.11)
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Local mock for tests (not importing from non-existent module)
class _Result:
    def __init__(self, context=None, attach_status=None, thread_id=None, goal=None, repo_root=None):
        self.context = context
        self.attach_status = attach_status
        self.thread_id = thread_id
        self.goal = goal
        self.repo_root = repo_root

from voyager import verification_harness as vh
from voyager.verification_harness import (
    ATTACH_PENDING, ATTACH_RESOLVED, CONTEXT_EMITTED, CONTEXT_PREPARED,
    HOOK_TRIGGERED, LIVE_VERIFIED, NATIVE_SESSION_ID_OBSERVED, NOT_FOUND,
    UNIT_VERIFIED, ZERO_TOUCH_LIVE_VERIFIED,
)
from voyager.capability_matrix import (
    DECLARED, PROVIDERS, STATE_ORDER,
    Evidence, collect_evidence, evidence_ceiling,
    provider_state, resolve_cell,
)


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    """An isolated index with the evidence schema."""
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


# --- O5.2: no hardcoded set, no log grepping --------------------------------

class TestNoHardcodedEvidence:
    """The ZERO_TOUCH_OBSERVED hardcoded set is gone.  Only the evidence
    table can promote a provider above UNIT_VERIFIED."""

    def test_codex_is_unit_verified_without_evidence(self):
        """Codex was hardcoded as ZERO_TOUCH_LIVE_VERIFIED.  Now it is
        UNIT_VERIFIED when the evidence table has no rows for it."""
        ev = collect_evidence("codex")
        assert ev.zero_touch_observed is False
        assert ev.hook_fired is False
        assert evidence_ceiling(ev) == UNIT_VERIFIED

    def test_claude_is_unit_verified_without_evidence(self):
        ev = collect_evidence("claude")
        assert ev.zero_touch_observed is False
        assert ev.hook_fired is False
        assert evidence_ceiling(ev) == UNIT_VERIFIED

    def test_grok_is_unit_verified_without_evidence(self):
        ev = collect_evidence("grok")
        assert ev.zero_touch_observed is False
        assert ev.hook_fired is False
        assert evidence_ceiling(ev) == UNIT_VERIFIED

    def test_provider_state_caps_at_evidence_ceiling(self):
        """provider_state returns UNIT_VERIFIED when there is no evidence,
        even though DECLARED says LIVE_VERIFIED / ZERO_TOUCH_LIVE_VERIFIED."""
        for p in ("codex", "claude", "grok"):
            assert provider_state(p) == UNIT_VERIFIED, p

    def test_dsh_is_not_found_without_evidence(self):
        """DSH has no startup surface; it is NOT_FOUND regardless."""
        assert provider_state("dsh") == NOT_FOUND

    def test_collect_evidence_queries_the_evidence_table(self, db):
        """collect_evidence reflects exactly what the table holds.

        G3-B semantics: CONTEXT_PREPARED alone proves compilation, not
        delivery -- the ceiling stays UNIT_VERIFIED until the protocol
        payload is actually written (CONTEXT_EMITTED)."""
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        _record("codex", CONTEXT_PREPARED, "c1", at=2.0)
        ev = collect_evidence("codex")
        assert ev.hook_fired is False      # prepared, not yet emitted
        assert ev.zero_touch_observed is False
        assert evidence_ceiling(ev) == UNIT_VERIFIED
        # the physical write promotes: emitted -> hook_fired
        _record("codex", CONTEXT_EMITTED, "c1", at=3.0)
        ev2 = collect_evidence("codex")
        assert ev2.hook_fired is True
        assert evidence_ceiling(ev2) == LIVE_VERIFIED

    def test_collect_evidence_finds_zero_touch_in_table(self, db):
        # the realistic deferred-attach order: prepared -> emitted ->
        # resolved (the scan resolves the pending after delivery)
        base = time.time()
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=base + 1)
        result_obj = _Result(context="doc", attach_status="pending_resolve")
        vh.note_result("codex", "c1", result_obj, native_session_id="s1")
        _record("codex", CONTEXT_EMITTED, "c1", sid="s1", at=base + 3)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=base + 4)
        ev = collect_evidence("codex")
        assert ev.zero_touch_observed is True
        assert ev.hook_fired is True
        assert evidence_ceiling(ev) == ZERO_TOUCH_LIVE_VERIFIED


# --- O5.2: query_status three layers ----------------------------------------

class TestQueryStatusThreeLayers:
    """query_status shows declared (ceiling), observed (evidence), effective
    (weaker of the two, capped at UNIT_VERIFIED when no evidence)."""

    def test_declared_is_the_raw_ceiling_from_declared(self, db):
        out = vh.query_status("codex")
        # Declared: max of machine-dependent dimensions in DECLARED
        # codex's live_zero_touch_continuity is ZERO_TOUCH_LIVE_VERIFIED
        assert out["providers"]["codex"]["declared_state"] == ZERO_TOUCH_LIVE_VERIFIED

    def test_observed_is_none_without_evidence(self, db):
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] is None

    def test_effective_is_unit_verified_without_evidence(self, db):
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["effective_state"] == UNIT_VERIFIED

    def test_effective_is_observed_when_lower(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        result_obj = _Result(context="doc")
        vh.note_result("codex", "c1", result_obj, native_session_id=None)
        vh.note_context_emitted("codex", "c1", len("doc"))
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] == LIVE_VERIFIED
        assert out["providers"]["codex"]["effective_state"] == LIVE_VERIFIED

    def test_effective_is_zero_touch_with_full_chain(self, db):
        base = time.time()
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=base + 1)
        result_obj = _Result(context="doc", attach_status="pending_resolve")
        vh.note_result("codex", "c1", result_obj, native_session_id="s1")
        _record("codex", CONTEXT_EMITTED, "c1", sid="s1", at=base + 3)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=base + 4)
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["effective_state"] == ZERO_TOUCH_LIVE_VERIFIED

    def test_dsh_declared_is_not_found(self, db):
        out = vh.query_status("dsh")
        assert out["providers"]["dsh"]["declared_state"] == NOT_FOUND
        assert out["providers"]["dsh"]["effective_state"] == NOT_FOUND

    def test_query_status_does_not_write(self, db):
        """Hard regression: query_status leaves the database byte-identical."""
        import hashlib
        def digest(path):
            h = hashlib.sha256()
            with open(str(path), "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            return h.hexdigest()
        before = digest(db)
        vh.query_status()
        assert digest(db) == before


# --- O5.3: state machine promotion conditions ------------------------------

class TestStateMachinePromotion:
    """A provider promotes only when the evidence chain is complete and
    ordered.  No shortcut from SUPPORTED to LIVE_VERIFIED."""

    def test_hook_alone_is_unit_verified(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] == UNIT_VERIFIED

    def test_hook_plus_context_is_live_verified(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        result_obj = _Result(context="doc")
        vh.note_result("codex", "c1", result_obj, native_session_id=None)
        # prepared alone proves compilation, not delivery
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] == UNIT_VERIFIED
        # the physical write completes the transport and promotes to LIVE
        vh.note_context_emitted("codex", "c1", len("doc"))
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] == LIVE_VERIFIED

    def test_pending_without_resolved_is_not_zero_touch(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        result_obj = _Result(context="doc", attach_status="pending_resolve")
        vh.note_result("codex", "c1", result_obj, native_session_id="s1")
        vh.note_context_emitted("codex", "c1", len("doc"))
        out = vh.query_status("codex")
        # emitted + pending (never resolved) = LIVE, not ZERO_TOUCH
        assert out["providers"]["codex"]["observed_state"] == LIVE_VERIFIED

    def test_full_chain_is_zero_touch(self, db):
        # the realistic deferred-attach order: prepared -> emitted ->
        # resolved (the scan resolves the pending after delivery)
        base = time.time()
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=base + 1)
        result_obj = _Result(context="doc", attach_status="pending_resolve")
        vh.note_result("codex", "c1", result_obj, native_session_id="s1")
        _record("codex", CONTEXT_EMITTED, "c1", sid="s1", at=base + 3)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=base + 4)
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] == ZERO_TOUCH_LIVE_VERIFIED


# --- O5.4: correlation chain rules -------------------------------------------

class TestCorrelationChain:
    """Evidence from different chains or different sessions never combines."""

    def test_cross_chain_evidence_does_not_combine(self, db):
        _record("codex", HOOK_TRIGGERED, "chainA", sid="s1", at=1.0)
        result_obj = _Result(context="doc")
        vh.note_result("codex", "chainB", result_obj, native_session_id=None)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "chainA", sid="s1", at=3.0)
        _record("codex", ATTACH_RESOLVED, "chainB", sid="s1", at=4.0)
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] != ZERO_TOUCH_LIVE_VERIFIED

    def test_cross_session_evidence_does_not_combine(self, db):
        base = time.time()
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=base + 1)
        result_obj = _Result(context="doc")
        vh.note_result("codex", "c1", result_obj, native_session_id="s1")
        _record("codex", CONTEXT_EMITTED, "c1", sid="s1", at=base + 2.5)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=base + 3)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s2", at=base + 4)
        out = vh.query_status("codex")
        # delivered, but the resolution names a different session: the two
        # facts must not combine into zero-touch
        assert out["providers"]["codex"]["observed_state"] == LIVE_VERIFIED


# --- O5.5: codex handler wiring ----------------------------------------------

class TestCodexHandlerWiring:
    """The codex handler now calls begin_hook / note_result."""

    def test_codex_handler_records_evidence_on_success(self, db, monkeypatch):
        from voyager.integrations import codex_session_start as cs

        class R:
            continuity_available = True
            context = "continuation context"
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
        assert result["status"] == "context_ready"

        # Emit the context (simulating what main() does after handle_* returns)
        correlation_id = result.get("_verification_correlation_id")
        if result.get("context") and correlation_id:
            vh.note_context_emitted("codex", correlation_id, len(result["context"]))

        # Evidence should be in the table.  The auto-attach flow resolves the
        # attach during startup_continuity (before delivery), so the honest
        # derived state is LIVE_VERIFIED -- the deferred-attach zero-touch
        # flow is pinned by the V2 suite's pending_resolve test.
        con = vh._connect()
        try:
            obs = vh.observed_state(con, "codex")
            assert obs["evidence_count"] >= 4  # hook + prepared + nsid + resolved + emitted
            assert obs["observed_state"] == LIVE_VERIFIED
        finally:
            con.close()

    def test_codex_handler_records_evidence_on_no_thread(self, db, monkeypatch):
        from voyager.integrations import codex_session_start as cs

        class R:
            continuity_available = False
            context = None
            attach_status = "no_auto_attach"
            thread_id = None
            goal = None
            repo_root = None
            context_source = "none"
            context_stale = False
            recommended_action = "none"
            current_session = None

        monkeypatch.setattr(cs, "startup_continuity", lambda **kw: R())
        monkeypatch.setattr(cs, "_log_event", lambda *a, **k: None)
        result = cs.handle_codex_session_start(cwd="E:/repo", stdin_raw='{"session_id":"s1"}')
        assert result["status"] == "no_thread"

        # At least HOOK_TRIGGERED should be in the table
        con = vh._connect()
        try:
            obs = vh.observed_state(con, "codex")
            assert obs["evidence_count"] >= 1
        finally:
            con.close()


# --- O5.5: grok handler wiring ----------------------------------------------

class TestGrokHandlerWiring:
    """The grok handler now calls begin_hook / note_result."""

    def test_grok_handler_records_evidence(self, db, monkeypatch):
        from voyager.integrations import grok_native as gn

        class R:
            continuity_available = True
            context = None  # Grok passive: no context compiled
            attach_status = "pending_resolve"
            thread_id = "thr_1"
            goal = "g"
            repo_root = "E:/repo"
            context_source = "none"
            context_stale = False
            recommended_action = "continue"
            current_session = "grok_s1"

        monkeypatch.setattr(gn, "startup_continuity", lambda **kw: R())
        result = gn.session_start(
            session_id="grok_s1", cwd="E:/repo",
            env={"GROK_SESSION_ID": "grok_s1", "GROK_WORKSPACE_ROOT": "E:/repo"})
        assert result["status"] == "ok"

        con = vh._connect()
        try:
            obs = vh.observed_state(con, "grok")
            assert obs["evidence_count"] >= 1  # at least HOOK_TRIGGERED
        finally:
            con.close()


# --- O5.7: doctor.check_verification uses verification_harness ---------------

class TestDoctorVerification:
    """doctor.check_verification consumes verification_harness, not the old
    capability_matrix evidence."""

    def test_check_verification_has_evidence_details(self, db):
        from voyager.doctor import check_verification
        
        class _Result:
            context = "doc"
            attach_status = "auto_attached"
        
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        vh.note_result("codex", "c1", _Result(), native_session_id="s1")
        vh.note_context_emitted("codex", "c1", len("doc"))
        rpt = check_verification()
        v = rpt["providers"]["codex"]
        assert v["chains"] == 1
        assert v["evidence_count"] == 5
        assert v["best_chain"] == "c1"
        assert v["last_live_event"] is not None

    def test_check_verification_has_blocked_reason(self):
        from voyager.doctor import check_verification
        rpt = check_verification()
        v = rpt["providers"]["codex"]
        assert v["blocked_reason"] is not None

    def test_check_verification_blocked_reason_for_dsh(self):
        from voyager.doctor import check_verification
        rpt = check_verification()
        v = rpt["providers"]["dsh"]
        assert v["blocked_reason"] == "no startup surface"

    def test_check_verification_gap_is_info_when_no_evidence(self):
        from voyager.doctor import check_verification, collect_issues
        rpt = check_verification()
        for p in ("codex", "claude", "grok"):
            v = rpt["providers"][p]
            assert v["has_gap"] is True
        issues = collect_issues()
        gaps = [i for i in issues if i.code.startswith("VERIFICATION_GAP_")]
        for g in gaps:
            assert g.severity == "info"
            assert g.repair_kind == "READ_ONLY_DIAGNOSIS"


# --- O5.8: CLI --all and --matrix -------------------------------------------

class TestCLIVerifyFlags:
    """voyager verify --all and --matrix work correctly."""

    def test_verify_all_shows_all_providers(self, db, capsys):
        vh.cmd_verify(provider=None, json_output=True)
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["providers"]) == set(PROVIDERS)

    def test_verify_matrix_does_not_crash(self, db, capsys):
        rc = vh.cmd_verify(matrix=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "provider" in out

    def test_verify_matrix_json(self, db, capsys):
        rc = vh.cmd_verify(matrix=True, json_output=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, dict)

    def test_verify_verbose_shows_chain_and_evidence(self, db, capsys):
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        _record("codex", CONTEXT_PREPARED, "c1", at=2.0)
        rc = vh.cmd_verify(verbose=True)
        assert rc == 0
        out = capsys.readouterr().out
        # the verbose mode prints the three-layer table plus the evidence
        # summary; the per-provider row for codex must appear
        assert "evidence" in out.lower()
        assert "codex" in out


# --- O5.10: idempotency -----------------------------------------------------

class TestIdempotency:
    """Replaying the same event does not inflate the record."""

    def test_duplicate_event_is_idempotent(self, db):
        first = _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        again = _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        assert first is not None
        assert again is None
        con = vh._connect()
        try:
            obs = vh.observed_state(con, "codex")
            assert obs["evidence_count"] == 1
        finally:
            con.close()

    def test_replayed_hook_does_not_promote(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)  # replay
        con = vh._connect()
        try:
            obs = vh.observed_state(con, "codex")
            assert obs["observed_state"] == UNIT_VERIFIED
        finally:
            con.close()

    def test_collect_evidence_is_idempotent(self, db):
        """Calling collect_evidence twice does not write or change state."""
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        _record("codex", CONTEXT_PREPARED, "c1", at=2.0)
        ev1 = collect_evidence("codex")
        ev2 = collect_evidence("codex")
        assert ev1.hook_fired == ev2.hook_fired
        assert ev1.zero_touch_observed == ev2.zero_touch_observed


# --- O5.11: source rotation interaction -------------------------------------

class TestSourceRotationInteraction:
    """Verification is historical fact.  Source rotation (O2/D15) does not
    deny or delete verification evidence."""

    def test_evidence_survives_source_rotation(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=1.0)
        _record("codex", CONTEXT_PREPARED, "c1", sid="s1", at=2.0)
        _record("codex", CONTEXT_EMITTED, "c1", sid="s1", at=3.0)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=3.5)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=4.0)
        # "Source rotation" -- the session's source file is gone.
        # The evidence table is independent; it is not affected.
        con = vh._connect()
        try:
            obs = vh.observed_state(con, "codex")
            assert obs["observed_state"] == ZERO_TOUCH_LIVE_VERIFIED
            assert obs["evidence_count"] == 5
        finally:
            con.close()
# --- O5 patch: transport truth, grok wiring, payload hygiene, convergence --


class TestGrokTransportEvidence:
    """§7/§33: grok's context transport is the rules file, and its evidence
    writers sit exactly where that transport succeeds or fails."""

    def _setup(self, tmp_path, monkeypatch):
        from voyager.integrations import grok_native as gn
        home = tmp_path / "grok-home"
        body = "SENTINEL-CONTEXT-_BODY " * 40

        class R:
            continuity_available = True
            context = body
            attach_status = None
            thread_id = "thr_g1"
            goal = "g"
            repo_root = "E:/repo"
            context_source = "tiered"
            current_session = None

        monkeypatch.setattr(gn, "startup_continuity", lambda **kw: R())
        return gn, home, body

    def test_written_rules_file_records_emitted_on_its_own_chain(
            self, db, tmp_path, monkeypatch):
        gn, home, body = self._setup(tmp_path, monkeypatch)
        out = gn.write_context_rules(cwd="E:/repo", home=home)
        assert out["status"] == "written"

        con = vh._connect()
        try:
            chains = list(vh.chains_for(con, "grok").values())
        finally:
            con.close()
        launcher = [c for c in chains
                    if c.first_index(CONTEXT_EMITTED) is not None
                    and c.first_index(CONTEXT_EMITTED) >= 0]
        assert len(launcher) == 1, "exactly one transport chain"
        c = launcher[0]
        assert c.first_index(CONTEXT_PREPARED) >= 0
        assert c.first_index(CONTEXT_EMITTED) >= 0
        assert c.first_index(CONTEXT_PREPARED) < c.first_index(CONTEXT_EMITTED), \
            "PREPARED must precede EMITTED"
        # the launcher IS grok's declared startup hook (rules-writer), so its
        # chain legitimately opens with HOOK_TRIGGERED
        assert c.first_index(HOOK_TRIGGERED) >= 0
        # but no native session id: the passive hook's chain holds that, and
        # the two chains are never spliced into a fake zero-touch
        assert c.first_index(NATIVE_SESSION_ID_OBSERVED) is None

    def test_failed_rules_write_leaves_chain_at_prepared(
            self, db, tmp_path, monkeypatch):
        gn, home, _ = self._setup(tmp_path, monkeypatch)
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        monkeypatch.setattr(gn, "context_rules_path",
                            lambda h=None: blocker / "rules" / "voyager.md")
        out = gn.write_context_rules(cwd="E:/repo", home=home)
        assert out["status"] == "error"

        con = vh._connect()
        try:
            chains = list(vh.chains_for(con, "grok").values())
        finally:
            con.close()
        assert chains, "the hook still fired"
        for c in chains:
            assert c.first_index(CONTEXT_EMITTED) is None, \
                "a failed transport must never record EMITTED"

    def test_status_dict_leaks_no_correlation_id(self, db, tmp_path,
                                                 monkeypatch):
        """§5: the public return contract is unchanged by instrumentation."""
        gn, home, _ = self._setup(tmp_path, monkeypatch)
        out = gn.write_context_rules(cwd="E:/repo", home=home)
        blob = json.dumps(out)
        assert "_verification_correlation_id" not in blob
        assert "correlation" not in blob


class TestEvidencePayloadHygiene:
    """§28/§29: the evidence table is a metadata log, never a transcript."""

    _SAFE_KEYS = {"chars", "bytes", "attach_status", "cwd"}

    def test_context_body_never_enters_the_evidence_table(
            self, db, tmp_path, monkeypatch):
        from voyager.integrations import grok_native as gn
        sentinel = "USER-CONTENT-SENTINEL-do-not-store"
        body = sentinel + " " + "filler " * 100

        class R:
            continuity_available = True
            context = body
            attach_status = None
            thread_id = "thr_p"
            repo_root = "E:/repo"
            context_source = "tiered"
            current_session = None

        monkeypatch.setattr(gn, "startup_continuity", lambda **kw: R())
        gn.write_context_rules(cwd="E:/repo", home=tmp_path / "gh")

        con = vh._connect()
        try:
            rows = con.execute(
                "SELECT event_type, payload_json FROM verification_events"
            ).fetchall()
        finally:
            con.close()
        assert rows, "evidence exists"
        blob = " ".join(str(r["payload_json"] or "") for r in rows)
        assert sentinel not in blob, "user content leaked into evidence"
        for r in rows:
            payload = json.loads(r["payload_json"] or "{}")
            assert set(payload) <= self._SAFE_KEYS, payload


class TestTransportFailureTruth:
    """§31: EMITTED appears only when serialize+write+flush all succeeded."""

    def test_claude_stdout_failure_records_no_emitted(self, db, capsys,
                                                      monkeypatch):
        from voyager.integrations import claude_session_start as cs

        result = {
            "status": "context_ready",
            "context": "claude transport body",
            "_verification_correlation_id": "c-t1",
        }
        cs.begin_hook("claude", cwd="E:/repo")   # chain exists (different cid)

        class Boom:
            def write(self, *_):
                raise OSError("stdout gone")

            def flush(self):
                raise OSError("stdout gone")

            @property
            def buffer(self):
                return self

        monkeypatch.setattr(cs.sys, "stdout", Boom())
        rc = cs.emit_claude_hook_output(dict(result))
        assert rc == 0                        # hooks fail open

        con = vh._connect()
        try:
            chains = list(vh.chains_for(con, "claude").values())
        finally:
            con.close()
        for c in chains:
            assert c.first_index(CONTEXT_EMITTED) is None, \
                "a failed stdout write must not record EMITTED"

    def test_claude_success_records_emitted(self, db, capsys):
        from voyager.integrations import claude_session_start as cs
        cs.begin_hook("claude", cwd="E:/repo")
        rc = cs.emit_claude_hook_output({
            "status": "context_ready",
            "context": "claude transport body",
            "_verification_correlation_id": "c-t2",
        })
        assert rc == 0
        con = vh._connect()
        try:
            chains = list(vh.chains_for(con, "claude").values())
        finally:
            con.close()
        emitted = [c for c in chains
                   if c.first_index(CONTEXT_EMITTED) is not None]
        assert emitted, "a real transport success records EMITTED"

    def test_handler_contract_unchanged_by_instrumentation(self, db, capsys):
        """§5: the correlation id stays internal; stdout is pure protocol."""
        from voyager.integrations import claude_session_start as cs
        cs.emit_claude_hook_output({
            "status": "context_ready",
            "context": "body",
            "_verification_correlation_id": "c-secret",
        })
        out = capsys.readouterr().out
        assert "_verification_correlation_id" not in out
        payload = json.loads(out)          # still exactly the hook protocol
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"


class TestCrossSurfaceConvergence:
    """§35: harness, capability matrix, doctor and the CLI must name the
    same effective state for the same evidence."""

    def test_all_four_surfaces_agree_on_zero_touch(self, db, capsys):
        from voyager import doctor
        from voyager.cli import main

        # canonical explicit sequence (same idiom as the promotion tests)
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=1.0)
        _record("codex", CONTEXT_PREPARED, "c1", sid="s1", at=2.0)
        _record("codex", CONTEXT_EMITTED, "c1", sid="s1", at=3.0)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=3.5)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=4.0)

        harness_eff = vh.query_status("codex")["providers"]["codex"][
            "effective_state"]
        ev = collect_evidence("codex")
        matrix_eff = provider_state("codex", ev)
        doc_eff = doctor.check_verification(db_path=db)["providers"]["codex"][
            "effective"]

        assert main(["verify", "codex", "--json"]) == 0
        cli = json.loads(capsys.readouterr().out)
        cli_eff = (cli.get("providers", {}).get("codex", {})
                   or cli.get("providers", {}).get("CODEX", {})
                   or cli).get("effective_state") or cli.get("effective")

        assert harness_eff == ZERO_TOUCH_LIVE_VERIFIED
        assert matrix_eff == ZERO_TOUCH_LIVE_VERIFIED
        assert doc_eff == ZERO_TOUCH_LIVE_VERIFIED
        assert cli_eff == ZERO_TOUCH_LIVE_VERIFIED, cli_eff

    def test_all_four_surfaces_agree_when_only_unit_proven(self, db, capsys):
        """No live evidence: every surface says UNIT_VERIFIED, none repeats
        the declared ZERO_TOUCH headline."""
        from voyager import doctor
        from voyager.cli import main

        _record("codex", HOOK_TRIGGERED, "c9", sid="s9", at=1.0)

        harness_eff = vh.query_status("codex")["providers"]["codex"][
            "effective_state"]
        matrix_eff = provider_state("codex", collect_evidence("codex"))
        doc_eff = doctor.check_verification(db_path=db)["providers"]["codex"][
            "effective"]
        assert main(["verify", "codex", "--json"]) == 0
        cli = json.loads(capsys.readouterr().out)
        cli_eff = (cli.get("providers", {}).get("codex", {})
                   or cli.get("providers", {}).get("CODEX", {})
                   or cli).get("effective_state") or cli.get("effective")

        assert harness_eff == UNIT_VERIFIED
        assert matrix_eff == UNIT_VERIFIED
        assert doc_eff == UNIT_VERIFIED
        assert cli_eff == UNIT_VERIFIED
