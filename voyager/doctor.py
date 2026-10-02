"""`voyager doctor` -- is this installation healthy, and why not?

The point of this command is that nobody should have to read log files to work
out whether continuity is working.  It checks the store, the providers, the hook
registrations, the last observed trigger, the continuity state and the known
debts, and classifies everything it finds as:

  blocking      -- continuity is broken or untrustworthy right now
  warning       -- real, not blocking, but with a consequence worth seeing
  non-blocking  -- a debt: real, recorded, not stopping work
  external      -- a provider's own behaviour, outside sessionFlow

Provider states come from :mod:`voyager.capability_matrix`, so this output and
the README cannot disagree.

Every check is defensive: the doctor reports what it can and never raises.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .capability_matrix import (
    DIMENSIONS, HOOK_CONFIG_PATHS, PROVIDERS, SOURCE_PATHS,
    collect_evidence, provider_state, resolve_cell,
    PROVIDER_CONTEXT_BUDGETS,
)

#: Debts that are tracked but do not stop work.  Kept here so `doctor` is the one
#: place that answers "what is still open?"
KNOWN_DEBTS: Tuple[Dict[str, str], ...] = (
    {"id": "L1_STRONG_FAIRNESS", "kind": "non-blocking",
     "detail": "the per-session minimum is best-effort; a very small budget can "
               "still starve the newest STRONG session"},
    {"id": "PROVIDER_HANDLER_DUPLICATION", "kind": "non-blocking",
     "detail": "_log_dir/_log_event/_read_stdin are duplicated across handlers and "
               "the log-trim logic has already drifted"},
    {"id": "PROVENANCE_NORMALISE_DEAD_CODE", "kind": "non-blocking",
     "detail": "provenance.normalise() has no caller; DB NULL stays the single "
               "representation of unclassified"},
    {"id": "LIVE_VERIFICATION_BLOCKED_BY_PROVIDER_UI", "kind": "non-blocking",
     "detail": "zcode/cursor/kiro/antigravity need their own UI to fire a hook; "
               "they stay UNIT_VERIFIED until that is observed"},
    {"id": "EXTERNAL_CODEX_APPSERVER_CONPTY_POPUP", "kind": "external",
     "detail": "the managed Codex app-server launches git through ConPTY, which can "
               "surface Windows Terminal windows; not a sessionFlow bug"},
    {"id": "GIT_STALE_REFLOG_MISSING_OBJECT", "kind": "non-blocking",
     "detail": "a local dangling commit's tree is missing and a reflog entry points "
               "at it; reachable history is healthy and is deliberately left alone"},
)


def provider_context_budget_info(provider: str) -> Dict[str, Any]:
    """Get context budget details for a provider."""
    return PROVIDER_CONTEXT_BUDGETS.get(provider, {})

CONTEXT_FORMAT = "tiered-v1"


def _store_path(explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit is not None:
        return Path(explicit)
    from .store import default_db_path
    try:
        return Path(default_db_path())
    except Exception:
        return None


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
    that thread has nothing left to compile from — that is a real consequence
    the user should see, not a silent one.

    Never raises.
    """
    empty = {"available": False, "sessions": 0, "events": 0, "bytes": 0,
             "providers": [], "oldest": None, "stranded_threads": []}
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
    """How many continuity bundles are cached, and how old is the newest?"""
    out: Dict[str, Any] = {"entries": None, "newest_age_s": None}
    try:
        import os as _os
        from .store import Store
        path = _store_path(db_path)
        if not path or not path.exists():
            return out
        store = Store(path)
        try:
            rows = store.q("SELECT key FROM meta WHERE key LIKE 'ctx_cache%'")
            out["entries"] = len(rows)
            if rows:
                # the newest compiled_at we can find
                newest = None
                for r in rows:
                    raw = store.meta_get(r["key"])
                    if not raw:
                        continue
                    try:
                        ts = json.loads(raw).get("compiled_at")
                    except Exception:
                        continue
                    if ts and (newest is None or ts > newest):
                        newest = ts
                out["newest_age_s"] = int(time.time() - newest) if newest else None
        finally:
            store.close()
    except Exception:
        pass
    return out


def run(repo: Optional[str] = None,
        db_path: Optional[Path] = None) -> Dict[str, Any]:
    """The whole diagnostic, as a dict. Never raises.

    `db_path` exists because `voyager --db X doctor` has to diagnose X, not the
    default index -- the same trap the `db` subcommands had.
    """
    providers: Dict[str, Any] = {}
    for p in PROVIDERS:
        ev = collect_evidence(p)
        cfg = check_hook_config(p)

        # Get provider-specific budget info
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

    issues: List[Dict[str, str]] = []
    store = check_store(db_path)
    if not store.get("ok"):
        issues.append({"kind": "blocking", "id": "STORE_UNAVAILABLE",
                       "detail": store.get("error") or "index cannot be opened"})
    cont = check_continuity(repo, db_path)
    if cont.get("ambiguous"):
        issues.append({"kind": "blocking", "id": "AMBIGUOUS_WORKTHREAD",
                       "detail": "more than one active WorkThread for a repo: %s"
                                 % ", ".join(cont.get("ambiguous_repos") or [])})
    if cont.get("coverage") and cont["coverage"].get("literal_unknown"):
        issues.append({"kind": "blocking", "id": "PROVENANCE_LITERAL_UNKNOWN",
                       "detail": "%d rows store the literal 'unknown'"
                                 % cont["coverage"]["literal_unknown"]})
    for p, info in providers.items():
        if info["hook_registered"] and info["hook_config_valid"] is False:
            issues.append({"kind": "blocking", "id": "HOOK_CONFIG_INVALID",
                           "detail": "%s: %s" % (p, info["hook_config_error"])})
        if info["hook_fired"] and info["state"] in ("LIVE_VERIFIED",
                                                    "ZERO_TOUCH_LIVE_VERIFIED"):
            continue  # healthy
    for debt in KNOWN_DEBTS:
        issues.append(dict(debt))

    # O2: retention is informational.  It only escalates when an active
    # WorkThread has nothing live left to compile a continuation from.
    retention = check_retention(db_path)
    if retention.get("stranded_threads"):
        issues.append({
            "kind": "warning",
            "id": "RETENTION_STRANDED_WORKTHREAD",
            "detail": "%d active WorkThread(s) have no live member left "
                      "(all sources missing): %s"
                      % (len(retention["stranded_threads"]),
                         ", ".join(t["thread"] for t in
                                   retention["stranded_threads"])),
        })

    return {
        "context_format": CONTEXT_FORMAT,
        "store": store,
        "continuity": cont,
        "cache": check_cache(db_path),
        "retention": retention,
        "providers": providers,
        "issues": issues,
        "blocking": [i for i in issues if i["kind"] == "blocking"],
        "external": [i for i in issues if i["kind"] == "external"],
        "non_blocking": [i for i in issues if i["kind"] == "non-blocking"],
        "warnings": [i for i in issues if i["kind"] == "warning"],
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
    lines.append("  context format : %s" % report.get("context_format"))

    # O2 retention: reported, not hidden.  A rotated provider source is a normal
    # event; the point is that the history behind it is still here.
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
