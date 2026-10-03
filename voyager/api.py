"""Voyager local API (roadmap Phase 7 / issue #8) — the thin client surface.

Every function here is a JSON-able wrapper over the SAME core the CLI uses
(store + ranker + budget + continuity). No business logic lives here, no
direct SQLite access, no second implementation — the VS Code extension (and
any other UI) talks to this surface, over stdio JSON-lines via
`voyager api serve` or by importing these functions directly.

Read-only by design: the only "write-ish" op is bundle_preview, which
writes nothing (it renders a bundle in memory).

Boundary (P9): this is the *read-only context* facet — it compiles and
returns text, and never leases, records a pending attach or launches. The
*handoff* facet (one WorkThread changing hands between agents) is
`continuity.handoff_thread()`, which the CLI and MCP share. A UI that wants
to hand work over must call that engine through the CLI/MCP surface rather
than growing a second pipeline here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .budget import apply_budget, parse_budget, resolve_auto_budget
from .continuity import build_continuation_bundle
from .ranker import extract_candidate_facts, rank_candidates
from .store import Store, default_db_path
from .util import parse_when, same_repo_loose


def _row(r) -> Dict[str, Any]:
    d = {k: r[k] for k in r.keys()}
    d["can_resume"] = bool(d.get("can_resume"))
    d["can_fork"] = bool(d.get("can_fork"))
    for key in ("metadata_json", "raw_metadata_json"):
        if d.get(key):
            try:
                d[key.replace("_json", "")] = json.loads(d[key])
            except json.JSONDecodeError:
                pass
        d.pop(key, None)
    return d


def overview(db: Optional[Path] = None, repo: Optional[str] = None,
             hours: float = 48.0, limit: int = 12) -> Dict[str, Any]:
    """Everything a sidebar needs on one screen: index stats, active
    threads, recent sessions."""
    import time
    store = Store(db)
    stats = store.stats()
    threads = [
        {k: t[k] for k in ("id", "title", "repo_root", "status", "members",
                           "updated_at")}
        for t in store.thread_list("active")
    ]
    if repo:
        threads = [t for t in threads
                   if t["repo_root"] and same_repo_loose(t["repo_root"], repo)]
    cutoff = time.time() - hours * 3600
    recent = []
    for r in store.sessions():
        if (r["updated_at"] or 0) < cutoff:
            continue
        if repo and not same_repo_loose(r["repo_root"] or r["cwd"] or "", repo):
            continue
        last_user = ""
        for e in reversed(store.events(r["id"])):
            if e["kind"] == "user" and e["content"]:
                last_user = " ".join(e["content"].split())[:160]
                break
        recent.append({
            "id": r["id"], "provider": r["provider"],
            "native_session_id": r["native_id"],
            "updated_at": r["updated_at"], "title": r["title"],
            "last_user": last_user,
            "message_count": r["message_count"], "tool_count": r["tool_count"],
            # O2: machine-readable retention state, so a UI can mark
            # "[source missing]" instead of silently hiding the row.
            "source_state": r["source_state"],
        })
    store.close()
    recent.sort(key=lambda x: x["updated_at"] or 0, reverse=True)
    return {"stats": stats, "threads": threads[:limit],
            "recent_sessions": recent[:limit]}


def thread_detail(db: Optional[Path] = None, thread_id: str = "") -> Dict[str, Any]:
    """Thread + members + lease + pending attaches for the detail view."""
    from .store import lease_state
    store = Store(db)
    t = store.thread_get(thread_id)
    if not t:
        store.close()
        return {"error": f"thread not found: {thread_id}"}
    lease = store.thread_lease_get(t["id"])
    st = lease_state(lease)
    members = [
        {k: m[k] for k in ("id", "provider", "native_id", "title",
                           "message_count", "tool_count", "updated_at",
                           "can_resume", "source_state",
                           "source_missing_since")}
        for m in store.thread_members(t["id"])
    ]
    pending = [dict(p) for p in store.thread_pending_list(t["id"])]
    store.close()
    return {"thread": {k: t[k] for k in t.keys()}, "members": members,
            "lease": {"held": st["held"], "expired": st["expired"],
                      "why": st["why"],
                      "holder": lease["holder"] if lease else None,
                      "pid": lease["pid"] if lease else None},
            "pending": pending}


def sessions(db: Optional[Path] = None, repo: Optional[str] = None,
             platform: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    store = Store(db)
    rows = store.sessions(platform)
    if repo:
        rows = [r for r in rows
                if same_repo_loose(r["repo_root"] or r["cwd"] or "", repo)]
    out = [_row(r) for r in rows[:limit]]
    store.close()
    return out


def bundle_preview(db: Optional[Path] = None,
                   session_refs: Optional[List[str]] = None,
                   goal: Optional[str] = None,
                   budget: Optional[str] = None,
                   target: Optional[str] = None) -> Dict[str, Any]:
    """Render a Continuation Bundle in memory (Phase 3 ranker + Phase 4
    budget) and return it with the token estimate. Writes nothing."""
    store = Store(db)
    rows: List[Any] = []
    for ref in session_refs or []:
        row, ambiguous = store.session(ref)
        if row is None:
            store.close()
            return {"error": f"session not found: {ref}"}
        if ambiguous:
            store.close()
            return {"error": "ambiguous session ref: " + ref,
                    "candidates": [r["native_id"] for r in ambiguous[:10]]}
        rows.append(row)
    try:
        tokens = parse_budget(budget)
        if tokens is None and budget and budget.strip().lower() == "auto":
            # Use provider-aware auto budget resolution
            tokens = resolve_auto_budget(target)
    except ValueError as e:
        store.close()
        return {"error": str(e)}
    bundle = build_continuation_bundle(store, rows, goal=goal)
    packed, info = apply_budget(bundle, tokens, target=target)
    store.close()
    return {"bundle": packed, "estimated_tokens": info["estimated_tokens"],
            "budget": info["budget"], "dropped": info["dropped"],
            "trimmed": info["trimmed"]}


def thread_timeline(db: Optional[Path] = None, thread_id: str = "",
                    limit: Optional[int] = None,
                    kinds: Optional[List[str]] = None,
                    provider: Optional[str] = None,
                    state: Optional[str] = None) -> Dict[str, Any]:
    """O3: one WorkThread's lifecycle, as the canonical timeline.

    Thin pass-through to :func:`voyager.timeline.build_thread_timeline` — the
    same function the CLI and the dashboard call.  A UI must never aggregate
    its own timeline, because two aggregations drift and the drift is
    invisible.  Read-only: it opens no transaction and writes nothing.
    """
    from .timeline import build_thread_timeline
    store = Store(db)
    try:
        return build_thread_timeline(store, thread_id, limit=limit,
                                     kinds=kinds, provider=provider,
                                     state=state)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# machine-readable ops for thin client integrations (DSH plugin, VS Code)
#
# These are the *only* additions a client needs from the core, and each is a
# thin pass-through to a function the CLI already uses -- a second
# implementation is how the two surfaces drift.
# ---------------------------------------------------------------------------

#: Bumped only when an op's request or response shape changes.  A client must
#: fail clearly against an older core instead of misreading a field.
BRIDGE_SCHEMA_VERSION = 1


def _package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("voyager")
        except PackageNotFoundError:
            return "unknown"
    except Exception:
        return "unknown"


def _search_row(r) -> Dict[str, Any]:
    d = dict(r)
    return {
        "id": d.get("id"),
        "provider": d.get("provider"),
        "native_session_id": d.get("native_id"),
        "title": d.get("title"),
        "repo": d.get("repo_root") or d.get("cwd"),
        "updated_at": d.get("updated_at"),
        # the matching turn, not just the session: a client shows the excerpt
        "matched_at": d.get("_ts"),
        "matched_kind": d.get("_kind"),
        "matched_tool": d.get("_tool"),
        "matched_file": d.get("_file"),
        "excerpt": (d.get("snippet") or "").strip(),
    }


def search(db: Optional[Path] = None, query: str = "", provider: Optional[str] = None,
           repo: Optional[str] = None, since: Optional[str] = None,
           until: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """Substring search over every indexed session.  Read-only.

    Runs the same :meth:`Store.search` the CLI runs, with the same time parser
    (:func:`voyager.util.parse_when`), so `voyager search --json` and this op
    cannot return different rows for one query.  ``provider`` is a comma list;
    ``since`` / ``until`` accept ``2026-09-30`` or ``7d``.
    """
    store = Store(db)
    try:
        providers = [p.strip() for p in (provider or "").split(",") if p.strip()]
        rows = store.search(query, limit=int(limit or 20), filters={
            "providers": providers or None,
            "repo": repo,
            "since": parse_when(since),
            "until": parse_when(until),
        })
        return {"query": query, "count": len(rows),
                "results": [_search_row(r) for r in rows]}
    finally:
        store.close()


def _newest_thread_for_repo(store: Store, repo: Optional[str]):
    threads = [t for t in store.thread_list("active")
               if not repo or (t["repo_root"] and same_repo_loose(t["repo_root"], repo))]
    threads.sort(key=lambda t: t["updated_at"] or 0, reverse=True)
    return threads[0] if threads else None


def _newest_session_for_repo(store: Store, repo: Optional[str]):
    rows = list(store.sessions())
    if repo:
        rows = [r for r in rows
                if same_repo_loose(r["repo_root"] or r["cwd"] or "", repo)]
    rows.sort(key=lambda r: r["updated_at"] or 0, reverse=True)
    return rows[0] if rows else None


def current_work(db: Optional[Path] = None, repo: Optional[str] = None,
                 goal: Optional[str] = None, budget: Optional[str] = None,
                 target: Optional[str] = None) -> Dict[str, Any]:
    """What is the current work for this repo?  Read-only.

    The ``sessionflow_current_work`` endpoint: the active WorkThread (if any),
    its members, the lease, and a continuation preview, in one round trip --
    so a client does not stitch several calls together and drift from what the
    CLI would have chosen.  Falls back to the newest session when there is no
    WorkThread.
    """
    store = Store(db)
    try:
        thread = _newest_thread_for_repo(store, repo)
        out: Dict[str, Any] = {"repo": repo, "has_thread": thread is not None}
        refs: List[str] = []
        if thread:
            out["thread"] = {k: thread[k] for k in
                             ("id", "title", "repo_root", "status", "goal",
                              "members", "updated_at")}
            detail = thread_detail(db, thread["id"])
            out["members"] = detail.get("members", [])
            out["lease"] = detail.get("lease")
            refs = [m["id"] for m in out["members"]]
        else:
            row = _newest_session_for_repo(store, repo)
            if row is not None:
                out["session"] = _row(row)
                refs = [row["id"]]
        out["scope"] = refs
        if refs:
            out["continuation"] = bundle_preview(
                db, session_refs=refs, goal=goal or (thread["goal"] if thread else None),
                budget=budget, target=target)
        return out
    finally:
        store.close()


def continue_context(db: Optional[Path] = None,
                     session_refs: Optional[List[str]] = None,
                     thread_id: Optional[str] = None,
                     repo: Optional[str] = None,
                     goal: Optional[str] = None,
                     budget: Optional[str] = None,
                     target: Optional[str] = None) -> Dict[str, Any]:
    """The continuation bundle for a scope, chosen the way the CLI chooses it.

    Scope precedence: explicit ``session_refs``, else ``thread_id``, else the
    newest active WorkThread for ``repo``, else the newest session of ``repo``,
    else the newest session.  Compilation is
    :func:`voyager.continuity.build_continuation_bundle` via
    :func:`bundle_preview` -- the plugin must not grow a second continuation
    algorithm.  Read-only.
    """
    store = Store(db)
    try:
        refs = list(session_refs or [])
        if not refs and thread_id:
            refs = [m["id"] for m in store.thread_members(thread_id)]
        if not refs:
            thread = _newest_thread_for_repo(store, repo)
            if thread:
                refs = [m["id"] for m in store.thread_members(thread["id"])]
        if not refs:
            row = _newest_session_for_repo(store, repo)
            if row is not None:
                refs = [row["id"]]
        if not refs:
            return {"error": "no sessions to continue from"}
    finally:
        store.close()
    out = bundle_preview(db, session_refs=refs, goal=goal, budget=budget,
                         target=target)
    if isinstance(out, dict):
        out.setdefault("scope", refs)
    return out


def integration_info() -> Dict[str, Any]:
    """Version / capability probe for client integrations.  Read-only.

    A thin client must fail clearly when the core is older than the interface
    it drives, so the bridge ``schema_version`` is reported separately from the
    package version: the former changes only when an op's shape changes.
    """
    return {
        "name": "sessionFlow",
        "package": "voyager",
        "version": _package_version(),
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "ops": sorted(_OPS),
        "capabilities": sorted(_OPS),
    }


# ---------------------------------------------------------------------------
# stdio JSON-lines bridge: {"id": N, "op": "...", "params": {...}} per line
# ---------------------------------------------------------------------------

_OPS = {
    "overview": overview,
    "thread_detail": thread_detail,
    "sessions": sessions,
    "bundle_preview": bundle_preview,
    "thread_timeline": thread_timeline,
    "search": search,
    "current_work": current_work,
    "continue_context": continue_context,
    "integration_info": integration_info,
}


def handle_request(req: Dict[str, Any], db: Optional[Path] = None) -> Dict[str, Any]:
    rid = req.get("id")
    op = req.get("op")
    params = req.get("params") or {}
    fn = _OPS.get(op)
    if fn is None:
        return {"id": rid, "error": f"unknown op: {op!r} (ops: {sorted(_OPS)})"}
    try:
        if "db" in fn.__code__.co_varnames:
            params = dict(params)
            params.setdefault("db", params.pop("_db", None) or db)
        return {"id": rid, "result": fn(**params)}
    except (TypeError, ValueError) as e:
        return {"id": rid, "error": str(e)}


def serve(db: Optional[Path] = None) -> None:
    """JSON-lines stdio loop: one request per line, one response per line."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(json.dumps({"id": None, "error": f"bad json: {e}"}), flush=True)
            continue
        print(json.dumps(handle_request(req, db=db), ensure_ascii=False),
              flush=True)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="voyager-api",
                                description="Voyager local API (stdio JSON-lines)")
    p.add_argument("--db", help="index db path (default ~/.voyager/index.db)")
    args = p.parse_args()
    serve(Path(args.db) if args.db else None)


if __name__ == "__main__":
    main()
