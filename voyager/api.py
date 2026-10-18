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
from .store import default_db_path, open_read, Store
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
    store = open_read(db)
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
    # Filter and order the cheap columns first, truncate, and only then read
    # events for the rows that are actually returned.  Reading events inside
    # the candidate loop made this an N+1 (OVERVIEW_RECENT_SESSIONS_N1): on the
    # 10k benchmark dataset it issued 10,016 statements to produce 12 rows,
    # with subprocess = 0, so the recorded reason for leaving it alone — that
    # the cost was sandbox spawn overhead rather than the query — did not hold.
    candidates = [
        r for r in store.sessions()
        if (r["updated_at"] or 0) >= cutoff
        and not (repo and not same_repo_loose(r["repo_root"] or r["cwd"] or "", repo))
    ]
    candidates.sort(key=lambda x: x["updated_at"] or 0, reverse=True)
    recent = []
    for r in candidates[:limit]:
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
    return {"stats": stats, "threads": threads[:limit],
            "recent_sessions": recent}


def thread_detail(db: Optional[Path] = None, thread_id: str = "") -> Dict[str, Any]:
    """Thread + members + lease + pending attaches for the detail view."""
    from .store import lease_state
    store = open_read(db)
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
    store = open_read(db)
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
    store = open_read(db)
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
    # Same canonical order as every other renderer: if the caller named
    # sessions that all belong to one active WorkThread, the bundle follows
    # that thread's handoff order rather than the clock.  When they belong to
    # no thread, the activity order stands (issue #15).
    tid = store.thread_find_containing({r["id"] for r in rows}) if rows else None
    bundle = build_continuation_bundle(store, rows, goal=goal, thread=tid)
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
    store = open_read(db)
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
    store = open_read(db)
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


def _resolve_thread(store: Store, repo: Optional[str] = None,
                    thread_id: Optional[str] = None) -> Dict[str, Any]:
    """The shared WorkThread resolution.

    Delegates to :func:`voyager.auto.resolve_work_thread` so the session-start
    hook, the MCP surface and this API cannot answer "which thread is this
    repo's current work?" differently.  Imported lazily to keep this module's
    import graph as thin as it was.
    """
    from .auto import resolve_work_thread
    return resolve_work_thread(store, repo=repo, thread_id=thread_id)


def _newest_thread_for_repo(store: Store, repo: Optional[str]):
    """The default thread for a repo.

    Kept for callers that only need the row; new code should call
    :func:`_resolve_thread`, which also reports that a default is all it is.
    """
    return _resolve_thread(store, repo=repo)["thread"]


def _newest_session_for_repo(store: Store, repo: Optional[str]):
    rows = list(store.sessions())
    if repo:
        rows = [r for r in rows
                if same_repo_loose(r["repo_root"] or r["cwd"] or "", repo)]
    rows.sort(key=lambda r: r["updated_at"] or 0, reverse=True)
    return rows[0] if rows else None


#: How many sessions a repo-scope continuation may compile from when nothing
#: links them.  Bounded because "the whole repo" is not a task: a repo with 100
#: sessions must not produce a 100-session bundle, and the reader still has to
#: fit the result in a context window.
MAX_REPO_SCOPE_SESSIONS = 5


def _sessions_for_repo_scope(store: Store, repo: Optional[str]):
    """The bounded session set a repo-scoped continuation compiles from.

    Returns ``(selected, considered)``.  ``considered`` is how many sessions the
    repo actually has, so the caller can say what it left out instead of
    silently narrowing.

    Selection is **the newest few**, and that is a default, not a task match:
    two sessions sharing a repo path are not necessarily the same piece of work.
    Recency is used because it is deterministic, explainable and cheap; the reply
    reports that it is a default so a caller can ask for something better rather
    than inherit a guess.

    Why more than one: cross-agent continuity is the point.  A task routinely
    runs Codex → Claude → another agent, and picking only the newest session
    drops the earlier agents' open items and the evidence that contradicts the
    latest claim.  One session per task is not the same as one session per repo.
    """
    rows = list(store.sessions())
    if repo:
        rows = [r for r in rows
                if same_repo_loose(r["repo_root"] or r["cwd"] or "", repo)]
    rows.sort(key=lambda r: r["updated_at"] or 0, reverse=True)
    return rows[:MAX_REPO_SCOPE_SESSIONS], len(rows)


def current_work(db: Optional[Path] = None, repo: Optional[str] = None,
                 goal: Optional[str] = None, budget: Optional[str] = None,
                 target: Optional[str] = None,
                 thread_id: Optional[str] = None) -> Dict[str, Any]:
    """What is the current work for this repo?  Read-only.

    The ``sessionflow_current_work`` endpoint: the active WorkThread (if any),
    its members, the lease, and a continuation preview, in one round trip --
    so a client does not stitch several calls together and drift from what the
    CLI would have chosen.  Falls back to the newest session when there is no
    WorkThread.

    **Recency may suggest a default; it does not establish intent.**  When a
    repo has more than one active WorkThread the reply carries
    ``thread_ambiguous: true`` and every candidate (``thread_candidates``), and
    the ``thread`` field is labelled as a default rather than presented as the
    answer.  With exactly one thread nothing is added, so the common case stays
    quiet.  Naming ``thread_id`` is an explicit choice: it is honoured even if
    it belongs to another repo (reported as ``thread_out_of_scope``), and a
    ``thread_id`` that does not exist is reported as ``thread_not_found``
    instead of silently falling back to some other thread.

    All of those fields are additive; ``schema_version`` stays 1.
    """
    store = open_read(db)
    try:
        resolved = _resolve_thread(store, repo=repo, thread_id=thread_id)
        thread = resolved["thread"]
        out: Dict[str, Any] = {"repo": repo, "has_thread": thread is not None}

        if resolved["explicit"]:
            out["thread_requested"] = thread_id
            if resolved["not_found"]:
                out["thread_not_found"] = True
            elif resolved["out_of_scope"]:
                out["thread_out_of_scope"] = True
        elif resolved["status"] == "ambiguous":
            out["thread_ambiguous"] = True
            out["thread_candidates"] = resolved["candidates"]
            if resolved["candidates_truncated"]:
                out["candidates_truncated"] = True
            out["thread_default_reason"] = (
                "{0} active WorkThreads in scope; `thread` is the most "
                "recently active one, which is a default and not a choice "
                "anyone made".format(len(resolved["candidates"])))

        refs: List[str] = []
        if thread:
            out["thread"] = {k: thread[k] for k in
                             ("id", "title", "repo_root", "status", "goal",
                              "members", "updated_at")}
            detail = thread_detail(db, thread["id"])
            out["members"] = detail.get("members", [])
            out["lease"] = detail.get("lease")
            refs = [m["id"] for m in out["members"]]
        elif resolved["not_found"]:
            # The caller named a thread and it is not there.  Falling back to
            # "the newest session of this repo" would answer a question nobody
            # asked, and the continuation bundle would look authoritative while
            # `thread_not_found` sat next to it.  Report, do not substitute.
            pass
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

    A named ``thread_id`` that does not exist is an error, not a cue to fall
    back to some other thread: the caller asked for a specific one.  When the
    repo scope resolves to more than one active thread the bundle is still
    compiled (this is a read), but the reply says so -- ``thread_ambiguous``
    plus ``thread_candidates`` -- and labels the one it used as a default.
    """
    store = open_read(db)
    resolved: Optional[Dict[str, Any]] = None
    # Set only when the fallback had to pick sessions itself; stays None when the
    # caller named a thread or explicit refs, where no default was invented.
    repo_scope: Optional[Dict[str, Any]] = None
    try:
        refs = list(session_refs or [])
        if not refs:
            resolved = _resolve_thread(store, repo=repo, thread_id=thread_id)
            if resolved["not_found"]:
                return {"error": "no such WorkThread: %s" % thread_id,
                        "thread_requested": thread_id, "thread_not_found": True}
            thread = resolved["thread"]
            if thread:
                refs = [m["id"] for m in store.thread_members(thread["id"])]
        if not refs:
            # No thread links these sessions, so nothing establishes that they
            # are the same piece of work.  Compile from the newest few instead of
            # the newest one -- a cross-agent task is routinely several sessions,
            # and taking only the latest drops earlier open items and the
            # evidence that contradicts the latest claim -- but say plainly that
            # recency is a default, and how much was left out.
            picked, considered = _sessions_for_repo_scope(store, repo)
            if picked:
                refs = [r["id"] for r in picked]
                repo_scope = {
                    "selection_scope": "repo_recent",
                    "selected_session_ids": list(refs),
                    "selection_reason": (
                        "no WorkThread links these sessions; the newest {0} of {1} "
                        "session(s) in this repo were compiled, so recency is a "
                        "default rather than a task match".format(len(refs), considered)
                    ),
                    "scope_ambiguous": considered > len(refs),
                    "excluded_session_count": max(considered - len(refs), 0),
                }
        if not refs:
            return {"error": "no sessions to continue from"}
    finally:
        store.close()
    out = bundle_preview(db, session_refs=refs, goal=goal, budget=budget,
                         target=target)
    if isinstance(out, dict):
        out.setdefault("scope", refs)
        if repo_scope is not None:
            # Additive only: existing callers read `scope`, new ones can explain
            # the choice.  Nothing already present changes meaning.
            out.update(repo_scope)
        if resolved is not None:
            if resolved["explicit"]:
                out["thread_requested"] = thread_id
                if resolved["out_of_scope"]:
                    out["thread_out_of_scope"] = True
            elif resolved["status"] == "ambiguous":
                out["thread_ambiguous"] = True
                out["thread_candidates"] = resolved["candidates"]
                if resolved["candidates_truncated"]:
                    out["candidates_truncated"] = True
                out["thread_default_reason"] = (
                    "{0} active WorkThreads in scope; the bundle was compiled "
                    "from the most recently active one, which is a default and "
                    "not a choice anyone made".format(
                        len(resolved["candidates"])))
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
