"""Synthesise a WorkThread's state across every agent that worked on it.

The product claim is that several agents' work becomes one continuous task: Claude
designs, Codex implements, Grok reviews, ZCode fixes, and any of them opening next
knows where the thread stands.  That is not a concatenation of transcripts -- it is
a derivation over the canonical WorkThread:

    authoritative state   the thread's own goal / repo / HEAD
    latest checkpoint     the explicit, deterministic state somebody wrote down
    recent strong turns   the newest work, per agent, in canonical order
    contributions         who did what, and how much
    open items            blockers, unresolved decisions, next actions
    retrieval pointers    where to read more, per session

Everything here is deterministic and offline.  A model may later *enrich* this
output; nothing here depends on one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .provenance import session_band

#: How many turns per session are quoted in a brief.
TURNS_PER_SESSION = 3

#: Kinds that carry a human-visible decision or statement.
_SPEAKING_KINDS = ("user", "assistant")


def _one(store, sql: str, args: tuple = ()) -> Optional[Any]:
    try:
        rows = store.q(sql, args)
        return rows[0] if rows else None
    except Exception:
        return None


def _many(store, sql: str, args: tuple = ()) -> List[Any]:
    try:
        return list(store.q(sql, args))
    except Exception:
        return []


@dataclass
class Contribution:
    """What one session (one agent run) brought to the thread."""

    sid: str
    provider: str
    band: str
    events: int
    human_turns: int
    last_ts: Optional[float]
    last_line: Optional[str] = None
    native_session_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"sid": self.sid, "provider": self.provider, "band": self.band,
                "events": self.events, "human_turns": self.human_turns,
                "last_ts": self.last_ts, "last_line": self.last_line,
                "native_session_id": self.native_session_id}


@dataclass
class Brief:
    thread_id: str
    title: Optional[str] = None
    goal: Optional[str] = None
    repo_root: Optional[str] = None
    status: Optional[str] = None
    contributions: List[Contribution] = field(default_factory=list)
    recent: List[Dict[str, Any]] = field(default_factory=list)
    checkpoint: Optional[Dict[str, Any]] = None
    open_items: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread": {"id": self.thread_id, "title": self.title, "goal": self.goal,
                       "repo_root": self.repo_root, "status": self.status},
            "providers": sorted({c.provider for c in self.contributions}),
            "contributions": [c.to_dict() for c in self.contributions],
            "recent": self.recent,
            "checkpoint": self.checkpoint,
            "open_items": self.open_items,
        }


def _latest_checkpoint(store, thread_id: str) -> Optional[Dict[str, Any]]:
    try:
        from .checkpoint import get_latest_checkpoint
        cp = get_latest_checkpoint(store, thread_id)
    except Exception:
        return None
    if cp is None:
        return None
    def _line(item, *keys):
        """Render a structured entry as one readable line."""
        if isinstance(item, str):
            return item
        for k in keys:
            v = getattr(item, k, None) or (item.get(k) if isinstance(item, dict) else None)
            if v:
                return str(v)
        return str(item)

    try:
        milestones = [m for m in (getattr(cp, "milestones", []) or [])
                      if (getattr(m, "status", "") == "completed")]
        return {
            "id": getattr(cp, "checkpoint_id", None) or getattr(cp, "id", None),
            "created_at": getattr(cp, "created_at", None),
            "phase": getattr(cp, "phase", None),
            "goal": getattr(cp, "goal", None),
            "milestones": [_line(m, "title", "description") for m in milestones],
            "blockers": [_line(b, "description", "title")
                         for b in (getattr(cp, "blockers", []) or [])
                         if not getattr(b, "resolved", False)],
            "decisions": [_line(d, "decision", "topic")
                          for d in (getattr(cp, "decisions", []) or [])],
            "next_actions": list(getattr(cp, "next_actions", []) or []),
            "changed_files": list(getattr(cp, "changed_files", []) or []),
        }
    except Exception:
        return None


def activity(store, thread_id: str, limit: int = 20) -> Dict[str, Any]:
    """Per-agent contribution to one thread. Read-only and deterministic."""
    thread = _one(store, "SELECT * FROM threads WHERE id=?", (thread_id,))
    if thread is None:
        return {"error": "no such WorkThread: %s" % thread_id}
    members = _many(store, "SELECT * FROM thread_sessions WHERE thread_id=?",
                    (thread_id,))
    rows: List[Contribution] = []
    for m in members:
        sid = m["session_id"]
        sess = _one(store, "SELECT * FROM sessions WHERE id=?", (sid,))
        if sess is None:
            continue
        stats: Dict[str, int] = {}
        try:
            band, stats = session_band(store.con, sid)
        except Exception:
            band = "UNKNOWN"
        ev = _one(store, "SELECT COUNT(*) AS n, MAX(ts) AS last FROM events "
                         "WHERE sid=?", (sid,))
        last_line = None
        try:
            row = _one(store, "SELECT content FROM events WHERE sid=? AND kind=? "
                              "ORDER BY ts DESC, seq DESC LIMIT 1",
                       (sid, "assistant"))
            if row and row["content"]:
                last_line = " ".join(str(row["content"]).split())[:160]
        except Exception:
            pass
        rows.append(Contribution(
            sid=sid,
            provider=sess["provider"] if "provider" in sess.keys() else "?",
            band=band,
            events=(ev["n"] if ev else 0) or 0,
            human_turns=stats.get("human", 0),
            last_ts=(ev["last"] if ev else None),
            last_line=last_line,
            native_session_id=(sess["native_id"] if "native_id" in sess.keys() else None),
        ))
    # most recent contribution first; deterministic tie-break on sid
    rows.sort(key=lambda c: (-(c.last_ts or 0), c.sid))
    return {
        "thread_id": thread_id,
        "title": thread["title"] if "title" in thread.keys() else None,
        "goal": thread["goal"] if "goal" in thread.keys() else None,
        "repo_root": thread["repo_root"] if "repo_root" in thread.keys() else None,
        "status": thread["status"] if "status" in thread.keys() else None,
        "providers": sorted({c.provider for c in rows}),
        "contributions": [c.to_dict() for c in rows[:limit]],
        "member_count": len(rows),
    }


def summarize(store, thread_id: str, turns: int = TURNS_PER_SESSION) -> Brief:
    """One continuous-task brief for a WorkThread. Read-only, deterministic."""
    act = activity(store, thread_id, limit=100)
    b = Brief(thread_id=thread_id)
    if "error" in act:
        b.open_items = {"error": act["error"]}
        return b
    b.title, b.goal = act.get("title"), act.get("goal")
    b.repo_root, b.status = act.get("repo_root"), act.get("status")
    b.contributions = [Contribution(**c) for c in act["contributions"]]

    # recent speaking turns, newest first, per member -- then rendered oldest-first
    # so the reader follows the thread rather than reading it backwards
    picked: List[Dict[str, Any]] = []
    for c in b.contributions:
        kinds = ",".join("'%s'" % k for k in _SPEAKING_KINDS)
        got = _many(store,
                    "SELECT ts, kind, content, origin FROM events "
                    "WHERE sid=? AND kind IN (%s) AND COALESCE(content,'')<>'' "
                    "ORDER BY ts DESC, seq DESC LIMIT ?" % kinds,
                    (c.sid, turns))
        for g in got:
            picked.append({
                "sid": c.sid, "provider": c.provider, "ts": g["ts"],
                "kind": g["kind"], "origin": g["origin"],
                "text": " ".join(str(g["content"]).split())[:280],
            })
    picked.sort(key=lambda e: (e["ts"] or 0, e["sid"]))
    b.recent = picked[-(turns * max(1, len(b.contributions))):]

    cp = _latest_checkpoint(store, thread_id)
    b.checkpoint = cp

    # open items come from the checkpoint when there is one, and from the index
    # otherwise; nothing here invents a decision nobody recorded
    pending = _many(store, "SELECT * FROM thread_pending WHERE thread_id=?",
                    (thread_id,))
    b.open_items = {
        "blockers": (cp or {}).get("blockers", []),
        "pending_decisions": (cp or {}).get("decisions", []),
        "next_actions": (cp or {}).get("next_actions", []),
        "open_attaches": len([p for p in pending
                              if (p["status"] or "") == "open"]),
        "providers_that_never_contributed": [],
        "retrieval": [{"provider": c.provider, "sid": c.sid,
                       "how": "voyager show %s" % c.sid}
                      for c in b.contributions],
    }
    return b


def render(brief: Brief) -> str:
    """The human view. ASCII-safe."""
    lines: List[str] = []
    lines.append("WorkThread %s" % brief.thread_id)
    if brief.title:
        lines.append("  title : %s" % brief.title)
    if brief.goal:
        lines.append("  goal  : %s" % brief.goal)
    if brief.repo_root:
        lines.append("  repo  : %s   status: %s" % (brief.repo_root, brief.status))

    lines.append("")
    lines.append("Agents that worked here")
    if not brief.contributions:
        lines.append("  (none attached)")
    for c in brief.contributions:
        lines.append("  %-9s %-8s events=%-6d human=%-4d %s"
                     % (c.provider, c.band, c.events, c.human_turns,
                        ("last %s" % int(c.last_ts)) if c.last_ts else ""))
        if c.last_line:
            lines.append("      %s" % c.last_line)

    if brief.checkpoint:
        cp = brief.checkpoint
        lines.append("")
        lines.append("Checkpoint")
        if cp.get("phase"):
            lines.append("  phase : %s" % cp["phase"])
        for key, label in (("milestones", "done"), ("blockers", "blocked"),
                           ("decisions", "decision"), ("next_actions", "next")):
            for item in cp.get(key) or []:
                lines.append("  %-9s %s" % (label + ":", item))

    if brief.recent:
        lines.append("")
        lines.append("Recent turns (oldest first)")
        for e in brief.recent:
            lines.append("  [%s %s] %s: %s"
                         % (int(e["ts"]) if e["ts"] else "-", e["kind"],
                            e["provider"], e["text"]))

    oi = brief.open_items or {}
    if oi.get("open_attaches"):
        lines.append("")
        lines.append("Open attaches: %d" % oi["open_attaches"])
    if oi.get("error"):
        lines.append("")
        lines.append("error: %s" % oi["error"])
    return "\n".join(lines)
