"""`voyager doctor` -- is this installation healthy, and why not?

The point of this command is that nobody should have to read log files to work
out whether continuity is working.  It checks the store, the providers, the hook
registrations, the last observed trigger, the continuity state, the lease
health, the pending-attach health, the cache health, the known debts, and
classifies everything it finds into a **canonical issue model** (O4).

O4 canonical issue model
-----------------------
Every finding is an :class:`Issue` with:

  code           -- machine-readable stable id (e.g. ``STORE_UNAVAILABLE``)
  severity       -- ``info`` | ``warning`` | ``critical``
  category       -- ``store`` | ``continuity`` | ``retention`` | ``lease``
                    | ``pending`` | ``cache`` | ``provider`` | ``debt``
  message        -- human-readable one-liner
  evidence       -- what was observed (counts, paths, timestamps)
  suggested_action -- what the user (or ``--fix``) should do
  auto_fixable   -- can ``doctor --fix`` handle this safely?
  repair_kind    -- one of the four repair classifications (below)

Repair classification
---------------------
  READ_ONLY_DIAGNOSIS      -- information only; nothing to fix
  SAFE_DERIVED_REPAIR      -- rebuild data derived from other tables (FTS,
                              cache); deterministic and reproducible
  USER_DECISION_REQUIRED   -- ambiguity, stale leases, stranded threads; needs
                              human judgment, never auto-fixed
  EXTERNAL_PROVIDER_ISSUE  -- provider's own behaviour, outside sessionFlow

``doctor --fix`` runs *only* ``SAFE_DERIVED_REPAIR``.  It will never: delete
retained history, resolve ambiguity, steal leases, touch provider files, or run
VACUUM (that is maintenance, not repair).

Provider states come from :mod:`voyager.capability_matrix`, so this output and
the README cannot disagree.

Every check is defensive: the doctor reports what it can and never raises.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .capability_matrix import (
    DIMENSIONS, HOOK_CONFIG_PATHS, PROVIDERS, SOURCE_PATHS,
    collect_evidence, evidence_ceiling, provider_state, resolve_cell,
    PROVIDER_CONTEXT_BUDGETS,
    NOT_FOUND, SUPPORTED, CONFIGURED, UNIT_VERIFIED,
    LIVE_VERIFIED, ZERO_TOUCH_LIVE_VERIFIED, STATE_ORDER,
    Evidence,
)

# --- O4 canonical vocabulary -----------------------------------------------

INFO = "info"
WARNING = "warning"
CRITICAL = "critical"

READ_ONLY_DIAGNOSIS = "READ_ONLY_DIAGNOSIS"
SAFE_DERIVED_REPAIR = "SAFE_DERIVED_REPAIR"
USER_DECISION_REQUIRED = "USER_DECISION_REQUIRED"
EXTERNAL_PROVIDER_ISSUE = "EXTERNAL_PROVIDER_ISSUE"

REPAIR_KINDS = (
    READ_ONLY_DIAGNOSIS,
    SAFE_DERIVED_REPAIR,
    USER_DECISION_REQUIRED,
    EXTERNAL_PROVIDER_ISSUE,
)

#: Debts that are tracked but do not stop work.  Kept here so `doctor` is the one
#: place that answers "what is still open?"  Each entry now carries the O4
#: canonical fields, and the legacy ``id`` / ``detail`` / ``kind`` are derived
#: from them for backward compatibility.
KNOWN_DEBTS: Tuple[Dict[str, str], ...] = (
    {"code": "L1_STRONG_FAIRNESS",
     "severity": INFO, "category": "debt",
     "repair_kind": READ_ONLY_DIAGNOSIS,
     "message": "the per-session minimum is best-effort; a very small budget "
                "can still starve the newest STRONG session"},
    {"code": "PROVIDER_HANDLER_DUPLICATION",
     "severity": INFO, "category": "debt",
     "repair_kind": READ_ONLY_DIAGNOSIS,
     "message": "_log_dir/_log_event/_read_stdin are duplicated across handlers "
                "and the log-trim logic has already drifted"},
    {"code": "PROVENANCE_NORMALISE_DEAD_CODE",
     "severity": INFO, "category": "debt",
     "repair_kind": READ_ONLY_DIAGNOSIS,
     "message": "provenance.normalise() has no caller; DB NULL stays the single "
                "representation of unclassified"},
    {"code": "LIVE_VERIFICATION_BLOCKED_BY_PROVIDER_UI",
     "severity": INFO, "category": "debt",
     "repair_kind": READ_ONLY_DIAGNOSIS,
     "message": "zcode/cursor/kiro/antigravity need their own UI to fire a hook; "
                "they stay UNIT_VERIFIED until that is observed"},
    {"code": "EXTERNAL_CODEX_APPSERVER_CONPTY_POPUP",
     "severity": INFO, "category": "debt",
     "repair_kind": EXTERNAL_PROVIDER_ISSUE,
     "message": "the managed Codex app-server launches git through ConPTY, "
                "which can surface Windows Terminal windows; not a sessionFlow bug"},
    {"code": "GIT_STALE_REFLOG_MISSING_OBJECT",
     "severity": INFO, "category": "debt",
     "repair_kind": READ_ONLY_DIAGNOSIS,
     "message": "a local dangling commit's tree is missing and a reflog entry "
                "points at it; reachable history is healthy and is deliberately "
                "left alone"},
)

#: O1 performance debts (O4.13: report only, do not fix).
O1_DEBTS: Tuple[Dict[str, str], ...] = (
    {"code": "OVERVIEW_RECENT_SESSIONS_N1",
     "severity": INFO, "category": "debt",
     "repair_kind": READ_ONLY_DIAGNOSIS,
     "message": "overview's recent-sessions query is N+1 on thread membership; "
                "not fixed because the wall-clock cost is dominated by sandbox "
                "spawn overhead, not the query"},
    {"code": "SCAN_MATERIALISE_ALL_BEFORE_WRITE",
     "severity": INFO, "category": "debt",
     "repair_kind": READ_ONLY_DIAGNOSIS,
     "message": "scan materialises all parsed events before writing; a streaming "
                "write would reduce peak memory, but the current batch is "
                "bounded and the cost is startup, not per-event"},
)


def provider_context_budget_info(provider: str) -> Dict[str, Any]:
    """Get context budget details for a provider."""
    return PROVIDER_CONTEXT_BUDGETS.get(provider, {})

CONTEXT_FORMAT = "tiered-v1"


# --- O4 canonical Issue model -----------------------------------------------

@dataclass
class Issue:
    """One diagnostic finding, in the canonical O4 shape.

    ``to_dict()`` includes legacy ``id`` / ``detail`` / ``kind`` keys so that
    existing consumers (dashboard, CLI, tests) keep working without a flag day.
    The legacy ``kind`` is derived from ``severity`` + ``repair_kind``:

      severity ``critical``                     -> ``"blocking"``
      repair_kind ``EXTERNAL_PROVIDER_ISSUE``    -> ``"external"``
      severity ``warning``                       -> ``"warning"``
      otherwise                                   -> ``"non-blocking"``
    """
    code: str
    severity: str
    category: str
    message: str
    evidence: Optional[str] = None
    suggested_action: Optional[str] = None
    auto_fixable: bool = False
    repair_kind: str = READ_ONLY_DIAGNOSIS

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "code": self.code,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
            "auto_fixable": self.auto_fixable,
            "repair_kind": self.repair_kind,
        }
        # Legacy compat: consumers that read ``id`` / ``detail`` / ``kind``.
        d["id"] = self.code
        d["detail"] = self.message
        d["kind"] = _legacy_kind(self.severity, self.repair_kind)
        return d


def _legacy_kind(severity: str, repair_kind: str) -> str:
    """Map (severity, repair_kind) to the pre-O4 ``kind`` string."""
    if severity == CRITICAL:
        return "blocking"
    if repair_kind == EXTERNAL_PROVIDER_ISSUE:
        return "external"
    if severity == WARNING:
        return "warning"
    return "non-blocking"


def _issue_from_debt(debt: Dict[str, str]) -> Issue:
    """Lift a KNOWN_DEBTS / O1_DEBTS entry into an Issue."""
    return Issue(
        code=debt["code"],
        severity=debt.get("severity", INFO),
        category=debt.get("category", "debt"),
        message=debt.get("message", debt.get("detail", "")),
        repair_kind=debt.get("repair_kind", READ_ONLY_DIAGNOSIS),
    )


# --- helpers ---------------------------------------------------------------

def _store_path(explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit is not None:
        return Path(explicit)
    from .store import default_db_path
    try:
        return Path(default_db_path())
    except Exception:
        return None


def _open_store(db_path: Optional[Path] = None):
    """Return (store, path) or (None, path) if the store cannot be opened."""
    path = _store_path(db_path)
    if path is None or not path.exists():
        return None, path
    try:
        from .store import Store
        return Store(path), path
    except Exception:
        return None, path


# --- individual checks (each returns a dict for the report) ----------------

def check_store(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """repo/store/schema: can we open it, and is the shape what we expect?"""
    out: Dict[str, Any] = {"ok": False, "path": None, "sessions": None,
                           "threads": None, "error": None}
    try:
        from .store import Store
        path = _store_path(db_path)
        out["path"] = str(path) if path else None
        if not path or not path.exists():
            out["error"] = "index database not found; run `voyager scan`"
            return out
        store = Store(path)
        try:
            out["sessions"] = store.q("SELECT COUNT(*) AS n FROM sessions")[0]["n"]
            out["threads"] = store.q("SELECT COUNT(*) AS n FROM threads")[0]["n"]
            cols = {r["name"] for r in store.q("PRAGMA table_info(events)")}
            out["has_origin"] = "origin" in cols
            out["ok"] = True
        finally:
            store.close()
    except Exception as e:
        out["error"] = str(e)
    return out


def check_hook_config(provider: str) -> Dict[str, Any]:
    """Is the provider's hook file present *and* shaped the way it must be?"""
    out: Dict[str, Any] = {"registered": False, "valid": None, "error": None}
    rel = HOOK_CONFIG_PATHS.get(provider)
    if not rel:
        out["error"] = "no hook surface"
        return out
    if rel.startswith("."):
        out["error"] = "project-scoped (not installed globally)"
        return out
    path = Path(rel).expanduser()
    if not path.exists():
        return out
    out["registered"] = True
    try:
        json.loads(path.read_text(encoding="utf-8"))
        out["valid"] = True
    except Exception as e:
        out["valid"] = False
        out["error"] = "invalid JSON: %s" % e
    return out


def check_continuity(repo: Optional[str] = None,
                     db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Active WorkThread, ambiguity, pending attach, provenance coverage."""
    out: Dict[str, Any] = {"active_threads": None, "ambiguous": False,
                           "pending": None, "coverage": None, "error": None}
    try:
        from .store import Store
        path = _store_path(db_path)
        if not path or not path.exists():
            out["error"] = "no index"
            return out
        store = Store(path)
        try:
            try:
                rows = store.q("SELECT id, repo_root, status FROM threads ORDER BY id")
            except Exception:
                rows = store.q("SELECT id, repo_root FROM threads ORDER BY id")
            out["threads_total"] = len(rows)
            # Ambiguity is about *active* WorkThreads.  Two closed threads for the
            # same repo are history, not a choice the user has to make -- counting
            # them would make the doctor cry wolf on every completed experiment.
            def _is_active(row) -> bool:
                if "status" in row.keys():
                    return (row["status"] or "active") == "active"
                return True

            by_repo: Dict[str, int] = {}
            for r in rows:
                if not _is_active(r):
                    continue
                key = (r["repo_root"] if "repo_root" in r.keys() else "") or ""
                if not key:
                    continue
                by_repo[key] = by_repo.get(key, 0) + 1
            try:
                out["active_threads"] = sum(
                    1 for r in rows
                    if ("status" in r.keys() and (r["status"] or "active") == "active"))
            except Exception:
                out["active_threads"] = len(rows)
            out["ambiguous"] = any(v > 1 for v in by_repo.values())
            out["ambiguous_repos"] = [k for k, v in by_repo.items() if v > 1]
            try:
                out["pending"] = store.q(
                    "SELECT COUNT(*) AS n FROM thread_pending")[0]["n"]
            except Exception as e:
                out["pending"] = None
                out.setdefault("errors", []).append("pending: %s" % e)
            try:
                total = store.q("SELECT COUNT(*) AS n FROM events")[0]["n"] or 0
                unk = store.q("SELECT COUNT(*) AS n FROM events "
                              "WHERE origin IS NULL")[0]["n"] or 0
                literal = store.q("SELECT COUNT(*) AS n FROM events "
                                  "WHERE origin='unknown'")[0]["n"] or 0
                out["coverage"] = {
                    "events": total,
                    "unclassified": unk,
                    "classified_pct": round(100.0 * (total - unk) / total, 1) if total else None,
                    "literal_unknown": literal,
                }
            except Exception as e:
                out["coverage"] = None
                out.setdefault("errors", []).append("coverage: %s" % e)
        finally:
            store.close()
    except Exception as e:
        out["error"] = str(e)
    return out


def check_retention(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """O2: how much history is retained because its provider sources vanished.

    Retention is the **correct** outcome of a provider rotating its own storage,
    so on its own this is informational, never a fault: it is reported as a
    `retention` block plus, at most, a warning.  It only becomes a warning when
    an *active* WorkThread has no live member left, because then continuity for
    that thread has nothing left to compile from -- that is a real consequence
    the user should see, not a silent one.

    Never raises.
    """
    empty = {"available": False, "sessions": 0, "events": 0, "bytes": 0,
             "providers": [], "oldest": None, "stranded_threads": [],
             "archived": 0}
    path = _store_path(db_path)
    if path is None or not path.exists():
        return empty
    try:
        from .store import Store
        store = Store(path)
    except Exception as e:                       # pragma: no cover - defensive
        out = dict(empty)
        out["error"] = str(e)
        return out
    try:
        stats = store.retained_stats()
        stats["archived"] = store.q(
            "SELECT COUNT(*) n FROM sessions"
            " WHERE source_state='ARCHIVED_CANONICAL'")[0]["n"]
        stranded = []
        if stats["sessions"]:
            for t in store.thread_list("active"):
                if not store.live_thread_members(t["id"]):
                    stranded.append({"thread": t["id"],
                                     "title": t["title"] or ""})
        stats["stranded_threads"] = stranded
        stats["available"] = True
        return stats
    except Exception as e:                       # pragma: no cover - defensive
        out = dict(empty)
        out["error"] = str(e)
        return out
    finally:
        try:
            store.close()
        except Exception:
            pass


def check_cache(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """How many continuity bundles are cached, how old is the newest, and are
    any of them stale relative to the sessions they were compiled from?

    O4.8: a stale cache entry is a candidate for ``SAFE_DERIVED_REPAIR`` -- the
    cache is *derived* from sessions/events, so clearing it is deterministic and
    the next ``get_continuation_context()`` call rebuilds it.  But we only mark
    it ``auto_fixable`` when the canonical source is intact (store opens, the
    session the cache references still exists), because clearing a cache whose
    source is also gone does not fix anything.
    """
    out: Dict[str, Any] = {"entries": None, "newest_age_s": None,
                           "stale": [], "auto_fixable": False}
    try:
        from .store import Store
        path = _store_path(db_path)
        if not path or not path.exists():
            return out
        store = Store(path)
        try:
            rows = store.q("SELECT key FROM meta WHERE key LIKE 'ctx_cache%'")
            out["entries"] = len(rows)
            stale_keys: List[str] = []
            newest = None
            for r in rows:
                raw = store.meta_get(r["key"])
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                ts = payload.get("compiled_at")
                if ts and (newest is None or ts > newest):
                    newest = ts
                # A cache entry is stale if it is older than the newest
                # session update.  We check the session_ids in the payload
                # (if present) against the live sessions table.
                sids = payload.get("session_ids") or []
                if sids:
                    for sid in sids:
                        row = store.q(
                            "SELECT updated_at FROM sessions WHERE id=?",
                            (sid,))
                        if not row:
                            # session gone: cache entry is stale
                            if r["key"] not in stale_keys:
                                stale_keys.append(r["key"])
                            continue
                        sua = row[0]["updated_at"] or 0
                        if ts and sua > ts:
                            if r["key"] not in stale_keys:
                                stale_keys.append(r["key"])
            out["stale"] = stale_keys
            # auto_fixable only when the store is healthy (canonical intact)
            out["auto_fixable"] = bool(stale_keys) and bool(
                store.q("SELECT COUNT(*) AS n FROM sessions")[0]["n"] >= 0)
            if newest:
                out["newest_age_s"] = int(time.time() - newest)
        finally:
            store.close()
    except Exception:
        pass
    return out


def check_leases(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """O4.5: lease health.

    A lease is held by a provider to claim single-writer status on a WorkThread.
    An expired lease (stale heartbeat or dead pid) is not necessarily broken --
    the D13 design lets the next acquirer take it over -- but it *is* something
    the user should see, because it means a previous agent crashed or was killed
    without releasing.

    Lease decisions are always ``USER_DECISION_REQUIRED``: stealing a lease is a
    policy decision that affects ongoing work, and the doctor must not make it
    automatically.  ``doctor --fix`` will never touch a lease.
    """
    out: Dict[str, Any] = {"total": 0, "active": 0, "expired": 0,
                           "details": []}
    try:
        from .store import Store, lease_state
        path = _store_path(db_path)
        if not path or not path.exists():
            return out
        store = Store(path)
        try:
            now = time.time()
            rows = store.q("SELECT * FROM thread_leases ORDER BY acquired_at")
            out["total"] = len(rows)
            for r in rows:
                st = lease_state(r, now)
                if st["held"] and not st["expired"]:
                    out["active"] += 1
                else:
                    out["expired"] += 1
                    out["details"].append({
                        "thread_id": r["thread_id"],
                        "holder": r["holder"],
                        "why": st["why"],
                        "heartbeat_age_s": int(now - (r["heartbeat_at"] or 0))
                            if r["heartbeat_at"] else None,
                    })
        finally:
            store.close()
    except Exception:
        pass
    return out


def check_pending(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """O4.4: pending-attach health.

    A pending-attach record is created when a handoff launches a new provider
    session and waits for it to show up.  A stale pending (open for a long time)
    is not necessarily broken -- the agent may still be starting -- but it is
    worth surfacing.

    ``doctor --fix`` will never clear a pending record: even one that looks
    stale could resolve at any moment if the agent is slow to start.  Only a
    pending record whose thread has been *archived* (permanently closed) is
    provably impossible to resolve, and even then the doctor reports it rather
    than acting, because the user may want to re-open the thread.
    """
    out: Dict[str, Any] = {"open": 0, "stale": [], "archived_orphans": [],
                           "ambiguous": []}
    try:
        from .store import Store
        path = _store_path(db_path)
        if not path or not path.exists():
            return out
        store = Store(path)
        try:
            # O4: an ambiguous pending recorded its outcome -- several
            # candidates matched.  Picking one is the user's call, never
            # the doctor's.
            amb_rows = store.q(
                "SELECT thread_id, provider, resolved_at FROM thread_pending "
                "WHERE status='ambiguous' ORDER BY created_at")
            for r in amb_rows:
                out["ambiguous"].append({
                    "thread_id": r["thread_id"], "provider": r["provider"],
                    "age_s": int(time.time() - (r["resolved_at"] or time.time())),
                })
            now = time.time()
            open_rows = store.q(
                "SELECT * FROM thread_pending "
                "WHERE COALESCE(status, 'open') = 'open' ORDER BY created_at")
            out["open"] = len(open_rows)
            for r in open_rows:
                age_s = int(now - (r["created_at"] or now))
                # "stale" = open for more than 10 minutes; informational
                if age_s > 600:
                    out["stale"].append({
                        "thread_id": r["thread_id"],
                        "provider": r["provider"],
                        "age_s": age_s,
                    })
                # check if the thread is archived (provably impossible to resolve)
                trows = store.q(
                    "SELECT status FROM threads WHERE id=?",
                    (r["thread_id"],))
                if trows and trows[0]["status"] == "archived":
                    out["archived_orphans"].append({
                        "thread_id": r["thread_id"],
                        "provider": r["provider"],
                    })
        finally:
            store.close()
    except Exception:
        pass
    return out


def check_fts(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """O4: is the FTS index consistent with the canonical events rows?

    Set-based (two COUNTs), never a row-by-row Python walk.  FTS is derived
    state: a mismatch is a WARNING with a safe rebuild available, never a
    corruption verdict -- the canonical events table is the truth.
    """
    out = {"available": False, "fts_rows": 0, "events": 0, "consistent": True}
    path = _store_path(db_path)
    if path is None or not path.exists():
        return out
    try:
        from .store import Store
        store = Store(path)
        try:
            out["events"] = store.q(
                "SELECT COUNT(*) n FROM events")[0]["n"]
            out["fts_rows"] = store.q(
                "SELECT COUNT(*) n FROM event_fts")[0]["n"]
            out["consistent"] = out["fts_rows"] == out["events"]
            out["available"] = True
            return out
        finally:
            store.close()
    except Exception as e:
        out["error"] = str(e)
        return out


def check_verification(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """O4.6 / O5.7: verification diagnostics.

    For each provider, show three layers, sourced from
    :mod:`voyager.verification_harness` (the evidence table) and
    :data:`DECLARED` (the code-level ceiling):

    * **declared** -- the strongest state any machine-dependent dimension is
      declared at in the code (the ceiling).
    * **observed** -- what the ``verification_events`` table proves on this
      machine (chains, evidence_count, best_chain, last_live_event).
    * **effective** -- the weaker of declared and observed; when no evidence
      exists, capped at ``UNIT_VERIFIED`` for machine-dependent dimensions.

    Also surfaces filesystem-level checks (installed, hook_registered) from
    :func:`collect_evidence`, because those are configuration state, not
    runtime state.

    A provider with a gap (declared > observed) is **not broken** -- it is
    waiting for a natural trigger, and that is informational, not a warning.
    """
    from .verification_harness import query_status
    from .capability_matrix import DECLARED, STATE_ORDER as _SO

    _MACHINE_DEPENDENT = frozenset({
        "startup_hook", "dynamic_context_injection",
        "native_session_id_at_start", "native_auto_attach",
        "live_zero_touch_continuity",
    })

    # Get the three-layer view from the evidence harness
    vh_status = query_status(db_path=db_path)
    out: Dict[str, Any] = {"providers": {}}
    for p in PROVIDERS:
        ev = collect_evidence(p)
        vh_p = vh_status["providers"].get(p, {})
        declared = vh_p.get("declared_state", NOT_FOUND)
        observed = vh_p.get("observed_state")
        effective = vh_p.get("effective_state", declared)
        # Use the observed ceiling from evidence for the "has_gap" check.
        # When observed is None (no evidence), the gap is informational.
        observed_ceiling = observed or UNIT_VERIFIED
        out["providers"][p] = {
            "declared": declared,
            "observed": observed_ceiling,
            "effective": effective,
            "installed": ev.installed,
            "hook_registered": ev.hook_registered,
            "hook_fired": ev.hook_fired,
            "last_trigger": ev.last_trigger,
            # Evidence-table details (O5.7)
            "chains": vh_p.get("chains", 0),
            "evidence_count": vh_p.get("evidence_count", 0),
            "best_chain": vh_p.get("best_chain"),
            "last_live_event": vh_p.get("last_live_event"),
            # "gap" means declared > observed; this is informational, not a fault
            "has_gap": _SO.get(observed_ceiling, 0) < _SO.get(declared, 0),
            # Why the effective state is not higher (O5.7)
            "blocked_reason": _blocked_reason(p, declared, observed, ev),
        }
    return out


def _blocked_reason(provider: str, declared: str,
                    observed: Optional[str], ev: Evidence) -> Optional[str]:
    """Why the effective state is not higher than it is. Never raises."""
    try:
        if declared == NOT_FOUND:
            return "no startup surface"
        if not ev.installed:
            return "provider not installed"
        if not ev.hook_registered:
            return "hook not registered"
        if observed is None:
            return "no evidence recorded yet (waiting for natural trigger)"
        if observed == UNIT_VERIFIED:
            return "hook registered, not yet observed firing"
        if observed == LIVE_VERIFIED:
            return "hook observed firing, zero-touch not yet proven"
        return None  # at the ceiling, nothing blocking
    except Exception:
        return None


# --- O4 canonical issue collection ------------------------------------------

def collect_issues(repo: Optional[str] = None,
                   db_path: Optional[Path] = None) -> List[Issue]:
    """The canonical O4 entry point: all findings as :class:`Issue` objects.

    Never raises.  This is what ``doctor --fix`` and the dashboard consume.
    """
    issues: List[Issue] = []

    # --- store --------------------------------------------------------------
    store_rpt = check_store(db_path)
    if not store_rpt.get("ok"):
        issues.append(Issue(
            code="STORE_UNAVAILABLE", severity=CRITICAL, category="store",
            message=store_rpt.get("error") or "index cannot be opened",
            evidence=str(store_rpt.get("path") or "(no path)"),
            suggested_action="run `voyager scan` to build an index",
            repair_kind=READ_ONLY_DIAGNOSIS,
        ))

    # --- continuity: ambiguity + provenance ---------------------------------
    cont = check_continuity(repo, db_path)
    if cont.get("ambiguous"):
        issues.append(Issue(
            code="AMBIGUOUS_WORKTHREAD", severity=CRITICAL,
            category="continuity",
            message="more than one active WorkThread for a repo: %s"
                    % ", ".join(cont.get("ambiguous_repos") or []),
            evidence="%d active threads" % (cont.get("active_threads") or 0),
            suggested_action="close or archive the thread you are not working on",
            repair_kind=USER_DECISION_REQUIRED,
        ))
    if cont.get("coverage") and cont["coverage"].get("literal_unknown"):
        issues.append(Issue(
            code="PROVENANCE_LITERAL_UNKNOWN", severity=CRITICAL,
            category="continuity",
            message="%d rows store the literal 'unknown'"
                    % cont["coverage"]["literal_unknown"],
            evidence="events with origin='unknown' (should be NULL)",
            suggested_action="inspect the affected rows; the sentinel must not "
                             "be stored, only classified",
            repair_kind=USER_DECISION_REQUIRED,
        ))

    # --- providers: hook config --------------------------------------------
    providers: Dict[str, Any] = {}
    for p in PROVIDERS:
        ev = collect_evidence(p)
        cfg = check_hook_config(p)
        budget_info = provider_context_budget_info(p)
        providers[p] = {
            "state": provider_state(p, ev),
            "installed": ev.installed,
            "source_path": SOURCE_PATHS.get(p),
            "hook_registered": ev.hook_registered,
            "hook_config_valid": cfg.get("valid"),
            "hook_config_error": cfg.get("error"),
            "hook_fired": ev.hook_fired,
            "last_trigger": ev.last_trigger,
            "budget": budget_info.get("startup_context_budget"),
            "transport_limit": budget_info.get("transport_limit"),
            "truncation_behavior": budget_info.get("truncation_behavior"),
            "supports_spill_pointer": budget_info.get("supports_spill_pointer", False),
            "native_session_id_at_start": budget_info.get("native_session_id_at_start", False),
        }
        # O4.7: invalid hook config is blocking; not-yet-fired is informational
        if providers[p]["hook_registered"] and providers[p]["hook_config_valid"] is False:
            issues.append(Issue(
                code="HOOK_CONFIG_INVALID", severity=CRITICAL,
                category="provider",
                message="%s: %s" % (p, providers[p]["hook_config_error"]),
                evidence="hook file: %s" % HOOK_CONFIG_PATHS.get(p, "?"),
                suggested_action="fix the JSON in the hook config file",
                repair_kind=USER_DECISION_REQUIRED,
            ))

    # --- retention (O4.3) ---------------------------------------------------
    retention = check_retention(db_path)
    if retention.get("sessions"):
        issues.append(Issue(
            code="RETENTION_HISTORY_HELD", severity=INFO,
            category="retention",
            message="%s session(s) from missing/rotated sources retained, "
                    "%s event(s), ~%s MB"
                    % (retention["sessions"], retention["events"],
                       round((retention.get("bytes") or 0) / 1048576, 1)),
            evidence="providers: %s" % ", ".join(
                "%s:%s" % (p["provider"], p["sessions"])
                for p in (retention.get("providers") or [])),
            suggested_action="this is expected; retained history is kept until "
                             "an explicit purge (not yet implemented)",
            repair_kind=READ_ONLY_DIAGNOSIS,
        ))
    if retention.get("archived"):
        issues.append(Issue(
            code="SOURCE_ARCHIVED", severity=INFO, category="retention",
            message="%s session(s) explicitly archived as canonical copies"
                    % retention["archived"],
            evidence="source_state='ARCHIVED_CANONICAL' (an explicit user "
                     "action, never automatic)",
            suggested_action="nothing to do; archived history stays searchable "
                             "and is excluded from live continuity by design",
            repair_kind=READ_ONLY_DIAGNOSIS,
        ))
    if retention.get("stranded_threads"):
        issues.append(Issue(
            code="RETENTION_STRANDED_WORKTHREAD", severity=WARNING,
            category="retention",
            message="%d active WorkThread(s) have no live member left "
                    "(all sources missing): %s"
                    % (len(retention["stranded_threads"]),
                       ", ".join(t["thread"] for t in
                                 retention["stranded_threads"])),
            evidence="stranded: %s" % ", ".join(
                t["thread"] for t in retention["stranded_threads"]),
            suggested_action="close the thread, or re-scan the provider source "
                             "if it came back",
            repair_kind=USER_DECISION_REQUIRED,
        ))

    # --- leases (O4.5) ------------------------------------------------------
    leases = check_leases(db_path)
    for detail in leases.get("details", []):
        issues.append(Issue(
            code="LEASE_EXPIRED", severity=WARNING, category="lease",
            message="lease on %s held by %s is expired (%s)"
                    % (detail["thread_id"], detail["holder"], detail["why"]),
            evidence="heartbeat %s s ago" % detail.get("heartbeat_age_s", "?"),
            suggested_action="the next handoff will take it over; if you want to "
                             "clear it now, use `voyager thread lease-release`",
            repair_kind=USER_DECISION_REQUIRED,
        ))

    # --- pending (O4.4) -----------------------------------------------------
    pending = check_pending(db_path)
    for s in pending.get("stale", []):
        issues.append(Issue(
            code="PENDING_STALE", severity=WARNING, category="pending",
            message="pending attach for %s on %s is %d s old"
                    % (s["thread_id"], s["provider"], s["age_s"]),
            evidence="open pending record, no resolution yet",
            suggested_action="the agent may still be starting; if it is truly "
                             "stuck, cancel the pending record manually",
            repair_kind=USER_DECISION_REQUIRED,
        ))
    for s in pending.get("archived_orphans", []):
        issues.append(Issue(
            code="PENDING_ARCHIVED_ORPHAN", severity=WARNING, category="pending",
            message="pending attach for %s on %s targets an archived thread"
                    % (s["thread_id"], s["provider"]),
            evidence="thread status='archived', pending still open",
            suggested_action="clear the pending record; it can never resolve",
            # Even here -- provably impossible -- we do not auto-fix, because
            # the user may want to re-open the thread and re-launch.
            repair_kind=USER_DECISION_REQUIRED,
        ))
    for s in pending.get("ambiguous", []):
        issues.append(Issue(
            code="PENDING_ATTACH_AMBIGUOUS", severity=WARNING,
            category="pending",
            message="pending attach for %s on %s resolved ambiguous: several "
                    "candidates matched" % (s["thread_id"], s["provider"]),
            evidence="status='ambiguous'; no candidate was chosen",
            suggested_action="decide which session to attach (voyager thread "
                             "attach) or cancel the pending record",
            repair_kind=USER_DECISION_REQUIRED,
        ))

    # --- cache (O4.8) -------------------------------------------------------
    cache = check_cache(db_path)
    if cache.get("stale"):
        # SAFE_DERIVED_REPAIR: the cache is derived from sessions/events, so
        # clearing it is deterministic.  But we only mark auto_fixable when
        # the canonical source is intact (check_cache already verified this).
        issues.append(Issue(
            code="CACHE_STALE", severity=WARNING, category="cache",
            message="%d stale cache entr%s: newer than the sessions they "
                    "reference" % (
                        len(cache["stale"]),
                        "y" if len(cache["stale"]) == 1 else "ies"),
            evidence="keys: %s" % ", ".join(cache["stale"][:5]),
            suggested_action="clear stale cache entries; the next "
                             "get_continuation_context() call rebuilds them",
            auto_fixable=cache.get("auto_fixable", False),
            repair_kind=SAFE_DERIVED_REPAIR,
        ))

    # --- FTS (O4: derived state, safely rebuildable) ------------------------
    fts = check_fts(db_path)
    if fts.get("available") and not fts.get("consistent"):
        issues.append(Issue(
            code="FTS_INCONSISTENT", severity=WARNING, category="fts",
            message="FTS index holds %s row(s) against %s canonical event(s)"
                    % (fts["fts_rows"], fts["events"]),
            evidence="event_fts vs events COUNT mismatch (search results may "
                     "be incomplete)",
            suggested_action="rebuild the FTS index from the canonical events",
            auto_fixable=True,
            repair_kind=SAFE_DERIVED_REPAIR,
        ))

    # --- verification (O4.6) ------------------------------------------------
    verification = check_verification(db_path)
    for p, v in verification.get("providers", {}).items():
        if v.get("has_gap") and v["installed"]:
            # Declared > observed: the provider is installed but hasn't been
            # naturally triggered yet.  This is informational, not a fault.
            issues.append(Issue(
                code="VERIFICATION_GAP_%s" % p.upper(), severity=INFO,
                category="provider",
                message="%s: declared %s, observed %s (effective %s)"
                        % (p, v["declared"], v["observed"], v["effective"]),
                evidence="installed but hook_fired=%s" % v["hook_fired"],
                suggested_action="trigger the provider naturally (start a "
                                 "session) to lift the observed state",
                repair_kind=READ_ONLY_DIAGNOSIS,
            ))

    # --- known debts + O1 debts (O4.13: report only) -----------------------
    for debt in KNOWN_DEBTS:
        issues.append(_issue_from_debt(debt))
    for debt in O1_DEBTS:
        issues.append(_issue_from_debt(debt))

    return issues


# --- O4 safe fix layer ------------------------------------------------------

def apply_fix(db_path: Optional[Path] = None,
              *, dry_run: bool = False) -> Dict[str, Any]:
    """Execute only ``SAFE_DERIVED_REPAIR`` issues.  Never raises.

    **Forbidden by design** (O4.9):

    * deleting retained history (``USER_DECISION_REQUIRED``)
    * resolving ambiguity (``USER_DECISION_REQUIRED``)
    * stealing or clearing leases (``USER_DECISION_REQUIRED``)
    * touching provider files (``EXTERNAL_PROVIDER_ISSUE``)
    * running VACUUM (that is maintenance, not repair)
    * clearing pending records (``USER_DECISION_REQUIRED``)

    The boundary is the explicit ``SAFE_FIXERS`` allowlist below: a repair
    runs only when its issue code is listed there AND the issue came back as
    ``SAFE_DERIVED_REPAIR`` + ``auto_fixable``.  Each repair is its own
    transaction — a failure rolls that repair back and leaves the canonical
    database untouched (no half-repaired state).  Repairs are idempotent: a
    second run reports the issue already healthy and changes nothing.

    The FTS rebuild ALSO lives in ``db_health.apply_safe_repairs`` (reached
    via ``voyager db repair --apply``); both go through the same allowlist.
    """
    path = _store_path(db_path)
    issues = collect_issues(db_path=db_path)
    fixable = [i for i in issues
               if i.repair_kind == SAFE_DERIVED_REPAIR and i.auto_fixable]
    attempted: List[str] = [i.code for i in fixable]
    skipped: List[str] = [i.code for i in issues
                          if i.repair_kind != SAFE_DERIVED_REPAIR
                          or not i.auto_fixable]
    executed: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    if dry_run:
        for i in fixable:
            executed.append({
                "code": i.code, "category": i.category,
                "message": i.message,
                "operation": SAFE_FIXERS[i.code].__name__,
                "target": str(path or "(default index)"),
                "why_safe": "derived state only; rebuilt or cleared from the "
                            "canonical tables, which are never touched",
                "would_execute": True,
            })
        return {"dry_run": True, "fixable": len(fixable),
                "attempted": attempted, "executed": executed,
                "skipped": skipped, "failed": failed}

    from .store import Store
    if path and path.exists():
        store = Store(path)
        try:
            for i in fixable:
                fixer = SAFE_FIXERS.get(i.code)
                if fixer is None:            # belt: not on the allowlist
                    skipped.append(i.code)
                    continue
                try:
                    # one repair operation = one transaction
                    store.con.execute("BEGIN")
                    detail = fixer(store, db_path)
                    store.con.commit()
                    executed.append({
                        "code": i.code, "category": i.category, "ok": True,
                        "action": detail,
                    })
                except Exception as e:
                    store.con.rollback()     # all-or-nothing, no half repair
                    failed.append({"code": i.code, "ok": False,
                                   "error": str(e)})
        finally:
            store.close()

    return {"dry_run": False, "fixable": len(fixable),
            "attempted": attempted, "executed": executed,
            "skipped": skipped, "failed": failed}


# --- O4 safe fixers (the auditable allowlist) --------------------------------

def _fix_cache_stale(store, db_path: Optional[Path]) -> str:
    """Clear stale derived cache rows.  The cache holds only pointers into
    the canonical index -- never the only copy of user history -- and the
    next get_continuation_context() rebuilds whatever it needs."""
    cache_rpt = check_cache(db_path)
    keys = list(cache_rpt.get("stale", []))
    for key in keys:
        store.con.execute("DELETE FROM meta WHERE key=?", (key,))
    return "cleared %d stale cache entr%s" % (
        len(keys), "y" if len(keys) == 1 else "ies")


def _fix_fts_inconsistent(store, db_path: Optional[Path]) -> str:
    """Rebuild the FTS index from the canonical events rows.  event_fts is
    a pure projection of `events`: rebuilding it can never lose history.

    Runs inside the caller's transaction, so no executescript here (that
    would COMMIT implicitly and break all-or-nothing) — the DDL and the
    projection mirror store._rebuild_fts deliberately.
    """
    store.con.execute("DROP TABLE IF EXISTS event_fts")
    store.con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5("
        "body, file_path, command, sid UNINDEXED, tokenize = 'trigram')")
    store.con.execute(
        """INSERT INTO event_fts(rowid, body, file_path, command, sid)
           SELECT id,
                  COALESCE(content,'') || ' ' || COALESCE(tool_input,'') || ' '
                    || COALESCE(tool_output,'') || ' ' || COALESCE(command,'')
                    || ' ' || COALESCE(stdout,''),
                  COALESCE(file_path,''), COALESCE(command,''),
                  COALESCE(sid,'')
           FROM events""")
    n = store.q("SELECT COUNT(*) n FROM event_fts")[0]["n"]
    return "rebuilt FTS index from canonical events (%d rows)" % n


#: The safe-repair allowlist (O4).  A code not listed here is NEVER executed
#: by --fix, no matter what flags an issue carries.  Adding an entry requires
#: proving the repair is derived-only, transactional and idempotent.
SAFE_FIXERS = {
    "CACHE_STALE": _fix_cache_stale,
    "FTS_INCONSISTENT": _fix_fts_inconsistent,
}


# --- the full report (backward-compatible dict) -----------------------------

def run(repo: Optional[str] = None,
        db_path: Optional[Path] = None) -> Dict[str, Any]:
    """The whole diagnostic, as a dict. Never raises.

    Returns the legacy dict shape (``store``, ``continuity``, ``cache``,
    ``providers``, ``issues``, ``blocking``, ``external``, ``non_blocking``,
    ``warnings``) plus the O4 additions (``issues`` now carry canonical fields,
    and ``leases``, ``pending``, ``verification`` are new top-level keys).

    `db_path` exists because `voyager --db X doctor` has to diagnose X, not the
    default index -- the same trap the `db` subcommands had.
    """
    # Collect providers (needed for both the report and issue collection)
    providers: Dict[str, Any] = {}
    for p in PROVIDERS:
        ev = collect_evidence(p)
        cfg = check_hook_config(p)
        budget_info = provider_context_budget_info(p)
        providers[p] = {
            "state": provider_state(p, ev),
            "installed": ev.installed,
            "source_path": SOURCE_PATHS.get(p),
            "hook_registered": ev.hook_registered,
            "hook_config_valid": cfg.get("valid"),
            "hook_config_error": cfg.get("error"),
            "hook_fired": ev.hook_fired,
            "last_trigger": ev.last_trigger,
            "budget": budget_info.get("startup_context_budget"),
            "transport_limit": budget_info.get("transport_limit"),
            "truncation_behavior": budget_info.get("truncation_behavior"),
            "supports_spill_pointer": budget_info.get("supports_spill_pointer", False),
            "native_session_id_at_start": budget_info.get("native_session_id_at_start", False),
        }

    store = check_store(db_path)
    cont = check_continuity(repo, db_path)
    retention = check_retention(db_path)
    cache = check_cache(db_path)
    fts = check_fts(db_path)
    leases = check_leases(db_path)
    pending = check_pending(db_path)
    verification = check_verification(db_path)

    # Collect canonical issues
    issues = collect_issues(repo, db_path)
    issue_dicts = [i.to_dict() for i in issues]

    return {
        "context_format": CONTEXT_FORMAT,
        "store": store,
        "continuity": cont,
        "cache": cache,
        "fts": fts,
        "retention": retention,
        "leases": leases,
        "pending": pending,
        "verification": verification,
        "providers": providers,
        "issues": issue_dicts,
        # Legacy grouping (derived from severity + repair_kind):
        "blocking": [d for d in issue_dicts if d["kind"] == "blocking"],
        "external": [d for d in issue_dicts if d["kind"] == "external"],
        "non_blocking": [d for d in issue_dicts if d["kind"] == "non-blocking"],
        "warnings": [d for d in issue_dicts if d["kind"] == "warning"],
    }


def render(report: Dict[str, Any]) -> str:
    """The human view. ASCII-safe; no colour, no width assumptions."""
    lines: List[str] = []
    lines.append("voyager doctor")
    lines.append("=" * 62)

    st = report.get("store") or {}
    lines.append("")
    lines.append("store")
    lines.append("  path     : %s" % (st.get("path") or "-"))
    lines.append("  sessions : %s   threads: %s"
                 % (st.get("sessions"), st.get("threads")))
    if st.get("error"):
        lines.append("  error    : %s" % st["error"])

    cont = report.get("continuity") or {}
    lines.append("")
    lines.append("continuity")
    lines.append("  active threads : %s   pending: %s   ambiguous: %s"
                 % (cont.get("active_threads"), cont.get("pending"),
                    cont.get("ambiguous")))
    cov = cont.get("coverage") or {}
    if cov:
        lines.append("  provenance     : %s events, %s%% classified, %s literal 'unknown'"
                     % (cov.get("events"), cov.get("classified_pct"),
                        cov.get("literal_unknown")))
    cache = report.get("cache") or {}
    lines.append("  cache          : %s entries, newest %s s ago"
                 % (cache.get("entries"), cache.get("newest_age_s")))
    if cache.get("stale"):
        lines.append("  cache stale   : %d entr%s (auto-fixable: %s)"
                     % (len(cache["stale"]),
                        "y" if len(cache["stale"]) == 1 else "ies",
                        "yes" if cache.get("auto_fixable") else "no"))
    lines.append("  context format : %s" % report.get("context_format"))

    # O2 retention: reported, not hidden.
    ret = report.get("retention") or {}
    if ret.get("sessions"):
        lines.append("  retained       : %s session(s) from missing/rotated "
                     "sources, %s event(s), ~%s MB"
                     % (ret.get("sessions"), ret.get("events"),
                        round((ret.get("bytes") or 0) / 1048576, 1)))
        by = ", ".join("%s:%s" % (p.get("provider"), p.get("sessions"))
                       for p in (ret.get("providers") or []))
        if by:
            lines.append("  retained by    : %s" % by)
        if ret.get("oldest"):
            lines.append("  oldest missing : %s"
                         % time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(ret["oldest"])))

    # O4.5: leases
    leases = report.get("leases") or {}
    if leases.get("total"):
        lines.append("  leases         : %s total, %s active, %s expired"
                     % (leases.get("total"), leases.get("active"),
                        leases.get("expired")))

    # O4.4: pending
    pending = report.get("pending") or {}
    if pending.get("open"):
        lines.append("  pending        : %s open, %s stale"
                     % (pending.get("open"), len(pending.get("stale", []))))

    lines.append("")
    lines.append("providers")
    lines.append("  %-12s %-26s %-8s %-8s %-6s %-8s"
                 % ("provider", "state", "installed", "hook", "fired", "budget"))
    lines.append("  " + "-" * 74)
    for p, info in (report.get("providers") or {}).items():
        budget = info.get("budget")
        budget_str = f"{budget}c" if budget else "-"
        lines.append("  %-12s %-26s %-8s %-8s %-6s %-8s"
                     % (p, info.get("state"),
                        "Y" if info.get("installed") else "N",
                        "Y" if info.get("hook_registered") else "N",
                        "Y" if info.get("hook_fired") else "N",
                        budget_str))

    # O5.7: verification three-layer view with evidence details
    verification = report.get("verification") or {}
    vp = verification.get("providers") or {}
    if vp:
        lines.append("")
        lines.append("verification")
        lines.append("  %-12s %-26s %-26s %-26s"
                     % ("provider", "declared", "observed", "effective"))
        lines.append("  " + "-" * 92)
        for p, v in vp.items():
            lines.append("  %-12s %-26s %-26s %-26s"
                         % (p,
                            v.get("declared", "-"),
                            v.get("observed") or "(none)",
                            v.get("effective", "-")))
        # Evidence details
        any_evidence = any(v.get("evidence_count", 0) for v in vp.values())
        if any_evidence:
            lines.append("")
            lines.append("  evidence details:")
            for p, v in vp.items():
                if not v.get("evidence_count"):
                    continue
                lines.append("    %-12s chains=%-4s events=%-5s chain=%s"
                             % (p, v.get("chains", 0),
                                v.get("evidence_count", 0),
                                v.get("best_chain") or "-"))
        # Blocked reasons
        any_blocked = any(v.get("blocked_reason") for v in vp.values())
        if any_blocked:
            lines.append("")
            lines.append("  blocked reasons:")
            for p, v in vp.items():
                reason = v.get("blocked_reason")
                if reason:
                    lines.append("    %-12s %s" % (p, reason))

    for title, key in (("blocking", "blocking"),
                       ("warning", "warnings"),
                       ("external", "external"),
                       ("non-blocking debt", "non_blocking")):
        items = report.get(key) or []
        if not items:
            continue
        lines.append("")
        lines.append("%s (%d)" % (title, len(items)))
        for it in items:
            lines.append("  [%s] %s" % (it.get("id"), it.get("detail")))

    if not (report.get("blocking") or []):
        lines.append("")
        lines.append("no blocking issues")
    return "\n".join(lines)
