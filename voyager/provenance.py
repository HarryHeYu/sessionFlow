"""Provenance: who actually produced an event.

`role` is the surface role a record carries; `origin` is who really produced it.
A provider writes injected context (AGENTS.md, skills, user_info, hook context,
turn-abort markers) as `role=user` records, so a real user turn can only be told
apart structurally -- never from the text.

Everything here is deterministic and offline.  The classifiers read the provider
record that was captured at ingest (`raw_event`) and the session-level metadata
(`sessions.raw_metadata`); nothing re-reads provider files, nothing calls a model,
and nothing rewrites anything except `events.origin`, only where it is NULL.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .model import EVENT_ORIGINS, ORIGIN_UNKNOWN

UNKNOWN = ORIGIN_UNKNOWN   # one sentinel, defined with the vocabulary in `model`


def normalise(origin: Optional[str]) -> str:
    """NULL in storage means 'not enriched' and surfaces as 'unknown'."""
    return origin if origin in EVENT_ORIGINS else UNKNOWN


# --------------------------------------------------------------------------
# Provider classifiers -- structural coordinates only
# --------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            got = json.loads(value)
        except Exception:
            return {}
        return got if isinstance(got, dict) else {}
    return {}


def codex_origin(raw_event: Any, session_raw_meta: Any = None) -> str:
    """Codex rollout record -> origin.

    Live-verified structural coordinates (4 sessions, 100% stable):

      response_item/message/role=developer            -> hook / bootstrap injection
      response_item/message/role=user + metadata.user_input_order -> real user input
      response_item/message/role=user without metadata -> AGENTS.md style injection
      event_msg/turn_aborted                          -> provider_system

    `user_input_order` alone is not enough: a subagent session submits injected
    history through the same channel, so the session source has to agree.
    """
    d = _as_dict(raw_event)
    p = _as_dict(d.get("payload"))
    meta = _as_dict(d.get("metadata"))
    typ = d.get("type")
    ptyp = p.get("type")

    if ptyp == "turn_aborted":
        return "provider_system"
    if typ == "response_item" and ptyp == "message":
        role = p.get("role")
        if role == "developer":
            # Injected context.  Codex writes both the skills bootstrap and the
            # context delivered by a lifecycle hook as role=developer with the
            # same metadata shape, so `hook_injection` is not structurally
            # distinguishable here -- both are reported as provider_bootstrap.
            # (The distinction does not affect banding: neither is human.)
            return "provider_bootstrap"
        if role == "user":
            if "user_input_order" in meta:
                return "human" if _codex_source_is_user_facing(session_raw_meta) else "provider_synthetic"
            return "provider_bootstrap"
        if role == "assistant":
            return "provider_system"
    if typ == "event_msg":
        return "provider_system"
    if typ == "session_meta":
        return "provider_system"
    return UNKNOWN


def _codex_source_is_user_facing(session_raw_meta: Any) -> bool:
    """A subagent session is not a human sitting at a terminal."""
    meta = _as_dict(session_raw_meta)
    src = meta.get("source")
    if src is None:
        return True                      # unknown source: do not invent a demotion
    if isinstance(src, str):
        return src.strip().lower() not in ("", "subagent", "internal")
    if isinstance(src, dict):
        return "subagent" not in src
    return True


def claude_origin(raw_event: Any, session_meta: Any = None) -> str:
    """Claude record -> origin.

    Claude marks meta records natively (`isMeta`); a `tool_result` carried on a
    user-role record is a tool result, not a user turn.  `session_meta` is
    accepted for a uniform classifier signature and is not needed here.

    `isSidechain` is decisive and is checked first: a sidechain record belongs to
    a spawned subagent, so its `role=user` payload is a delegation written by
    Claude itself -- a provider-generated pseudo-user record, never a turn the
    human typed.  Leaving it out would inflate `human_user_turns`, which feeds
    session banding and therefore L1 composition.

    Evidence note: no `isSidechain=true` record exists in this machine's Claude
    history, so this branch is pinned by synthesis and semantics, not by an
    observed sample.  It is deliberately NOT reported as real-world coverage.
    """
    d = _as_dict(raw_event)
    if d.get("type") != "user":
        return "provider_system" if d.get("type") else UNKNOWN
    if d.get("isSidechain"):
        return "provider_synthetic"
    content = _as_dict(d.get("message")).get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict) \
            and content[0].get("type") == "tool_result":
        return "provider_system"
    if d.get("isMeta"):
        return "provider_system"
    return "human"


def zcode_origin(raw_event: Any, session_meta: Any = None) -> str:
    """ZCode record -> origin.

    ZCode carries explicit `synthetic` / `source` flags on its message rows, and
    a `subagent_child` session is not a human at a terminal.
    """
    meta = _as_dict(session_meta)
    if meta.get("task_type") == "subagent_child" or meta.get("parent_id"):
        return "provider_synthetic"
    d = _as_dict(raw_event)
    if d.get("synthetic"):
        return "provider_synthetic"
    if d.get("source"):
        return "provider_system"
    if d.get("role") == "user":
        return "human"
    if d.get("role") in ("assistant", "tool"):
        return "provider_system"
    return UNKNOWN


CLASSIFIERS = {
    "codex": codex_origin,
    "claude": claude_origin,
    "zcode": zcode_origin,
    # grok: no message-level structural source is reachable yet -- stays unknown.
    #        tracked as GROK_MESSAGE_PROVENANCE_COVERAGE.
}


def classify(provider: str, raw_event: Any, session_raw_meta: Any = None) -> str:
    """Pure function of the stored record; never raises, never guesses."""
    fn = CLASSIFIERS.get(provider)
    if fn is None:
        return UNKNOWN
    try:
        if provider == "codex":
            return fn(raw_event, session_raw_meta)
        return fn(raw_event, session_raw_meta)
    except Exception:
        return UNKNOWN


# --------------------------------------------------------------------------
# Enrichment + coverage (store-level, offline, NULL-only)
# --------------------------------------------------------------------------

def _sessions_raw_meta(con) -> Dict[str, Any]:
    out = {}
    cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
    col = "raw_metadata" if "raw_metadata" in cols else ("metadata" if "metadata" in cols else None)
    if col is None:
        return out
    for sid, raw in con.execute("SELECT id, %s FROM sessions" % col):
        out[sid] = raw
    return out


def coverage(con) -> List[Dict[str, Any]]:
    """Per-provider provenance coverage over user-role events."""
    rows = con.execute(
        """SELECT s.provider AS provider,
                  COUNT(*) FILTER (WHERE e.kind = 'user') AS user_events,
                  COUNT(*) FILTER (WHERE e.kind = 'user' AND e.origin IS NOT NULL) AS enriched,
                  COUNT(*) FILTER (WHERE e.kind = 'user' AND e.origin = 'human') AS human
             FROM events e JOIN sessions s ON s.id = e.sid
            GROUP BY s.provider ORDER BY s.provider""").fetchall()
    out = []
    for provider, total, enriched, human in rows:
        total = total or 0
        out.append({
            "provider": provider,
            "user_events": total,
            "enriched": enriched or 0,
            "human": human or 0,
            "unknown": total - (enriched or 0),
            "coverage_pct": round(100.0 * (enriched or 0) / total, 1) if total else 0.0,
        })
    return out


def enrich(con, *, dry_run: bool = True, providers: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Fill `events.origin` where it is NULL, from the captured provider record.

    Idempotent: a second run finds nothing to fill and reports the same state.
    Deterministic: classification is a pure function of the stored record.
    Never touches id / seq / kind / role / content / membership.
    """
    wanted = set(providers) if providers else None
    session_meta = _sessions_raw_meta(con)
    rows = con.execute(
        """SELECT e.id, e.sid, e.raw_json, s.provider, e.kind
             FROM events e JOIN sessions s ON s.id = e.sid
            WHERE e.origin IS NULL""").fetchall()
    plan: List[Tuple[str, int]] = []
    per_provider: Dict[str, Dict[str, int]] = {}
    for eid, sid, raw, provider, kind in rows:
        if wanted and provider not in wanted:
            continue
        origin = classify(provider, raw, session_meta.get(sid))
        stat = per_provider.setdefault(
            provider, {"candidates": 0, "classified": 0, "unknown": 0,
                       "user_candidates": 0, "user_classified": 0})
        stat["candidates"] += 1
        if kind == "user":
            stat["user_candidates"] += 1
        if origin == UNKNOWN:
            stat["unknown"] += 1
            continue
        stat["classified"] += 1
        if kind == "user":
            stat["user_classified"] += 1
        plan.append((origin, eid))
    applied = 0
    if not dry_run and plan:
        con.executemany("UPDATE events SET origin = ? WHERE id = ? AND origin IS NULL", plan)
        con.commit()
        applied = len(plan)
    return {
        "dry_run": dry_run,
        "candidates": sum(s["candidates"] for s in per_provider.values()),
        "classified": sum(s["classified"] for s in per_provider.values()),
        "left_unknown": sum(s["unknown"] for s in per_provider.values()),
        "applied": applied,
        "per_provider": per_provider,
    }


# --------------------------------------------------------------------------
# L1 composition: band priority (Phase P2)
# --------------------------------------------------------------------------

def session_band(con, sid: str) -> Tuple[str, Dict[str, int]]:
    """Classify one session for L1 composition.

    ``human > 0`` is positive evidence and may promote.  ``human == 0`` is a
    *negative* claim ("this session holds no human work"), so it may only become
    BOOTSTRAP_ONLY when every relevant user event in **this session** was
    classified -- one unclassified event means absence cannot be claimed and the
    session is UNKNOWN instead.  Provider-level coverage is never a substitute.

    Read-only and offline: a pure function of the stored events.
    """
    evs = con.execute(
        "SELECT seq, kind, origin, tool_name FROM events WHERE sid=? ORDER BY seq, id",
        (sid,)).fetchall()
    # Count over *all* user events.  A work-boundary filter would be wrong: a
    # normal "user asks once, agent works" session has its human turn before the
    # first assistant event, and dropping that would misclassify it as
    # BOOTSTRAP_ONLY.  Only `origin` decides what a user event really is.
    relevant = [e for e in evs if e["kind"] == "user"]
    human = sum(1 for e in relevant if e["origin"] == "human")
    unknown = sum(1 for e in relevant if e["origin"] is None)
    nonhuman = len(relevant) - human - unknown
    distinct = len({e["tool_name"] for e in evs
                    if e["kind"] == "tool_call" and e["tool_name"]})
    if human == 0:
        band = "BOOTSTRAP_ONLY" if unknown == 0 else "UNKNOWN"
    elif human >= 2 or distinct >= 2:
        band = "STRONG"
    else:
        band = "WEAK"
    return band, {"human": human, "unknown": unknown, "nonhuman": nonhuman,
                  "distinct_tools": distinct, "relevant_user_events": len(relevant)}


def order_rows_for_l1(con, rows) -> List[Any]:
    """Order L1 candidate rows by band for the existing turn-packing builder.

    ``build_working_context`` serializes turns oldest-first and packs
    newest-first, so this returns the rows in **reverse priority order**: the
    band that must be packed first goes last.  The packing that results is

        newest WEAK turn -> newest UNKNOWN turn -> STRONG -> leftovers

    which is the approved first version: confirmed-but-thin work keeps a
    footprint, an unclassified provider is never starved, and explicit STRONG
    work takes whatever remains.  BOOTSTRAP_ONLY never contributes.

    Canonical ``thread_sessions.ord`` is preserved inside every band (rows
    arrive in that order) and no timestamp is consulted.  Pure and read-only.
    """
    buckets: Dict[str, List[Any]] = {"STRONG": [], "UNKNOWN": [], "WEAK": []}
    for row in rows:
        band, _ = session_band(con, row["id"])
        if band in buckets:
            buckets[band].append(row)
    return buckets["STRONG"] + buckets["UNKNOWN"] + buckets["WEAK"]
