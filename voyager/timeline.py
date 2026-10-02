"""O3 — the canonical WorkThread timeline.

**One model, one implementation.**  The CLI, the dashboard and the VS Code
webview all consume this module; none of them aggregates anything itself,
because three timelines would drift and the drift would be invisible.

Two rules govern what may appear here.

1. **Only what the canonical data can prove.**  Every event below comes either
   from a column that already carries a time (`threads.created_at`,
   `thread_sessions.attached_at`, `thread_pending.created_at`,
   `checkpoints.created_at`, `sessions.source_missing_since`) or from the
   append-only `thread_events` log, which exists precisely for the facts that
   have no other timestamped home.  Assistant prose is **never** scanned: "this
   sentence looks like a milestone" is a guess, and a timeline that guesses is
   worse than a short one.
2. **Timestamps order the display and nothing else.**  They never settle a
   WorkThread ambiguity and never establish authority — that is what the lease
   (D13) and the explicit resolution rules (D14) are for.

The timeline is a **lifecycle/milestone view, not a transcript dump**: it never
touches the `events` table, so its cost is bounded by the thread's own rows.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

# --- event types -----------------------------------------------------------

THREAD_CREATED = "THREAD_CREATED"
SESSION_ATTACHED = "SESSION_ATTACHED"
HANDOFF = "HANDOFF"
PROVIDER_SWITCHED = "PROVIDER_SWITCHED"
CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
THREAD_CLOSED = "THREAD_CLOSED"
THREAD_REOPENED = "THREAD_REOPENED"
THREAD_ARCHIVED = "THREAD_ARCHIVED"
SOURCE_MISSING = "SOURCE_MISSING"
SOURCE_RETURNED = "SOURCE_RETURNED"
COMMIT_OBSERVED = "COMMIT_OBSERVED"
TEST_GATE = "TEST_GATE"
BLOCKER_ADDED = "BLOCKER_ADDED"
BLOCKER_RESOLVED = "BLOCKER_RESOLVED"

ALL_TYPES = (
    THREAD_CREATED, SESSION_ATTACHED, HANDOFF, PROVIDER_SWITCHED,
    CHECKPOINT_CREATED, THREAD_CLOSED, THREAD_REOPENED, THREAD_ARCHIVED,
    SOURCE_MISSING, SOURCE_RETURNED, COMMIT_OBSERVED, TEST_GATE,
    BLOCKER_ADDED, BLOCKER_RESOLVED,
)

#: Types that only make sense for a session that is still live.
_LIVE_ONLY = (SESSION_ATTACHED,)

#: O3.3 wording.  A rotated provider source is **not** a deletion or a loss:
#: the history is right here.  These strings are the only place that says so,
#: so the CLI, the dashboard and the webview cannot drift into saying
#: "deleted".
SOURCE_MISSING_SUMMARY = "Provider source disappeared — history retained locally"
SOURCE_RETURNED_SUMMARY = "Provider source restored — session reconciled"


def _ev(kind: str, ts: Optional[float], *, eid: str, thread_id: str,
        provider: Optional[str] = None, session_id: Optional[str] = None,
        title: Optional[str] = None, summary: Optional[str] = None,
        source_state: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "id": eid,
        "timestamp": ts,
        "event_type": kind,
        "thread_id": thread_id,
        "provider": provider,
        "session_id": session_id,
        "title": title,
        "summary": summary,
        "source_state": source_state,
        "metadata": metadata or {},
    }


def _json_list(raw) -> List[Any]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def _text(item: Any) -> str:
    """Checkpoint entries are dicts (`CheckpointBlocker`/`CheckpointMilestone`
    serialise with a `description`).  Same convention thread_brief uses, so the
    timeline and the brief describe one checkpoint the same way."""
    if isinstance(item, dict):
        for k in ("description", "title", "text", "name"):
            if item.get(k):
                return str(item[k])
        return json.dumps(item, ensure_ascii=False, sort_keys=True)
    return str(item)


# --- derivations (each one evidence-backed) --------------------------------

def _thread_created(thread) -> List[Dict[str, Any]]:
    return [_ev(THREAD_CREATED, thread["created_at"],
                eid="created:%s" % thread["id"], thread_id=thread["id"],
                title=thread["title"],
                summary="WorkThread created"
                        + (" — %s" % thread["title"] if thread["title"] else ""),
                metadata={"repo_root": thread["repo_root"],
                          "goal": thread["goal"]})]


def _attachments(store, thread_id: str) -> List[Dict[str, Any]]:
    """`thread_sessions.attached_at` is written by thread_attach itself."""
    out = []
    for r in store.q(
            """SELECT ts.attached_at AS at, ts.ord AS ord, s.*
               FROM thread_sessions ts JOIN sessions s ON s.id = ts.session_id
               WHERE ts.thread_id=? ORDER BY ts.attached_at, ts.ord""",
            (thread_id,)):
        state = r["source_state"] or "LIVE"
        out.append(_ev(
            SESSION_ATTACHED, r["at"],
            eid="attached:%s:%s" % (thread_id, r["id"]),
            thread_id=thread_id, provider=r["provider"], session_id=r["id"],
            title=r["title"], source_state=state,
            summary="%s attached" % r["provider"],
            metadata={"ord": r["ord"], "native_id": r["native_id"],
                      "message_count": r["message_count"],
                      "tool_count": r["tool_count"]}))
    return out


def _handoffs(store, thread_id: str) -> List[Dict[str, Any]]:
    """A pending-attach record IS the handoff: it carries the source provider,
    the source session and the time.  `source_provider != provider` is a
    provider switch; the same provider is a plain handoff (a forced bundle)."""
    out = []
    for r in store.q(
            "SELECT * FROM thread_pending WHERE thread_id=? ORDER BY created_at",
            (thread_id,)):
        src, dst = r["source_provider"], r["provider"]
        switched = bool(src) and bool(dst) and src != dst
        kind = PROVIDER_SWITCHED if switched else HANDOFF
        out.append(_ev(
            kind, r["created_at"],
            eid="pending:%s:%s:%s" % (thread_id, r["provider"], r["created_at"]),
            thread_id=thread_id, provider=dst, session_id=r["source_session"],
            summary=("%s → %s" % (src, dst) if switched
                     else "handoff to %s" % dst),
            metadata={"from": src, "to": dst, "goal": r["goal"],
                      "note": r["note"], "status": r["status"],
                      "resolved_sid": r["resolved_sid"]}))
    return out


def _has_table(store, name: str) -> bool:
    """`checkpoints` is created by checkpoint.add_checkpoint_schema, not by the
    core SCHEMA, so a database that never used checkpoints simply does not have
    it — the timeline must still work there."""
    return bool(store.q(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _checkpoints(store, thread_id: str) -> List[Dict[str, Any]]:
    """Checkpoint facts only.  `blockers_json` / `tests_json` / `head_commit`
    are *explicit records* the user or the agent wrote down, which is why they
    may become events at all."""
    if not _has_table(store, "checkpoints"):
        return []
    out = []
    for r in store.q(
            "SELECT * FROM checkpoints WHERE thread_id=? ORDER BY created_at",
            (thread_id,)):
        cid = r["id"]
        out.append(_ev(
            CHECKPOINT_CREATED, r["created_at"],
            eid="chk:%s" % cid, thread_id=thread_id,
            summary="checkpoint: %s%s" % (r["phase"] or "?",
                                          " — %s" % r["goal"] if r["goal"] else ""),
            metadata={"checkpoint_id": cid, "phase": r["phase"],
                      "goal": r["goal"],
                      "milestones": _json_list(r["milestones_json"]),
                      "next_actions": _json_list(r["next_actions"])}))

        for i, b in enumerate(_json_list(r["blockers_json"])):
            if not b:
                continue
            out.append(_ev(
                BLOCKER_ADDED, r["created_at"],
                eid="chk-blocker:%s:%d" % (cid, i), thread_id=thread_id,
                summary="blocker: %s" % _text(b),
                metadata={"checkpoint_id": cid, "blocker": _text(b)}))

        # A blocker counts as resolved only when a LATER checkpoint records it
        # as a milestone -- i.e. the agent wrote down that it was done.  Absence
        # alone is not evidence (it may simply have been dropped), so nothing is
        # inferred from a blocker merely disappearing.
        milestones = " ".join(_text(m) for m in _json_list(r["milestones_json"]))
        for i, b in enumerate(_json_list(r["blockers_json"])):
            if b and _text(b) in milestones:
                out.append(_ev(
                    BLOCKER_RESOLVED, r["created_at"],
                    eid="chk-blocker-done:%s:%d" % (cid, i), thread_id=thread_id,
                    summary="blocker resolved: %s" % _text(b),
                    metadata={"checkpoint_id": cid, "blocker": _text(b)}))

        tests = _json_list(r["tests_json"])
        if tests:
            out.append(_ev(
                TEST_GATE, r["created_at"],
                eid="chk-tests:%s" % cid, thread_id=thread_id,
                summary="tests recorded: %s" % ", ".join(_text(t) for t in tests[:4]),
                metadata={"checkpoint_id": cid, "tests": [_text(t) for t in tests]}))

        if r["head_commit"]:
            out.append(_ev(
                COMMIT_OBSERVED, r["created_at"],
                eid="chk-commit:%s" % cid, thread_id=thread_id,
                summary="commit %s" % str(r["head_commit"])[:12],
                metadata={"checkpoint_id": cid, "head_commit": r["head_commit"],
                          "branches": _json_list(r["branches_json"])}))
    return out


def _source_missing(store, thread_id: str) -> List[Dict[str, Any]]:
    """A session's *current* retention episode, when the transition was not
    already logged.

    `prune_missing_sessions` logs SOURCE_MISSING as it happens, so the only
    case left here is a session that was already retained before that logging
    existed — and a currently-retained session must still show up.  The logged
    rows win, so nothing is ever counted twice.
    """
    logged = {
        r["session_id"] for r in store.q(
            "SELECT session_id FROM thread_events WHERE thread_id=?"
            " AND kind='SOURCE_MISSING'", (thread_id,))
    }
    out = []
    for r in store.q(
            """SELECT s.* FROM thread_sessions ts
               JOIN sessions s ON s.id = ts.session_id
               WHERE ts.thread_id=? AND s.source_state='SOURCE_MISSING'""",
            (thread_id,)):
        if r["id"] in logged:
            continue
        out.append(_ev(
            SOURCE_MISSING, r["source_missing_since"],
            eid="missing:%s:%s" % (thread_id, r["id"]),
            thread_id=thread_id, provider=r["provider"], session_id=r["id"],
            title=r["title"], source_state="SOURCE_MISSING",
            summary=SOURCE_MISSING_SUMMARY,
            metadata={"native_id": r["native_id"],
                      "derived": "current state (no transition log row)"}))
    return out


def _logged(store, thread_id: str) -> List[Dict[str, Any]]:
    out = []
    for r in store.q(
            "SELECT * FROM thread_events WHERE thread_id=? ORDER BY ts, id",
            (thread_id,)):
        kind = r["kind"]
        summary = {
            THREAD_CLOSED: "WorkThread closed",
            THREAD_REOPENED: "WorkThread reopened",
            THREAD_ARCHIVED: "WorkThread archived",
            SOURCE_MISSING: SOURCE_MISSING_SUMMARY,
            SOURCE_RETURNED: SOURCE_RETURNED_SUMMARY,
        }.get(kind, kind.replace("_", " ").title())
        # The state the event leaves the session in -- the same vocabulary the
        # derived events use, so a filter on 'retained' catches both.
        state = {SOURCE_MISSING: "SOURCE_MISSING",
                 SOURCE_RETURNED: "LIVE"}.get(kind)
        detail = {}
        if r["detail_json"]:
            try:
                detail = json.loads(r["detail_json"])
            except (TypeError, ValueError):
                detail = {}
        out.append(_ev(kind, r["ts"], eid="log:%d" % r["id"],
                       thread_id=thread_id, provider=r["provider"],
                       session_id=r["session_id"], summary=summary,
                       source_state=state, metadata=detail))
    return out


# --- the public model ------------------------------------------------------

def build_thread_timeline(store, thread_id: str, *, limit: Optional[int] = None,
                          kinds: Optional[Iterable[str]] = None,
                          provider: Optional[str] = None,
                          state: Optional[str] = None) -> Dict[str, Any]:
    """One WorkThread's lifecycle, oldest first.

    `limit` keeps the newest N events (the first screen is the recent story),
    then restores chronological order for display.  `kinds` / `provider` /
    `state` ('live' | 'retained') are the O3.4 filters, applied here so every
    consumer filters identically.
    """
    thread = store.thread_get(thread_id)
    if thread is None:
        return {"error": "no such WorkThread: %s" % thread_id}

    events: List[Dict[str, Any]] = []
    events += _thread_created(thread)
    events += _attachments(store, thread_id)
    events += _handoffs(store, thread_id)
    events += _checkpoints(store, thread_id)
    events += _source_missing(store, thread_id)
    events += _logged(store, thread_id)

    total = len(events)
    counts: Dict[str, int] = {}
    for e in events:
        counts[e["event_type"]] = counts.get(e["event_type"], 0) + 1

    if kinds:
        want = {k.upper() for k in kinds}
        events = [e for e in events if e["event_type"] in want]
    if provider:
        events = [e for e in events
                  if (e["provider"] or "").lower() == provider.lower()]
    if state:
        want_retained = state.lower() in ("retained", "source_missing")
        events = [e for e in events
                  if (e["source_state"] == "SOURCE_MISSING") == want_retained]

    # deterministic order: time first, then id — never a bare timestamp sort,
    # which would let two same-second events swap places between runs.
    events.sort(key=lambda e: (e["timestamp"] if e["timestamp"] is not None else 0.0,
                               e["id"]))
    shown = events
    if limit is not None and limit >= 0 and len(events) > limit:
        shown = events[-limit:] if limit else []

    return {
        "thread": {"id": thread["id"], "title": thread["title"],
                   "goal": thread["goal"], "status": thread["status"],
                   "repo_root": thread["repo_root"]},
        "events": shown,
        "total": total,
        "shown": len(shown),
        "counts": counts,
        "filters": {"kinds": sorted(kinds) if kinds else None,
                    "provider": provider, "state": state, "limit": limit},
    }


def render_text(timeline: Dict[str, Any], *, show_ids: bool = False) -> str:
    """The CLI view.  ASCII-safe, one line per event, oldest first."""
    if timeline.get("error"):
        return "error: %s" % timeline["error"]
    t = timeline["thread"]
    lines = ["WorkThread %s  [%s]  %s" % (t["id"], t["status"], t["title"] or "")]
    if t["repo_root"]:
        lines.append("repo: %s" % t["repo_root"])
    lines.append("")
    if not timeline["events"]:
        lines.append("(no timeline events)")
    import time as _time
    for e in timeline["events"]:
        ts = e["timestamp"]
        when = (_time.strftime("%Y-%m-%d %H:%M", _time.localtime(ts))
                if ts else "---------- --:--")
        bits = ["%s  %-18s" % (when, e["event_type"])]
        if e["provider"]:
            bits.append("[%s]" % e["provider"])
        bits.append(e["summary"] or "")
        line = " ".join(b for b in bits if b)
        if e["source_state"] == "SOURCE_MISSING":
            line += "  (retained: source unavailable)"
        lines.append(line)
        if show_ids and e["session_id"]:
            lines.append("      session: %s" % e["session_id"])
    if timeline["shown"] != timeline["total"]:
        lines.append("")
        lines.append("showing %d of %d event(s)"
                     % (timeline["shown"], timeline["total"]))
    return "\n".join(lines)
