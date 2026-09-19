"""Automatic Continuity engine (post-0.3.0).

The point: the user should not have to remember `handoff` / `continue` /
`switch` / `thread attach`. This module provides the automatic half:

- **pending attach auto-resolution** — after a switch/handoff launches a
  target agent, the next scan matches the newly indexed session against
  the pending-attach record (provider + repo/cwd + born-after-launch +
  unattached) and attaches it automatically. Ambiguities are never
  guessed: they stay open and surface as AMBIGUOUS.
- **continuity discovery** — one deterministic lookup answering "is there
  an active WorkThread for the repo I'm in, and what state is it in?"
- **continuation context** — the compiled, budget-aware context for the
  next agent, built by the SAME core the CLI uses (Phase 3 ranker +
  Phase 4 budget). Deterministic, offline, provenance-bound.

Everything here is read-only toward provider files.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .store import Store

# A pending attach older than this is marked stale (the continuation it
# waited for was either abandoned or already handled manually).
PENDING_TTL = 7 * 86400.0

# Tolerance when matching "session born after the switch launched":
# filesystem timestamps and provider clocks drift; two minutes is safe.
LAUNCH_TIME_SLACK = 120.0


def _now() -> float:
    return time.time()


def _repo_of(row) -> str:
    return (row["repo_root"] or row["cwd"] or "") if "repo_root" in row.keys() \
        else (row["cwd"] or "")


def _same_repo(a: str, b: str) -> bool:
    a = (a or "").replace("\\", "/").rstrip("/").lower()
    b = (b or "").replace("\\", "/").rstrip("/").lower()
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a) \
        or a in b or b in a


# ---------------------------------------------------------------------------
# Phase A — pending attach auto-resolution
# ---------------------------------------------------------------------------

def resolve_pending_attaches(store: Store, now: Optional[float] = None,
                             quiet: bool = True) -> Dict[str, Any]:
    """Match open pending-attach records against newly indexed sessions.

    Match requirements (ALL must hold for an auto-attach):
    - provider matches the pending's target provider
    - the session is not attached to ANY thread (no silent swallowing)
    - session repo_root/cwd matches the pending's repo/cwd
    - the session was born after the switch launched (with slack)
    - exactly one such candidate exists (0 → keep waiting; >1 → AMBIGUOUS)

    A pending past its TTL is marked stale. Returns a stats dict; every
    auto-attach is written to the continuity audit log.
    """
    now = _now() if now is None else now
    stats: Dict[str, Any] = {"checked": 0, "attached": [], "ambiguous": [],
                             "stale": [], "open": 0}
    open_rows = store.pending_open()
    stats["checked"] = len(open_rows)

    claims: Dict[str, List[Dict[str, Any]]] = {}   # session sid -> pendings
    plans: List[Tuple[Any, List[Any]]] = []

    for pend in open_rows:
        stats["checked"] = stats.get("checked", 0) + 0
        launched = pend["created_at"] or 0
        if now - launched > PENDING_TTL:
            store.pending_mark(pend["rid"], "stale")
            stats["stale"].append({"thread": pend["thread_id"],
                                   "provider": pend["provider"]})
            continue
        repo_ref = pend["repo_root"] or pend["cwd"]
        cands = []
        for s in store.q(
            """SELECT * FROM sessions
               WHERE provider=?
                 AND id NOT IN (SELECT session_id FROM thread_sessions)
                 AND COALESCE(started_at, updated_at, 0) >= ?""",
            (pend["provider"], launched - LAUNCH_TIME_SLACK),
        ):
            if repo_ref and _same_repo(
                    s["repo_root"] or s["cwd"] or "", repo_ref):
                cands.append(s)
        plans.append((pend, cands))
        for c in cands:
            claims.setdefault(c["id"], []).append(pend)

    for pend, cands in plans:
        # cross-pending conflict: a session claimed by more than one
        # pending must not be silently attached to either
        cands = [c for c in cands
                 if len(claims.get(c["id"], [])) == 1]
        if len(cands) == 1:
            c = cands[0]
            store.thread_attach(pend["thread_id"], c["id"])
            store.pending_mark(pend["rid"], "resolved", c["id"])
            store._continuity_log("auto-attach", thread=pend["thread_id"],
                                  session=c["id"], provider=pend["provider"])
            stats["attached"].append({"thread": pend["thread_id"],
                                      "session": c["id"]})
        elif len(cands) > 1:
            store.pending_mark(pend["rid"], "ambiguous")
            stats["ambiguous"].append({"thread": pend["thread_id"],
                                       "candidates": [c["id"] for c in cands]})
        else:
            stats["open"] += 1
    return stats


# ---------------------------------------------------------------------------
# Phase B — continuity discovery (shared core primitive)
# ---------------------------------------------------------------------------

def discover_continuity(store: Store, cwd: Optional[str] = None,
                        provider: Optional[str] = None,
                        native_session_id: Optional[str] = None,
                        thread_id: Optional[str] = None,
                        repo: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic continuity lookup for "I'm an agent, sitting in this
    repo — is there work to continue?".

    Resolution order: explicit thread → exact repo_root of the active
    WorkThread (via cwd git toplevel) → cwd fallback. No silent
    multi-signal clustering: if there is no persisted WorkThread, the
    answer is continuity_available=False.
    """
    from .adapters.base import git_info
    from .store import lease_state

    repo = repo or (git_info(cwd or os.getcwd()).get("repo_root")
                    or (cwd or os.getcwd()).replace("\\", "/"))
    threads = [t for t in store.thread_list("active")
               if t["repo_root"] and _same_repo(t["repo_root"], repo)]
    if thread_id:
        t = store.thread_get(thread_id)
        threads = [t] if t else []
    thread = max(threads, key=lambda x: x["updated_at"] or 0) if threads else None

    result: Dict[str, Any] = {
        "repo_root": repo,
        "continuity_available": thread is not None,
        "active_thread": None,
        "current_goal": None,
        "latest_holder": None,
        "latest_session": None,
        "lease_state": {"held": False, "expired": False, "why": "free",
                        "holder": None, "pid": None},
        "pending_attach": [],
        "recommended_action": "none",
    }
    if thread is None:
        return result

    result["active_thread"] = dict(thread)
    result["current_goal"] = thread["goal"] or thread["title"]
    lease = store.thread_lease_get(thread["id"])
    lst = lease_state(lease)
    result["lease_state"] = {"held": lst["held"], "expired": lst["expired"],
                             "why": lst["why"],
                             "holder": lease["holder"] if lease else None,
                             "pid": lease["pid"] if lease else None}

    members = store.thread_members(thread["id"])
    if members:
        newest = max(members, key=lambda m: m["updated_at"] or 0)
        result["latest_holder"] = newest["provider"]
        result["latest_session"] = dict(newest)

    pend = store.pending_open(thread_id=thread["id"])
    if pend:
        result["pending_attach"] = [
            {"provider": p["provider"], "created_at": p["created_at"],
             "note": p["note"]} for p in pend]

    # the caller's own session: already a member of this thread?
    unattached = None
    if native_session_id:
        own = f"{provider}:{native_session_id}"
        row = store.q("SELECT id FROM sessions WHERE id=?", (own,))
        if row and not store.attached_to_any_thread(own):
            unattached = own

    if unattached:
        result["recommended_action"] = "attach"
        result["unattached_session"] = unattached
    elif result["latest_holder"] == provider and provider:
        result["recommended_action"] = "native-resume"
    elif members:
        result["recommended_action"] = "compile-continuation"
    return result


# ---------------------------------------------------------------------------
# Phase 3 — shared continuation context (CLI / MCP / Skill / API)
# ---------------------------------------------------------------------------

def get_continuation_context(store: Optional[Store] = None,
                             db: Optional[Path] = None,
                             cwd: Optional[str] = None,
                             provider: Optional[str] = None,
                             native_session_id: Optional[str] = None,
                             thread_id: Optional[str] = None,
                             repo: Optional[str] = None,
                             goal: Optional[str] = None,
                             budget: Optional[str] = None,
                             target: Optional[str] = None,
                             sync: bool = True) -> Dict[str, Any]:
    """Compile the continuation context for the work in `cwd`/`thread`.

    Shared core surface for CLI / MCP / Skill / local API. Deterministic:
    scan (D12) → discover → rank (Phase 3) → budget (Phase 4) → context.
    No LLM, no network. Provenance is preserved end to end.
    """
    from .budget import apply_budget, auto_budget, parse_budget
    from .ranker import extract_candidate_facts, rank_candidates

    own_store = store is None
    store = store or Store(db)
    try:
        if sync:
            # D12: incremental scan before compiling (never --force)
            from .cli import run_scan
            run_scan(store, force=False, quiet=True)

        disc = discover_continuity(store, cwd=cwd, provider=provider,
                                   native_session_id=native_session_id,
                                   thread_id=thread_id, repo=repo)
        thread = disc.get("active_thread")
        if not thread:
            return {"continuity_available": False, "discovery": disc,
                    "context": None}

        tid = thread["id"]
        members = store.thread_members(tid)

        # automatic safe attach: the caller's own unattached session
        auto_attach = None
        if native_session_id:
            own = f"{provider}:{native_session_id}" if provider else None
            if own and store.q("SELECT 1 FROM sessions WHERE id=?", (own,)) \
                    and not store.attached_to_any_thread(own):
                store.thread_attach(tid, own)
                store.pending_mark_open_resolved(tid, provider)
                store._continuity_log("auto-attach", thread=tid,
                                      session=own, provider=provider)
                auto_attach = own
                members = store.thread_members(tid)

        facts = extract_candidate_facts(store, members)
        ranked = rank_candidates(facts, goal=goal)

        tokens = parse_budget(budget)
        if tokens is None and budget and budget.strip().lower() == "auto":
            tokens = auto_budget(target)
        from .continuity import build_continuation_bundle
        raw_bundle = build_continuation_bundle(store, members, goal=goal)
        packed, info = apply_budget(raw_bundle, tokens, target=target)
        store._continuity_log("context-compiled", thread=tid,
                              provider=provider, budget=info["budget"])

        return {
            "continuity_available": True,
            "discovery": disc,
            "thread": {"id": tid, "title": thread["title"],
                       "goal": thread["goal"], "status": thread["status"]},
            "members": [m["id"] for m in members],
            "context": packed,
            "estimated_tokens": info["estimated_tokens"],
            "budget": info["budget"],
            "auto_attach": auto_attach,
        }
    finally:
        if own_store:
            store.close()


# ---------------------------------------------------------------------------
# Phase D — holder-aware continuity cycle (watch)
# ---------------------------------------------------------------------------

def continuity_cycle(store: Store, quiet: bool = True) -> Dict[str, Any]:
    """One watch cycle of automatic continuity: resolve pending attaches,
    renew live-holder heartbeats. Returns a summary for observability."""
    pend = resolve_pending_attaches(store, quiet=quiet)
    renewed = store.thread_lease_renew_alive()
    out = {"pending": pend, "heartbeat_renewed": renewed}
    if not quiet:
        for a in pend["attached"]:
            print("  ↳ auto-attached {0} → {1}".format(
                a["session"], a["thread"]))
        for a in pend["ambiguous"]:
            print("  ↳ pending {0}: AMBIGUOUS ({1} candidates) — "
                  "not attached; resolve with `voyager thread attach`".format(
                      a["thread"], len(a["candidates"])))
        for s in pend["stale"]:
            print("  ↳ pending stale: {0} ({1})".format(
                s["thread"], s["provider"]))
        if renewed:
            print("  ↳ lease heartbeat renewed ({0})".format(renewed))
    return out
