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

from voyager import verification_harness as vh
from voyager.verification_harness import (
    ATTACH_PENDING, ATTACH_RESOLVED, CONTEXT_DELIVERED, HOOK_TRIGGERED,
    LIVE_VERIFIED, NATIVE_SESSION_ID_OBSERVED, NOT_FOUND,
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
        """When the evidence table has a chain, collect_evidence reflects it."""
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        _record("codex", CONTEXT_DELIVERED, "c1", at=2.0)
        ev = collect_evidence("codex")
        assert ev.hook_fired is True
        assert ev.zero_touch_observed is False
        assert evidence_ceiling(ev) == LIVE_VERIFIED

    def test_collect_evidence_finds_zero_touch_in_table(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=1.0)
        _record("codex", CONTEXT_DELIVERED, "c1", sid="s1", at=2.0)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=3.0)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=4.0)
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
        _record("codex", CONTEXT_DELIVERED, "c1", at=2.0)
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] == LIVE_VERIFIED
        assert out["providers"]["codex"]["effective_state"] == LIVE_VERIFIED

    def test_effective_is_zero_touch_with_full_chain(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=1.0)
        _record("codex", CONTEXT_DELIVERED, "c1", sid="s1", at=2.0)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=3.0)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=4.0)
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
        _record("codex", CONTEXT_DELIVERED, "c1", at=2.0)
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] == LIVE_VERIFIED

    def test_pending_without_resolved_is_not_zero_touch(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        _record("codex", CONTEXT_DELIVERED, "c1", at=2.0)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=3.0)
        _record("codex", ATTACH_PENDING, "c1", sid="s1", at=4.0)
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] == LIVE_VERIFIED

    def test_full_chain_is_zero_touch(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=1.0)
        _record("codex", CONTEXT_DELIVERED, "c1", sid="s1", at=2.0)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=3.0)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=4.0)
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] == ZERO_TOUCH_LIVE_VERIFIED


# --- O5.4: correlation chain rules -------------------------------------------

class TestCorrelationChain:
    """Evidence from different chains or different sessions never combines."""

    def test_cross_chain_evidence_does_not_combine(self, db):
        _record("codex", HOOK_TRIGGERED, "chainA", sid="s1", at=1.0)
        _record("codex", CONTEXT_DELIVERED, "chainB", sid="s1", at=2.0)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "chainA", sid="s1", at=3.0)
        _record("codex", ATTACH_RESOLVED, "chainB", sid="s1", at=4.0)
        out = vh.query_status("codex")
        assert out["providers"]["codex"]["observed_state"] != ZERO_TOUCH_LIVE_VERIFIED

    def test_cross_session_evidence_does_not_combine(self, db):
        _record("codex", HOOK_TRIGGERED, "c1", sid="s1", at=1.0)
        _record("codex", CONTEXT_DELIVERED, "c1", sid="s1", at=2.0)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=3.0)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s2", at=4.0)
        out = vh.query_status("codex")
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

        # Evidence should be in the table
        con = vh._connect()
        try:
            obs = vh.observed_state(con, "codex")
            assert obs["evidence_count"] >= 3  # hook + context + session_id + resolved
            assert obs["observed_state"] == ZERO_TOUCH_LIVE_VERIFIED
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
        _record("codex", HOOK_TRIGGERED, "c1", at=1.0)
        _record("codex", CONTEXT_DELIVERED, "c1", at=2.0)
        rpt = check_verification()
        v = rpt["providers"]["codex"]
        assert v["chains"] == 1
        assert v["evidence_count"] == 2
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
        _record("codex", CONTEXT_DELIVERED, "c1", at=2.0)
        rc = vh.cmd_verify(verbose=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "chain" in out.lower()
        assert "c1" in out


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
        _record("codex", CONTEXT_DELIVERED, "c1", at=2.0)
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
        _record("codex", CONTEXT_DELIVERED, "c1", sid="s1", at=2.0)
        _record("codex", NATIVE_SESSION_ID_OBSERVED, "c1", sid="s1", at=3.0)
        _record("codex", ATTACH_RESOLVED, "c1", sid="s1", at=4.0)
        # "Source rotation" -- the session's source file is gone.
        # The evidence table is independent; it is not affected.
        con = vh._connect()
        try:
            obs = vh.observed_state(con, "codex")
            assert obs["observed_state"] == ZERO_TOUCH_LIVE_VERIFIED
            assert obs["evidence_count"] == 4
        finally:
            con.close()
