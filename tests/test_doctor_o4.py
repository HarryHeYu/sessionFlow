"""O4 -- Doctor / Safe Self-healing.

Tests the canonical issue model, the new diagnostic checks (leases, pending,
verification, cache), the safe fix layer, and the mutation guarantee:

  * ``doctor --fix`` runs only ``SAFE_DERIVED_REPAIR``.
  * ``USER_DECISION_REQUIRED`` issues are never touched by ``--fix``, even when
    they look stale (mutation test: set up a stale lease, run ``--fix``,
    assert the lease row is byte-identical).
  * Plain ``doctor`` is always read-only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from voyager import doctor
from voyager.doctor import (
    CRITICAL, INFO, WARNING,
    READ_ONLY_DIAGNOSIS, SAFE_DERIVED_REPAIR,
    USER_DECISION_REQUIRED, EXTERNAL_PROVIDER_ISSUE,
    Issue, collect_issues, apply_fix, _legacy_kind,
)


# --- helpers ----------------------------------------------------------------

def _make_store(tmp_path: Path):
    """Create a fresh store at a known path, return (store, path)."""
    from voyager.store import Store
    path = tmp_path / "test.db"
    store = Store(path)
    return store, path


def _seed_session(store, sid="codex:test1", provider="codex",
                  source_path=None, cwd="/repo", repo_root="/repo"):
    """Insert a minimal session for testing."""
    import time as _t
    now = _t.time()
    store.con.execute(
        """INSERT OR REPLACE INTO sessions(
               id, provider, native_id, title, started_at, updated_at,
               cwd, repo_root, can_resume, resume_cmd)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sid, provider, sid.split(":")[-1], "test", now, now,
         cwd, repo_root, 1, "codex resume x"))
    if source_path:
        p = Path(source_path)
        store.con.execute(
            "INSERT OR REPLACE INTO sources(provider, path, sid, mtime, size)"
            " VALUES (?,?,?,?,?)",
            (provider, str(p), sid, _t.time(), 100))
    store.con.commit()
    return sid


# --- O4.1: canonical Issue model --------------------------------------------

class TestIssueModel:
    def test_issue_has_all_canonical_fields(self):
        issue = Issue(
            code="TEST_CODE", severity=WARNING, category="test",
            message="something", evidence="evidence here",
            suggested_action="do something", auto_fixable=False,
            repair_kind=USER_DECISION_REQUIRED,
        )
        d = issue.to_dict()
        for field in ("code", "severity", "category", "message",
                      "evidence", "suggested_action", "auto_fixable",
                      "repair_kind"):
            assert field in d, field

    def test_issue_to_dict_has_legacy_compat_keys(self):
        issue = Issue(code="X", severity=CRITICAL, category="c",
                      message="m", repair_kind=USER_DECISION_REQUIRED)
        d = issue.to_dict()
        assert d["id"] == "X"
        assert d["detail"] == "m"
        assert d["kind"] == "blocking"

    def test_legacy_kind_mapping(self):
        assert _legacy_kind(CRITICAL, READ_ONLY_DIAGNOSIS) == "blocking"
        assert _legacy_kind(CRITICAL, USER_DECISION_REQUIRED) == "blocking"
        assert _legacy_kind(INFO, EXTERNAL_PROVIDER_ISSUE) == "external"
        assert _legacy_kind(WARNING, USER_DECISION_REQUIRED) == "warning"
        assert _legacy_kind(WARNING, SAFE_DERIVED_REPAIR) == "warning"
        assert _legacy_kind(INFO, READ_ONLY_DIAGNOSIS) == "non-blocking"
        assert _legacy_kind(INFO, SAFE_DERIVED_REPAIR) == "non-blocking"

    def test_repair_kinds_are_four(self):
        assert len(doctor.REPAIR_KINDS) == 4
        assert set(doctor.REPAIR_KINDS) == {
            READ_ONLY_DIAGNOSIS, SAFE_DERIVED_REPAIR,
            USER_DECISION_REQUIRED, EXTERNAL_PROVIDER_ISSUE,
        }

    def test_severity_levels(self):
        assert INFO == "info"
        assert WARNING == "warning"
        assert CRITICAL == "critical"


# --- O4.2: collect_issues covers every category -----------------------------

class TestCollectIssues:
    def test_collect_issues_never_raises(self):
        issues = collect_issues()
        assert isinstance(issues, list)

    def test_collect_issues_includes_known_debts(self):
        issues = collect_issues()
        codes = {i.code for i in issues}
        assert "L1_STRONG_FAIRNESS" in codes
        assert "EXTERNAL_CODEX_APPSERVER_CONPTY_POPUP" in codes

    def test_collect_issues_includes_o1_debts(self):
        """O4.13: O1 debts are reported, not fixed."""
        issues = collect_issues()
        codes = {i.code for i in issues}
        assert "OVERVIEW_RECENT_SESSIONS_N1" in codes
        assert "SCAN_MATERIALISE_ALL_BEFORE_WRITE" in codes

    def test_store_unavailable_is_critical(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor, "_store_path",
                            lambda explicit=None: tmp_path / "nope.db")
        issues = collect_issues(db_path=tmp_path / "nope.db")
        store_issues = [i for i in issues if i.code == "STORE_UNAVAILABLE"]
        assert len(store_issues) == 1
        assert store_issues[0].severity == CRITICAL
        assert store_issues[0].repair_kind == READ_ONLY_DIAGNOSIS

    def test_ambiguity_is_user_decision(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor, "check_continuity",
                            lambda repo=None, db_path=None: {
                                "active_threads": 2, "ambiguous": True,
                                "ambiguous_repos": ["E:/r"],
                                "coverage": None, "pending": 0,
                            })
        issues = collect_issues(db_path=tmp_path / "x.db")
        amb = [i for i in issues if i.code == "AMBIGUOUS_WORKTHREAD"]
        assert len(amb) == 1
        assert amb[0].severity == CRITICAL
        assert amb[0].repair_kind == USER_DECISION_REQUIRED
        assert not amb[0].auto_fixable

    def test_literal_unknown_is_user_decision(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor, "check_continuity",
                            lambda repo=None, db_path=None: {
                                "active_threads": 0, "ambiguous": False,
                                "coverage": {"events": 10, "unclassified": 5,
                                             "classified_pct": 50.0,
                                             "literal_unknown": 3},
                                "pending": 0,
                            })
        issues = collect_issues(db_path=tmp_path / "x.db")
        prov = [i for i in issues if i.code == "PROVENANCE_LITERAL_UNKNOWN"]
        assert len(prov) == 1
        assert prov[0].severity == CRITICAL
        assert prov[0].repair_kind == USER_DECISION_REQUIRED


# --- O4.3: retention diagnostics --------------------------------------------

class TestRetentionDiagnostics:
    def test_retention_info_not_warning(self, tmp_path, monkeypatch):
        """Retained history with live members is informational, not a fault."""
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "src.jsonl")
            # mark as retained
            store.con.execute(
                "UPDATE sessions SET source_state='SOURCE_MISSING',"
                " source_missing_since=? WHERE id=?",
                (time.time(), sid))
            store.con.commit()
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            issues = collect_issues(db_path=path)
            ret = [i for i in issues if i.code == "RETENTION_HISTORY_HELD"]
            assert len(ret) == 1
            assert ret[0].severity == INFO
            assert ret[0].repair_kind == READ_ONLY_DIAGNOSIS
            # stranded should NOT appear (no thread)
            stranded = [i for i in issues
                        if i.code == "RETENTION_STRANDED_WORKTHREAD"]
            assert len(stranded) == 0
        finally:
            store.close()

    def test_stranded_thread_is_warning(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "src.jsonl")
            tid = store.thread_create(repo_root="/repo", title="t", goal="g")
            store.thread_attach(tid, sid)
            # mark as retained (strands the thread)
            store.con.execute(
                "UPDATE sessions SET source_state='SOURCE_MISSING',"
                " source_missing_since=? WHERE id=?",
                (time.time(), sid))
            store.con.commit()
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            issues = collect_issues(db_path=path)
            stranded = [i for i in issues
                        if i.code == "RETENTION_STRANDED_WORKTHREAD"]
            assert len(stranded) == 1
            assert stranded[0].severity == WARNING
            assert stranded[0].repair_kind == USER_DECISION_REQUIRED
            assert not stranded[0].auto_fixable
        finally:
            store.close()


# --- O4.4: pending attach health --------------------------------------------

class TestPendingHealth:
    def test_stale_pending_is_warning(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        try:
            tid = store.thread_create(repo_root="/r", title="t", goal="g")
            # create an old pending record
            store.pending_record(tid, "codex")
            # backdate it
            store.con.execute(
                "UPDATE thread_pending SET created_at=? "
                "WHERE thread_id=? AND provider=?",
                (time.time() - 7200, tid, "codex"))
            store.con.commit()
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            issues = collect_issues(db_path=path)
            stale = [i for i in issues if i.code == "PENDING_STALE"]
            assert len(stale) == 1
            assert stale[0].severity == WARNING
            assert stale[0].repair_kind == USER_DECISION_REQUIRED
            assert not stale[0].auto_fixable
        finally:
            store.close()

    def test_archived_orphan_pending_is_warning(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        try:
            tid = store.thread_create(repo_root="/r", title="t", goal="g")
            store.pending_record(tid, "codex")
            # archive the thread
            store.thread_set_status(tid, "archived")
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            issues = collect_issues(db_path=path)
            orphans = [i for i in issues
                       if i.code == "PENDING_ARCHIVED_ORPHAN"]
            assert len(orphans) == 1
            assert orphans[0].severity == WARNING
            # Even provably impossible -> still USER_DECISION_REQUIRED
            assert orphans[0].repair_kind == USER_DECISION_REQUIRED
            assert not orphans[0].auto_fixable
        finally:
            store.close()


# --- O4.5: lease health -----------------------------------------------------

class TestLeaseHealth:
    def test_expired_lease_is_warning(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        try:
            tid = store.thread_create(repo_root="/r", title="t", goal="g")
            # acquire a lease with a stale heartbeat
            granted, lease = store.thread_lease_acquire(tid, "codex", pid=1)
            assert granted
            # backdate the heartbeat past LEASE_HEARTBEAT_TIMEOUT
            from voyager.store import LEASE_HEARTBEAT_TIMEOUT
            store.con.execute(
                "UPDATE thread_leases SET heartbeat_at=? WHERE thread_id=?",
                (time.time() - LEASE_HEARTBEAT_TIMEOUT - 10, tid))
            store.con.commit()
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            issues = collect_issues(db_path=path)
            expired = [i for i in issues if i.code == "LEASE_EXPIRED"]
            assert len(expired) == 1
            assert expired[0].severity == WARNING
            assert expired[0].repair_kind == USER_DECISION_REQUIRED
            assert not expired[0].auto_fixable
        finally:
            store.close()


# --- O4.6: verification diagnostics -----------------------------------------

class TestVerificationDiagnostics:
    def test_verification_has_three_layers(self):
        rpt = doctor.check_verification()
        for p in ("codex", "claude", "grok"):
            v = rpt["providers"][p]
            assert "declared" in v
            assert "observed" in v
            assert "effective" in v

    def test_verification_gap_is_info(self, tmp_path, monkeypatch):
        """A provider that is declared > observed is informational, not broken."""
        issues = collect_issues()
        gaps = [i for i in issues if i.code.startswith("VERIFICATION_GAP_")]
        for g in gaps:
            assert g.severity == INFO
            assert g.repair_kind == READ_ONLY_DIAGNOSIS


# --- O4.7: hook/provider health ---------------------------------------------

class TestHookProviderHealth:
    def test_hook_config_invalid_is_blocking(self, tmp_path, monkeypatch):
        """An invalid hook config is blocking; not-yet-fired is informational."""
        from voyager.capability_matrix import Evidence
        monkeypatch.setattr(doctor, "check_hook_config",
                            lambda p: {"registered": True, "valid": False,
                                       "error": "bad json"})
        monkeypatch.setattr(doctor, "collect_evidence",
                            lambda p: Evidence(
                                installed=True, hook_registered=True,
                                hook_fired=False, last_trigger=None))
        issues = collect_issues(db_path=tmp_path / "x.db")
        invalid = [i for i in issues if i.code == "HOOK_CONFIG_INVALID"]
        assert len(invalid) >= 1
        assert invalid[0].severity == CRITICAL
        assert invalid[0].repair_kind == USER_DECISION_REQUIRED


# --- O4.8: cache health -----------------------------------------------------

class TestCacheHealth:
    def test_stale_cache_is_safe_derived_repair(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "s.jsonl")
            # insert a cache entry that references the session but is older
            cache_key = "ctx_cache_" + sid
            old_ts = time.time() - 3600
            store.meta_set(cache_key, json.dumps({
                "compiled_at": old_ts,
                "session_ids": [sid],
            }))
            # update the session to be newer than the cache
            store.con.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (time.time(), sid))
            store.con.commit()
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            cache_rpt = doctor.check_cache(path)
            assert cache_rpt["stale"], "expected stale cache entry"
            assert cache_rpt["auto_fixable"]

            issues = collect_issues(db_path=path)
            stale = [i for i in issues if i.code == "CACHE_STALE"]
            assert len(stale) == 1
            assert stale[0].severity == WARNING
            assert stale[0].repair_kind == SAFE_DERIVED_REPAIR
            assert stale[0].auto_fixable
        finally:
            store.close()

    def test_fresh_cache_is_not_flagged(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "s.jsonl")
            # insert a cache entry newer than the session
            cache_key = "ctx_cache_" + sid
            new_ts = time.time()
            store.meta_set(cache_key, json.dumps({
                "compiled_at": new_ts,
                "session_ids": [sid],
            }))
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            cache_rpt = doctor.check_cache(path)
            assert not cache_rpt.get("stale"), "should not be stale"
        finally:
            store.close()


# --- O4.9+O4.10+O4.12: safe fix layer + mutation test -----------------------

class TestApplyFix:
    def test_dry_run_does_nothing(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "s.jsonl")
            cache_key = "ctx_cache_" + sid
            old_ts = time.time() - 3600
            store.meta_set(cache_key, json.dumps({
                "compiled_at": old_ts,
                "session_ids": [sid],
            }))
            store.con.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (time.time(), sid))
            store.con.commit()
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)

            result = apply_fix(db_path=path, dry_run=True)
            assert result["dry_run"] is True
            assert len(result["executed"]) >= 1
            # the cache entry must still be there
            assert store.meta_get(cache_key) is not None
        finally:
            store.close()

    def test_fix_clears_stale_cache(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "s.jsonl")
            cache_key = "ctx_cache_" + sid
            old_ts = time.time() - 3600
            store.meta_set(cache_key, json.dumps({
                "compiled_at": old_ts,
                "session_ids": [sid],
            }))
            store.con.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (time.time(), sid))
            store.con.commit()
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)

            result = apply_fix(db_path=path, dry_run=False)
            assert result["dry_run"] is False
            # the stale cache entry should be gone
            assert store.meta_get(cache_key) is None
            # at least one step should have ok=True
            assert any(s.get("ok") for s in result["executed"])
        finally:
            store.close()

    def test_fix_does_not_touch_lease(self, tmp_path, monkeypatch):
        """MUTATION TEST: a USER_DECISION_REQUIRED issue (stale lease) must
        survive ``apply_fix`` byte-identical."""
        store, path = _make_store(tmp_path)
        try:
            tid = store.thread_create(repo_root="/r", title="t", goal="g")
            granted, lease = store.thread_lease_acquire(tid, "codex", pid=1)
            assert granted
            from voyager.store import LEASE_HEARTBEAT_TIMEOUT
            store.con.execute(
                "UPDATE thread_leases SET heartbeat_at=? WHERE thread_id=?",
                (time.time() - LEASE_HEARTBEAT_TIMEOUT - 10, tid))
            store.con.commit()
            # snapshot the lease row
            before = dict(store.q(
                "SELECT * FROM thread_leases WHERE thread_id=?", (tid,))[0])
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)

            result = apply_fix(db_path=path, dry_run=False)
            # the lease row must be unchanged
            after = dict(store.q(
                "SELECT * FROM thread_leases WHERE thread_id=?", (tid,))[0])
            assert before == after, "apply_fix must not touch leases"
        finally:
            store.close()

    def test_fix_does_not_touch_pending(self, tmp_path, monkeypatch):
        """MUTATION TEST: pending records (USER_DECISION_REQUIRED) must
        survive ``apply_fix`` unchanged."""
        store, path = _make_store(tmp_path)
        try:
            tid = store.thread_create(repo_root="/r", title="t", goal="g")
            store.pending_record(tid, "codex")
            before_count = store.q(
                "SELECT COUNT(*) AS n FROM thread_pending")[0]["n"]
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)

            apply_fix(db_path=path, dry_run=False)
            after_count = store.q(
                "SELECT COUNT(*) AS n FROM thread_pending")[0]["n"]
            assert before_count == after_count, \
                "apply_fix must not touch pending records"
        finally:
            store.close()

    def test_fix_does_not_touch_retained_history(self, tmp_path, monkeypatch):
        """MUTATION TEST: retained history (USER_DECISION_REQUIRED) must
        survive ``apply_fix`` unchanged."""
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "src.jsonl")
            store.con.execute(
                "UPDATE sessions SET source_state='SOURCE_MISSING',"
                " source_missing_since=? WHERE id=?",
                (time.time(), sid))
            store.con.commit()
            before = dict(store.q(
                "SELECT * FROM sessions WHERE id=?", (sid,))[0])
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)

            apply_fix(db_path=path, dry_run=False)
            after = dict(store.q(
                "SELECT * FROM sessions WHERE id=?", (sid,))[0])
            assert before == after, \
                "apply_fix must not touch retained history"
        finally:
            store.close()

    def test_fix_does_not_resolve_ambiguity(self, tmp_path, monkeypatch):
        """MUTATION TEST: ambiguous threads (USER_DECISION_REQUIRED) must
        survive ``apply_fix`` unchanged."""
        store, path = _make_store(tmp_path)
        try:
            store.thread_create(repo_root="/r", title="one", goal="g")
            store.thread_create(repo_root="/r", title="two", goal="g")
            before = store.q("SELECT COUNT(*) AS n FROM threads")[0]["n"]
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)

            apply_fix(db_path=path, dry_run=False)
            after = store.q("SELECT COUNT(*) AS n FROM threads")[0]["n"]
            assert before == after, \
                "apply_fix must not resolve ambiguity"
        finally:
            store.close()

    def test_fix_with_nothing_to_fix(self, tmp_path, monkeypatch):
        """A clean store should produce no executed steps."""
        store, path = _make_store(tmp_path)
        try:
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            result = apply_fix(db_path=path, dry_run=False)
            assert result["fixable"] == 0
            assert result["executed"] == []
        finally:
            store.close()


# --- O4.10: CLI --fix / --dry-run / --json ----------------------------------

class TestCLIDoctorFix:
    def test_doctor_fix_dry_run(self, tmp_path, monkeypatch):
        from voyager.cli import main
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "s.jsonl")
            cache_key = "ctx_cache_" + sid
            old_ts = time.time() - 3600
            store.meta_set(cache_key, json.dumps({
                "compiled_at": old_ts,
                "session_ids": [sid],
            }))
            store.con.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (time.time(), sid))
            store.con.commit()
        finally:
            store.close()

        monkeypatch.setattr(doctor, "_store_path",
                            lambda explicit=None: path)
        rc = main(["--db", str(path), "doctor", "--fix", "--dry-run"])
        assert rc == 0

    def test_doctor_fix_json(self, tmp_path, monkeypatch, capsys):
        from voyager.cli import main
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "s.jsonl")
            cache_key = "ctx_cache_" + sid
            old_ts = time.time() - 3600
            store.meta_set(cache_key, json.dumps({
                "compiled_at": old_ts,
                "session_ids": [sid],
            }))
            store.con.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (time.time(), sid))
            store.con.commit()
        finally:
            store.close()

        monkeypatch.setattr(doctor, "_store_path",
                            lambda explicit=None: path)
        capsys.readouterr()
        rc = main(["--db", str(path), "doctor", "--fix", "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "executed" in data
        assert rc == 0

    def test_doctor_plain_is_read_only(self, tmp_path, monkeypatch, capsys):
        """Plain `doctor` must not modify anything."""
        from voyager.cli import main
        store, path = _make_store(tmp_path)
        try:
            sid = _seed_session(store, source_path=tmp_path / "s.jsonl")
            cache_key = "ctx_cache_" + sid
            store.meta_set(cache_key, json.dumps({
                "compiled_at": time.time() - 3600,
                "session_ids": [sid],
            }))
            store.con.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (time.time(), sid))
            store.con.commit()
            before = store.meta_get(cache_key)
        finally:
            store.close()

        monkeypatch.setattr(doctor, "_store_path",
                            lambda explicit=None: path)
        capsys.readouterr()
        main(["--db", str(path), "doctor"])

        # reopen and check the cache entry survived
        from voyager.store import Store
        s2 = Store(path)
        try:
            after = s2.meta_get(cache_key)
        finally:
            s2.close()
        assert before == after, "plain doctor must be read-only"


# --- O4.11: dashboard integration -------------------------------------------

class TestDashboardO4:
    def test_dashboard_has_lease_and_pending(self, tmp_path, monkeypatch):
        from voyager.dashboard import build
        store, path = _make_store(tmp_path)
        try:
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            data = build(store)
            health = data.get("health") or {}
            assert "leases" in health
            assert "pending" in health
            assert "warnings" in health
        finally:
            store.close()

    def test_dashboard_reuses_doctor_model(self, tmp_path, monkeypatch):
        """The dashboard does not judge health itself -- it uses doctor's."""
        from voyager.dashboard import build, render_html
        store, path = _make_store(tmp_path)
        try:
            tid = store.thread_create(repo_root="/r", title="t", goal="g")
            sid = _seed_session(store, source_path=tmp_path / "s.jsonl")
            store.thread_attach(tid, sid)
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            data = build(store)
            html_out = render_html(data)
            # the page should contain the health summary
            assert "sessions" in html_out or "threads" in html_out
        finally:
            store.close()


# --- O4.2: run() backward compatibility -------------------------------------

class TestRunBackwardCompat:
    def test_run_still_has_legacy_keys(self, tmp_path, monkeypatch):
        """The report must still carry the keys existing consumers expect."""
        store, path = _make_store(tmp_path)
        try:
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            report = doctor.run(db_path=path)
            for key in ("store", "continuity", "cache", "providers",
                        "issues", "blocking", "external", "non_blocking",
                        "warnings", "leases", "pending", "verification"):
                assert key in report, key
        finally:
            store.close()

    def test_run_issues_have_legacy_kind(self, tmp_path, monkeypatch):
        """Each issue dict must carry the legacy ``kind`` / ``id`` / ``detail``
        keys for backward compatibility."""
        store, path = _make_store(tmp_path)
        try:
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            report = doctor.run(db_path=path)
            for issue in report["issues"]:
                assert "kind" in issue
                assert "id" in issue
                assert "detail" in issue
                assert "code" in issue  # canonical
                assert "severity" in issue
                assert "repair_kind" in issue
        finally:
            store.close()

    def test_run_json_serializable(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        try:
            monkeypatch.setattr(doctor, "_store_path",
                                lambda explicit=None: path)
            report = doctor.run(db_path=path)
            json.dumps(report, ensure_ascii=False, default=str)
        finally:
            store.close()
# --- O4 patch: FTS / archive / ambiguity / allowlist / exit codes -----------

def _digest_db(path: Path) -> str:
    """Digest of the canonical DB file (main file only: opening a WAL
    database rewrites -wal bookkeeping bytes even for pure readers, so the
    -wal file cannot serve as a read-only witness)."""
    import hashlib
    return hashlib.sha256(Path(str(path)).read_bytes()).hexdigest()


def _settle(path: Path) -> None:
    """One throwaway open/close so WAL bookkeeping stabilises before the
    first digest."""
    from voyager.store import Store
    s = Store(path)
    s.close()


class TestArchiveAndAmbiguityDiagnostics:

    def test_archived_sessions_are_informational(self, tmp_path, monkeypatch):
        """Test 3: ARCHIVED_CANONICAL is a normal state — info, read-only,
        never blocking."""
        store, path = _make_store(tmp_path)
        _seed_session(store, "codex:arch1")
        store.mark_canonical_archived("codex:arch1")
        store.close()
        monkeypatch.setattr(doctor, "_store_path", lambda db_path=None: path)
        issues = collect_issues(db_path=path)
        arch = [i for i in issues if i.code == "SOURCE_ARCHIVED"]
        assert len(arch) == 1
        assert arch[0].severity == INFO
        assert arch[0].repair_kind == READ_ONLY_DIAGNOSIS
        assert arch[0].auto_fixable is False
        assert _legacy_kind(arch[0].severity,
                            arch[0].repair_kind) == "non-blocking"

    def test_ambiguous_pending_is_user_decision(self, tmp_path, monkeypatch):
        """Test 4: an ambiguous pending is a warning the user must resolve —
        never auto-fixed, never picked by the doctor."""
        store, path = _make_store(tmp_path)
        tid = store.thread_create(repo_root="/repo", title="amb")
        store.pending_record(tid, "codex", source_provider="claude",
                             source_session="claude:c1", note="switch")
        rid = store.q("SELECT rowid rid FROM thread_pending"
                      " WHERE thread_id=?", (tid,))[0]["rid"]
        store.pending_mark(rid, "ambiguous")
        store.close()
        monkeypatch.setattr(doctor, "_store_path", lambda db_path=None: path)
        issues = collect_issues(db_path=path)
        amb = [i for i in issues if i.code == "PENDING_ATTACH_AMBIGUOUS"]
        assert len(amb) == 1
        assert amb[0].severity == WARNING
        assert amb[0].repair_kind == USER_DECISION_REQUIRED
        assert amb[0].auto_fixable is False

    def test_stale_pending_is_warning_never_autofixed(self, tmp_path,
                                                      monkeypatch):
        """Test 5: stale pending severity follows the O4 spec (warning) and
        stays out of the fixer's reach."""
        store, path = _make_store(tmp_path)
        tid = store.thread_create(repo_root="/repo", title="st")
        store.pending_record(tid, "codex", note="slow start")
        store.con.execute("UPDATE thread_pending SET created_at=?"
                          " WHERE thread_id=?", (time.time() - 3600, tid))
        store.con.commit()
        store.close()
        monkeypatch.setattr(doctor, "_store_path", lambda db_path=None: path)
        issues = collect_issues(db_path=path)
        stale = [i for i in issues if i.code == "PENDING_STALE"]
        assert len(stale) == 1
        assert stale[0].severity == WARNING
        assert stale[0].repair_kind == USER_DECISION_REQUIRED


class TestFtsDiagnosticsAndRepair:

    def _make_inconsistent(self, tmp_path, monkeypatch):
        store, path = _make_store(tmp_path)
        _seed_session(store, "codex:fts1")
        store.con.execute(
            "INSERT INTO events(sid, ts, seq, kind, content)"
            " VALUES ('codex:fts1', 1.0, 1, 'user', 'needle-fts-corrupt')")
        store.con.commit()
        # half the FTS index vanishes: the canonical rows are untouched
        store.con.execute("DELETE FROM event_fts WHERE rowid > 0")
        store.con.commit()
        store.close()
        _settle(path)     # let the additive migration land before digesting
        monkeypatch.setattr(doctor, "_store_path", lambda db_path=None: path)
        return path

    def test_fts_mismatch_is_reported(self, tmp_path, monkeypatch):
        """Test 8: a broken FTS index is detected, as safe derived repair."""
        path = self._make_inconsistent(tmp_path, monkeypatch)
        issues = collect_issues(db_path=path)
        fts = [i for i in issues if i.code == "FTS_INCONSISTENT"]
        assert len(fts) == 1
        assert fts[0].severity == WARNING
        assert fts[0].repair_kind == SAFE_DERIVED_REPAIR
        assert fts[0].auto_fixable is True

    def test_fts_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        """Test 9: the dry-run plan names operation/target/why-safe and the
        database is byte-identical afterwards."""
        path = self._make_inconsistent(tmp_path, monkeypatch)
        before = _digest_db(path)
        result = apply_fix(db_path=path, dry_run=True)
        assert result["dry_run"] is True
        plan = [e for e in result["executed"] if e["code"] == "FTS_INCONSISTENT"]
        assert plan, "the plan must include the FTS rebuild"
        assert plan[0]["operation"] == "_fix_fts_inconsistent"
        assert plan[0]["target"] == str(path)
        assert "derived" in plan[0]["why_safe"]
        assert _digest_db(path) == before, "dry-run must not write"

    def test_fts_fix_rebuilds_and_search_works(self, tmp_path, monkeypatch):
        """Test 10: after the fix the index matches and search finds rows."""
        from voyager.store import Store
        path = self._make_inconsistent(tmp_path, monkeypatch)
        result = apply_fix(db_path=path)
        assert [e["code"] for e in result["executed"]] == ["FTS_INCONSISTENT"]
        assert not result["failed"]
        s = Store(path)
        try:
            assert s.q("SELECT COUNT(*) n FROM events")[0]["n"] == s.q(
                "SELECT COUNT(*) n FROM event_fts")[0]["n"]
            assert s.search("needle-fts-corrupt"), "search works again"
        finally:
            s.close()

    def test_fts_fix_is_idempotent(self, tmp_path, monkeypatch):
        """Test 11: the second --fix changes nothing."""
        path = self._make_inconsistent(tmp_path, monkeypatch)
        apply_fix(db_path=path)
        before = _digest_db(path)
        result = apply_fix(db_path=path)
        assert [e["code"] for e in result["executed"]
                if e["code"] == "FTS_INCONSISTENT"] == []
        assert _digest_db(path) == before

    def test_fts_fix_rolls_back_on_failure(self, tmp_path, monkeypatch):
        """Test 12: a mid-repair failure leaves the database in its pre-fix
        state — the index is not half-rebuilt, the canonical rows untouched."""
        from voyager.store import Store
        path = self._make_inconsistent(tmp_path, monkeypatch)
        s = Store(path)
        try:
            before_fts = s.q("SELECT COUNT(*) n FROM event_fts")[0]["n"]
            before_events = s.q("SELECT COUNT(*) n FROM events")[0]["n"]
        finally:
            s.close()

        def _boom(store, db_path):
            raise RuntimeError("boom")

        monkeypatch.setitem(doctor.SAFE_FIXERS, "FTS_INCONSISTENT", _boom)
        result = apply_fix(db_path=path)
        assert result["failed"] and result["failed"][0]["code"] ==             "FTS_INCONSISTENT"
        assert not result["executed"]
        s = Store(path)
        try:
            assert s.q("SELECT COUNT(*) n FROM events")[0]["n"] == before_events
            assert s.q("SELECT COUNT(*) n FROM event_fts")[0]["n"] == before_fts
        finally:
            s.close()


class TestSafeFixBoundary:

    def test_safe_fixers_allowlist_is_explicit_and_minimal(self):
        """Test (§26): the allowlist exists, is auditable, and contains only
        derived-state repairs."""
        assert set(doctor.SAFE_FIXERS) == {
            "CACHE_STALE", "FTS_INCONSISTENT"}

    def test_a_code_off_the_allowlist_is_never_executed(self, tmp_path,
                                                        monkeypatch):
        """An issue that claims SAFE_DERIVED_REPAIR but has no allowlist
        entry is skipped, never dynamically dispatched."""
        path = _make_store(tmp_path)[1]
        monkeypatch.setattr(doctor, "_store_path", lambda db_path=None: path)
        rogue = doctor.Issue(
            code="ROGUE_REPAIR", severity=WARNING, category="cache",
            message="claims to be safe", evidence="-",
            suggested_action="-", auto_fixable=True,
            repair_kind=SAFE_DERIVED_REPAIR)
        monkeypatch.setattr(doctor, "collect_issues",
                            lambda db_path=None: [rogue])
        result = apply_fix(db_path=path)
        assert result["executed"] == []
        assert "ROGUE_REPAIR" in result["skipped"]
        assert result["failed"] == []

    def test_fix_report_has_the_four_way_split(self, tmp_path, monkeypatch):
        """Test (§27): attempted / fixed(executed) / skipped / failed, by
        code."""
        path = _make_store(tmp_path)[1]
        monkeypatch.setattr(doctor, "_store_path", lambda db_path=None: path)
        result = apply_fix(db_path=path)
        for key in ("attempted", "executed", "skipped", "failed"):
            assert key in result
        assert isinstance(result["attempted"], list)
        assert isinstance(result["skipped"], list)


class TestExitCodesAndReadOnly:

    def test_exit_zero_with_only_warnings(self, tmp_path, monkeypatch, capsys):
        """Test 19: warning/info issues must not fail the CLI."""
        import sys as _sys
        from voyager.cli import main
        store, path = _make_store(tmp_path)
        _seed_session(store, "codex:ex1")
        store.mark_canonical_archived("codex:ex1")   # INFO only
        store.close()
        rc = main(["--db", str(path), "doctor", "--json"])
        assert rc == 0

    def test_doctor_json_and_verbose_are_read_only(self, tmp_path,
                                                   monkeypatch, capsys):
        """Test 1: doctor, doctor --json and a second run leave the DB
        byte-identical (digest regression)."""
        from voyager.cli import main
        store, path = _make_store(tmp_path)
        _seed_session(store, "codex:ro1")
        tid = store.thread_create(repo_root="/repo", title="ro")
        store.thread_attach(tid, "codex:ro1")
        store.close()
        monkeypatch.setattr(doctor, "_store_path", lambda db_path=None: path)
        _settle(path)
        before = _digest_db(path)
        assert main(["--db", str(path), "doctor"]) == 0
        assert main(["--db", str(path), "doctor", "--json"]) == 0
        assert main(["--db", str(path), "doctor"]) == 0
        assert _digest_db(path) == before

    def test_doctor_never_creates_evidence(self, tmp_path, monkeypatch):
        """Test (§30): doctor must not write verification events."""
        from voyager.store import Store
        store, path = _make_store(tmp_path)
        store.con.execute(
            "CREATE TABLE IF NOT EXISTS verification_events("
            "id INTEGER PRIMARY KEY, provider TEXT, correlation_id TEXT,"
            " kind TEXT, ts REAL, session_id TEXT, detail_json TEXT)")
        store.con.commit()
        before = store.q("SELECT COUNT(*) n FROM verification_events")[0]["n"]
        store.close()
        monkeypatch.setattr(doctor, "_store_path", lambda db_path=None: path)
        collect_issues(db_path=path)
        s = Store(path)
        try:
            after = s.q("SELECT COUNT(*) n FROM verification_events")[0]["n"]
        finally:
            s.close()
        assert before == after == 0, "doctor must never fabricate evidence"
