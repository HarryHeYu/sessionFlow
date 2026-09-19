"""Voyager local API (roadmap Phase 7 / issue #8) — the thin client surface.

Every function here is a JSON-able wrapper over the SAME core the CLI uses
(store + ranker + budget + continuity). No business logic lives here, no
direct SQLite access, no second implementation — the VS Code extension (and
any other UI) talks to this surface, over stdio JSON-lines via
`voyager api serve` or by importing these functions directly.

Read-only by design: the only "write-ish" op is bundle_preview, which
writes nothing (it renders a bundle in memory).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .budget import apply_budget, auto_budget, parse_budget
from .continuity import build_continuation_bundle
from .ranker import extract_candidate_facts, rank_candidates
from .store import Store, default_db_path


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


def _same_repo(a: str, b: str) -> bool:
    a = (a or "").replace("\\", "/").rstrip("/").lower()
    b = (b or "").replace("\\", "/").rstrip("/").lower()
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a) \
        or a in b or b in a


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
                   if t["repo_root"] and _same_repo(t["repo_root"], repo)]
    cutoff = time.time() - hours * 3600
    recent = []
    for r in store.sessions():
        if (r["updated_at"] or 0) < cutoff:
            continue
        if repo and not _same_repo(r["repo_root"] or r["cwd"] or "", repo):
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
                           "can_resume")}
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
                if _same_repo(r["repo_root"] or r["cwd"] or "", repo)]
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
            tokens = auto_budget(target)
    except ValueError as e:
        store.close()
        return {"error": str(e)}
    bundle = build_continuation_bundle(store, rows, goal=goal)
    packed, info = apply_budget(bundle, tokens, target=target)
    store.close()
    return {"bundle": packed, "estimated_tokens": info["estimated_tokens"],
            "budget": info["budget"], "dropped": info["dropped"],
            "trimmed": info["trimmed"]}


# ---------------------------------------------------------------------------
# stdio JSON-lines bridge: {"id": N, "op": "...", "params": {...}} per line
# ---------------------------------------------------------------------------

_OPS = {
    "overview": overview,
    "thread_detail": thread_detail,
    "sessions": sessions,
    "bundle_preview": bundle_preview,
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
