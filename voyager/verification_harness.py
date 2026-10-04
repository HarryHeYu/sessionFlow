"""Automatic live verification: append-only evidence, derived state.

A provider is only *live verified* when this machine has observed it doing the
thing, and the way to make that claim trustworthy is to make it impossible to
write the claim directly.  So:

  * the only persisted truth is `verification_events`, an append-only table of
    facts -- "the hook ran", "context was delivered", "the session id appeared",
    "an attach was pending", "the attach resolved"
  * every state (`LIVE_VERIFIED`, `ZERO_TOUCH_LIVE_VERIFIED`) is **derived** from
    those facts at read time; nothing stores a status
  * `voyager verify` is strictly read-only.  It cannot promote anything, and no
    `get_or_create`-shaped API exists for it to misuse.

Three further rules make the derivation honest:

  **Per chain, never per provider.**  Evidence is grouped by `correlation_id`,
  which ties a lifecycle together *before* a native session id exists (the hook
  knows its own invocation; the resolution happens later, in a scan).  A hook in
  one session and a resolution in another must never combine into a zero-touch
  claim.

  **Ordered.**  A chain proves zero-touch only if the attach resolved *after* the
  hook and the context, and the session id was known before the attach.  Events
  are ordered by `observed_at` with `event_id` as a deterministic tie-break.

  **Idempotent.**  `event_id` is a hash of the facts, so a replayed hook or a
  watcher observing the same thing twice is ignored by the primary key rather
  than inflating the record.

`ATTACH_PENDING` is allowed in a chain but never required: some providers attach
directly, and Codex's real path goes through pending.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --- vocabulary -------------------------------------------------------------
#
# State names are constants, not literals sprinkled through the code: a typo in
# a state name is exactly how a promotion silently stops working.

NOT_FOUND = "NOT_FOUND_IN_CURRENT_AUDIT"
SUPPORTED = "SUPPORTED"
CONFIGURED = "CONFIGURED"
UNIT_VERIFIED = "UNIT_VERIFIED"
LIVE_VERIFIED = "LIVE_VERIFIED"
ZERO_TOUCH_LIVE_VERIFIED = "ZERO_TOUCH_LIVE_VERIFIED"

STATE_ORDER = {
    NOT_FOUND: 0, SUPPORTED: 1, CONFIGURED: 2,
    UNIT_VERIFIED: 3, LIVE_VERIFIED: 4, ZERO_TOUCH_LIVE_VERIFIED: 5,
}

#: Facts.  A chain of these is the only thing that can promote a provider.
HOOK_TRIGGERED = "HOOK_TRIGGERED"
CONTEXT_PREPARED = "CONTEXT_PREPARED"        # Context successfully compiled
CONTEXT_EMITTED = "CONTEXT_EMITTED"          # Protocol payload written to stdout
NATIVE_SESSION_ID_OBSERVED = "NATIVE_SESSION_ID_OBSERVED"
ATTACH_PENDING = "ATTACH_PENDING"
ATTACH_RESOLVED = "ATTACH_RESOLVED"

EVENT_TYPES = (
    HOOK_TRIGGERED,
    CONTEXT_PREPARED,
    CONTEXT_EMITTED,
    NATIVE_SESSION_ID_OBSERVED,
    ATTACH_PENDING,
    ATTACH_RESOLVED,
)

#: `CONTEXT_PREPARED` means startup_continuity() successfully compiled context.
#: `CONTEXT_EMITTED` means emit() successfully wrote protocol payload to stdout.
#: Only HOOK + CONTEXT_EMITTED proves LIVE_VERIFIED.

#: `ZERO_TOUCH_CONFIRMED` is deliberately **not** an event: it is a conclusion,
#: and persisting conclusions next to facts is how a system starts proving
#: itself.  It is derived like every other state.

TABLE = "verification_events"

SCHEMA = """
CREATE TABLE IF NOT EXISTS verification_events (
    event_id          TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    correlation_id    TEXT NOT NULL,
    native_session_id TEXT,
    thread_id         TEXT,
    source_session_id TEXT,
    observed_at       REAL NOT NULL,
    payload_json      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_verification_events_chain
    ON verification_events(provider, correlation_id);
CREATE INDEX IF NOT EXISTS idx_verification_events_session
    ON verification_events(provider, native_session_id);
CREATE INDEX IF NOT EXISTS idx_verification_events_time
    ON verification_events(provider, observed_at);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the evidence table if absent. Idempotent; touches nothing else."""
    con.executescript(SCHEMA)


def log_dir() -> Path:
    override = os.environ.get("VOYAGER_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".voyager" / "logs"


def _db_path(explicit: Optional[Path] = None) -> Optional[Path]:
    """Where the evidence lives.

    `VOYAGER_DB` wins when set.  That is not a convenience: a test that drives a
    handler with a synthetic payload must not write that payload into the real
    index as if the provider had fired, and an explicit override is the only way
    to guarantee it.
    """
    if explicit is not None:
        return Path(explicit)
    override = os.environ.get("VOYAGER_DB")
    if override:
        return Path(override).expanduser()
    try:
        from .store import default_db_path
        return Path(default_db_path())
    except Exception:
        return None


def _connect(create: bool = False, db_path: Optional[Path] = None) -> Optional[sqlite3.Connection]:
    """Open the index. `create=False` means strictly read-only.

    A read path must not issue DDL: `voyager verify` has to leave the database
    byte-identical, and creating the evidence table on first look would make the
    command a writer.  When the table is absent the honest answer is "no evidence
    yet", not "here is a new table".
    """
    path = _db_path(db_path)
    if not path or not path.exists():
        return None
    try:
        if create:
            con = sqlite3.connect(str(path))
            con.row_factory = sqlite3.Row
            ensure_schema(con)
            return con
        # A read path opens read-only, so "verify writes nothing" is enforced by
        # SQLite rather than by careful coding.
        uri = "file:%s?mode=ro" % str(path).replace("\\", "/").replace("?", "%3f")
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        # a read-only open can fail on an unusual path; fall back to a normal
        # handle, which the read queries still never write through
        try:
            con = sqlite3.connect(str(path))
            con.row_factory = sqlite3.Row
            return con
        except Exception:
            return None


def has_evidence_table(con: sqlite3.Connection) -> bool:
    try:
        return bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,)).fetchone())
    except Exception:
        return False


# --- recording (append-only) ------------------------------------------------

def event_id(provider: str, event_type: str, correlation_id: str,
             native_session_id: Optional[str], source_session_id: Optional[str],
             observed_at: float) -> str:
    """A deterministic identity for one fact.

    Exact duplicate insertion of the same observed fact is idempotent.
    Distinct observations at different timestamps are distinct events --
    there is no stable provider invocation id to collapse them, so the
    identity is a hash of what happened *including* when we got around to
    storing it (`observed_at` quantised to the millisecond, because two
    observations of one event can differ in the sub-millisecond noise of a
    clock read).
    """
    basis = "|".join([
        provider or "", event_type or "", correlation_id or "",
        native_session_id or "", source_session_id or "",
        "%.3f" % float(observed_at),
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def new_correlation_id(provider: str, *, cwd: Optional[str] = None,
                       native_session_id: Optional[str] = None) -> str:
    """The id that ties a lifecycle together before a session id exists.

    The hook knows its provider, its repository and the moment it ran; the
    resolution happens later, in a scan.  Those facts are enough to name the
    chain, and the id is short enough to put in a pending record.
    """
    basis = "|".join([provider or "", cwd or "", native_session_id or "",
                      "%.3f" % time.time(), str(os.getpid())])
    return "vc_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def record_event(provider: str, event_type: str, correlation_id: str, *,
                 native_session_id: Optional[str] = None,
                 thread_id: Optional[str] = None,
                 source_session_id: Optional[str] = None,
                 observed_at: Optional[float] = None,
                 payload: Optional[Dict[str, Any]] = None,
                 con: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """Append one fact. Returns its id, or None when it was a replay.

    Best effort and never raises: verification is observation, and an observer
    that breaks the thing it observes is worse than no observer.
    """
    if event_type not in EVENT_TYPES:
        return None
    ts = float(observed_at if observed_at is not None else time.time())
    eid = event_id(provider, event_type, correlation_id, native_session_id,
                   source_session_id, ts)
    own = con is None
    try:
        con = con or _connect(create=True)
        if con is None:
            return None
        ensure_schema(con)
        cur = con.execute(
            "INSERT OR IGNORE INTO %s (event_id, provider, event_type, "
            "correlation_id, native_session_id, thread_id, source_session_id, "
            "observed_at, payload_json) VALUES (?,?,?,?,?,?,?,?,?)" % TABLE,
            (eid, provider, event_type, correlation_id, native_session_id,
             thread_id, source_session_id, ts,
             json.dumps(payload or {}, ensure_ascii=False, default=str)),
        )
        con.commit()
        return eid if cur.rowcount else None
    except Exception:
        return None
    finally:
        if own and con is not None:
            try:
                con.close()
            except Exception:
                pass


# --- derivation (pure, read-only) -------------------------------------------

@dataclass
class Chain:
    """One lifecycle: the facts observed under a single correlation id."""

    correlation_id: str
    provider: str
    events: List[Dict[str, Any]] = field(default_factory=list)

    def types(self) -> List[str]:
        return [e["event_type"] for e in self.events]

    def has(self, event_type: str) -> bool:
        return any(e["event_type"] == event_type for e in self.events)

    def first_index(self, event_type: str) -> Optional[int]:
        for i, e in enumerate(self.events):
            if e["event_type"] == event_type:
                return i
        return None

    def session_ids(self) -> List[str]:
        return sorted({e["native_session_id"] for e in self.events
                       if e.get("native_session_id")})

    def status(self) -> str:
        """The state this chain proves.
        
        Only HOOK_TRIGGERED + CONTEXT_EMITTED (actually written to stdout) proves
        LIVE_VERIFIED.  CONTEXT_PREPARED alone is insufficient because emit() can
        fail after context is compiled.
        
        Ordering: hook precedes emitted context, attach resolves after both.
        Session id must be known no later than resolution, and all events in same
        chain must agree on session identity.
        """
        i_hook = self.first_index(HOOK_TRIGGERED)
        i_ctx = self.first_index(CONTEXT_EMITTED)
        i_sid = self.first_index(NATIVE_SESSION_ID_OBSERVED)
        i_res = self.first_index(ATTACH_RESOLVED)

        if i_hook is None or i_ctx is None or i_hook > i_ctx:
            return UNIT_VERIFIED          # hook ran, but nothing emitted
        if i_res is None or i_sid is None:
            return LIVE_VERIFIED          # emitted, but nothing attached
        if i_sid > i_res:
            return LIVE_VERIFIED          # session id came too late
        if i_ctx > i_res:
            return LIVE_VERIFIED          # resolved before emit
        if len(self.session_ids()) > 1:
            return LIVE_VERIFIED          # inconsistent session ids
        return ZERO_TOUCH_LIVE_VERIFIED


def _ordered(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    events = [dict(r) for r in rows]
    events.sort(key=lambda e: (float(e.get("observed_at") or 0.0),
                               str(e.get("event_id") or "")))
    return events


def chains_for(con: sqlite3.Connection, provider: Optional[str] = None
               ) -> Dict[Tuple[str, str], Chain]:
    """Every chain, keyed by (provider, correlation_id).  Read-only."""
    if not has_evidence_table(con):
        return {}
    sql = "SELECT * FROM %s" % TABLE
    args: Tuple[Any, ...] = ()
    if provider:
        sql += " WHERE provider=?"
        args = (provider,)
    out: Dict[Tuple[str, str], Chain] = {}
    for e in _ordered(con.execute(sql, args)):
        key = (e["provider"], e["correlation_id"])
        out.setdefault(key, Chain(correlation_id=e["correlation_id"],
                                  provider=e["provider"])).events.append(e)
    return out


def chain_ids_for_session(con: sqlite3.Connection, provider: str,
                          native_session_id: str) -> List[str]:
    """Every correlation chain a native session id appears in.

    Codex's pending attach resolves in a scan, long after the hook returned, so
    the resolver has to recover the chain from the session id rather than from an
    in-process variable.  The distinct-id list is what makes the resolution
    safe: one chain resolves cleanly; more than one is ambiguous and must be
    refused rather than silently picking the newest observation.
    """
    if not native_session_id or not has_evidence_table(con):
        return []
    try:
        rows = con.execute(
            "SELECT DISTINCT correlation_id FROM %s WHERE provider=? AND "
            "native_session_id=? ORDER BY observed_at" % TABLE,
            (provider, native_session_id)).fetchall()
        return [r["correlation_id"] for r in rows]
    except Exception:
        return []


def observed_state(con: sqlite3.Connection, provider: str) -> Dict[str, Any]:
    """What the evidence proves for one provider, plus the supporting detail."""
    chains = [c for (p, _), c in chains_for(con, provider).items()]
    best = UNIT_VERIFIED
    best_chain: Optional[Chain] = None
    for c in chains:
        st = c.status()
        if STATE_ORDER[st] > STATE_ORDER[best]:
            best, best_chain = st, c
    events = [e for c in chains for e in c.events]
    last = max((float(e["observed_at"]) for e in events), default=None)
    return {
        "observed_state": best if events else None,
        "chains": len(chains),
        "evidence_count": len(events),
        "last_live_event": last,
        "best_chain": best_chain.correlation_id if best_chain else None,
    }


def query_status(provider: Optional[str] = None,
                 db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Declared vs observed vs effective. Read-only; writes nothing.

    Three layers, kept apart on purpose so a reader can tell "implemented"
    from "seen working":

    * **declared** -- the ceiling from :data:`DECLARED`: the strongest state
      any machine-dependent dimension supports in the code.
    * **observed** -- what the evidence table proves (None if no evidence).
    * **effective** -- the weaker of declared and observed.  When there is no
      evidence, effective is capped at UNIT_VERIFIED for machine-dependent
      dimensions, because "code exists and is tested" is all the evidence
      supports without a recorded observation.
    """
    from .capability_matrix import (
        PROVIDERS, DECLARED, STATE_ORDER as _SO,
    )

    _MACHINE_DEPENDENT = frozenset({
        "startup_hook", "dynamic_context_injection",
        "native_session_id_at_start", "native_auto_attach",
        "live_zero_touch_continuity",
    })

    providers = [provider] if provider else list(PROVIDERS)
    out: Dict[str, Any] = {"providers": {}}
    con = _connect(db_path=db_path)
    try:
        for p in providers:
            declared = max(
                (DECLARED[p][d][0] for d in _MACHINE_DEPENDENT
                 if d in DECLARED.get(p, {})),
                key=lambda s: _SO.get(s, 0),
                default=NOT_FOUND,
            )
            if con is None:
                obs = {"observed_state": None, "chains": 0, "evidence_count": 0,
                       "last_live_event": None, "best_chain": None}
            else:
                obs = observed_state(con, p)
            observed = obs["observed_state"]
            if observed is None:
                effective = (UNIT_VERIFIED
                             if _SO.get(declared, 0) > _SO.get(UNIT_VERIFIED, 0)
                             else declared)
            else:
                effective = (observed if _SO.get(observed, 0)
                             < _SO.get(declared, 0) else declared)
            out["providers"][p] = {
                "declared_state": declared,
                "observed_state": obs["observed_state"],
                "effective_state": effective,
                "chains": obs["chains"],
                "evidence_count": obs["evidence_count"],
                "last_live_event": obs["last_live_event"],
                "best_chain": obs["best_chain"],
            }
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    return out


def provider_effective_state(provider: str) -> str:
    """The state to publish for one provider. Read-only."""
    return query_status(provider)["providers"][provider]["effective_state"]


# --- hook wiring ------------------------------------------------------------
#
# These are the only writers.  A handler calls `begin_hook` when it starts and
# `note_result` once the core returns, which is what turns a real provider
# lifecycle into evidence.

def begin_hook(provider: str, *, cwd: Optional[str] = None,
               native_session_id: Optional[str] = None) -> Optional[str]:
    """Record that a provider hook ran; returns the chain id.

    Call this from the handler itself -- the fact that the code is executing *is*
    the evidence that the provider fired its hook.
    """
    cid = new_correlation_id(provider, cwd=cwd,
                             native_session_id=native_session_id)
    record_event(provider, HOOK_TRIGGERED, cid,
                 native_session_id=native_session_id,
                 payload={"cwd": cwd})
    return cid


def note_result(provider: str, correlation_id: Optional[str], result: Any,
                *, native_session_id: Optional[str] = None) -> None:
    """Record what the continuity core produced for this chain. Never raises.
    
    Called immediately after startup_continuity() returns successfully.
    Records CONTEXT_PREPARED (not CONTEXT_EMITTED - that comes from emit()).
    
    The mapping is about *outcomes*, not about one status string:
    
      context available              -> CONTEXT_PREPARED
      a native session id is known   -> NATIVE_SESSION_ID_OBSERVED
      attach_status pending_resolve  -> ATTACH_PENDING
      attach_status already/auto     -> ATTACH_RESOLVED
    """
    if not correlation_id:
        return
    try:
        context = getattr(result, "context", None)
        sid = native_session_id or getattr(result, "current_session", None)
        if context:
            record_event(provider, CONTEXT_PREPARED, correlation_id,
                         native_session_id=sid, payload={"chars": len(context)})
        if sid:
            record_event(provider, NATIVE_SESSION_ID_OBSERVED, correlation_id,
                         native_session_id=sid)
        status = (getattr(result, "attach_status", None) or "")
        if status == "pending_resolve":
            record_event(provider, ATTACH_PENDING, correlation_id,
                         native_session_id=sid)
        elif status in ("already_attached", "auto_attached"):
            record_event(provider, ATTACH_RESOLVED, correlation_id,
                         native_session_id=sid,
                         thread_id=getattr(result, "thread_id", None),
                         payload={"attach_status": status})
    except Exception:
        pass


def note_attach_resolved_for_session(provider: str,
                                     native_session_id: Optional[str],
                                     *, thread_id: Optional[str] = None,
                                     source_session_id: Optional[str] = None) -> bool:
    """Record a resolution found from the session id alone.

    The resolution runs in a scan, in a different process from the hook that
    opened the chain, so the correlation id is not in hand -- it has to be
    recovered from the session id.  Returns True when a chain was found and the
    evidence recorded.
    """
    if not native_session_id:
        return False
    con = _connect()
    if con is None:
        return False
    try:
        cids = chain_ids_for_session(con, provider, native_session_id)
    finally:
        try:
            con.close()
        except Exception:
            pass
    if not cids:
        return False                       # no chain: nothing to resolve
    if len(cids) > 1:
        # ambiguous: the same native session id appears under more than one
        # correlation chain.  Resolving would silently pick one lifecycle and
        # fabricate evidence -- refuse instead, and let a human reconcile.
        return False
    record_event(provider, ATTACH_RESOLVED, cids[0],
                 native_session_id=native_session_id, thread_id=thread_id,
                 source_session_id=source_session_id)
    return True


def note_attach_resolved(provider: str, correlation_id: str, *,
                         native_session_id: Optional[str] = None,
                         thread_id: Optional[str] = None,
                         source_session_id: Optional[str] = None) -> None:
    """Record the *later* half of a pending lifecycle, from the resolver.

    Codex's real path is SessionStart -> pending -> the native session is
    discovered and indexed -> the pending resolves.  That last step happens in a
    scan, not in the hook, so it needs its own entry point -- and it must pass the
    correlation id through, or the chain would look like it never resolved.
    """
    record_event(provider, ATTACH_RESOLVED, correlation_id,
                 native_session_id=native_session_id, thread_id=thread_id,
                 source_session_id=source_session_id)


# --- CLI (read-only) --------------------------------------------------------

def cmd_verify(provider: Optional[str] = None, verbose: bool = False,
               json_output: bool = False, db_path: Optional[Path] = None,
               matrix: bool = False) -> int:
    """`voyager verify` -- strictly read-only.

    It queries evidence and derives state; it never records, ensures or creates
    anything.  A provider with no evidence shows its declared state and an empty
    observation, which is the honest answer.
    """
    status = query_status(provider, db_path=db_path)

    # Matrix mode: full provider-by-dimension matrix from capability_matrix
    if matrix:
        from .capability_matrix import matrix as cap_matrix, DIMENSIONS, PROVIDERS
        m = cap_matrix(provider and [provider] or None)
        if json_output:
            print(json.dumps(m, indent=2, ensure_ascii=False, default=str))
            return 0
        print("provider      " + "  ".join(d[:12] for d in DIMENSIONS))
        print("-" * (13 + 14 * len(DIMENSIONS)))
        for p in (m if provider is None else [provider]):
            if p not in m:
                continue
            cells = "  ".join(m[p][d]["state"][:12] for d in DIMENSIONS)
            print("%-13s %s" % (p, cells))
        return 0

    if json_output:
        print(json.dumps(status, indent=2, ensure_ascii=False, default=str))
        return 0

    if verbose:
        print("provider      declared                    observed                    effective")
        print("-" * 88)
        for p, v in status["providers"].items():
            print("%-13s %-27s %-27s %s" % (
                p, v["declared_state"],
                v["observed_state"] or "(no evidence)",
                v["effective_state"]))
        print("")
        print("evidence: chains = lifecycles seen, events = facts recorded.")
        for p, v in status["providers"].items():
            print("  %-13s chains=%-4s events=%-5s last=%s  chain=%s" % (
                p, v["chains"], v["evidence_count"],
                ("%.0f" % v["last_live_event"]) if v["last_live_event"] else "-",
                v["best_chain"] or "-"))
        return 0

    for p, v in status["providers"].items():
        print("%-13s %s" % (p, v["effective_state"]))
    return 0


def note_context_emitted(provider: str, correlation_id: Optional[str],
                         nbytes: int) -> None:
    """Record that emit() successfully wrote to stdout.
    
    This is called from handler emit() paths **after** json.dumps() + sys.stdout.write()
    + flush() succeeds without raising an exception. Never raises.
    """
    if not correlation_id:
        return
    try:
        record_event(provider, CONTEXT_EMITTED, correlation_id, payload={"bytes": nbytes})
    except Exception:
        pass


def note_context_prepared(provider: str, correlation_id: Optional[str]) -> None:
    """Record that context was compiled in the handler before emit().
    
    Called from handle_*_session_start paths **after** startup_continuity() returns
    a result with non-empty context, but before write() to stdout. Never raises.
    """
    if not correlation_id:
        return
    try:
        record_event(provider, CONTEXT_PREPARED, correlation_id)
    except Exception:
        pass
