"""Programmatic verification of an Evidence Reference.

A Continuation Bundle cites an event as ``seq N · #digest``.  Until now that
pointer could only be compared by a human: open the bundle, open ``voyager
show``, read the two lines, squint.  This module answers the question a machine
can ask:

    is the event this reference points at still the event it was made from?

It cannot answer the question a human actually cares about -- *is the claim
true?* -- and it must never be read as if it could.  A matching digest means
"this is the message the reference was recorded from", nothing more.  The
distinction is why ``MATCH`` covers content identity only, and why a missing
original source is reported separately rather than folded into it.

"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from .util import event_digest

#: The five outcomes.  They are kept apart because a caller can act on them
#: differently: a MISMATCH means the transcript was rewritten under the
#: reference, a NOT_FOUND means it is gone, and an UNVERIFIABLE one means the
#: reference was never specific enough to check.  Collapsing them into one
#: "invalid reference" error would force every caller to re-derive the
#: difference by hand.
MATCH = "MATCH"
MISMATCH = "MISMATCH"
NOT_FOUND = "NOT_FOUND"
UNVERIFIABLE = "UNVERIFIABLE"
INVALID_REFERENCE = "INVALID_REFERENCE"

#: Exit codes, one per outcome.  Stable, because shell callers and CI pin them.
EXIT_CODES = {
    MATCH: 0,
    MISMATCH: 1,
    NOT_FOUND: 2,
    UNVERIFIABLE: 3,
    INVALID_REFERENCE: 4,
}

_DIGEST_RE = re.compile(r"^[0-9a-f]{8}$")


def parse_digest(value: Optional[str]) -> Optional[str]:
    """Normalise a digest, or return None when it cannot be one.

    Accepts ``c71c180a`` and ``#c71c180a`` -- the bundle prints the latter, and
    a human copying it will paste the hash with it.
    """
    if not value:
        return None
    candidate = value.strip().lstrip("#").lower()
    return candidate if _DIGEST_RE.match(candidate) else None


def _source_stat(store, session_row) -> dict:
    """Compare the source file's live stat against what the scan recorded.

    The scanner already stores ``mtime`` and ``size`` per source; nothing read
    them back.  Doing so turns "the index still matches" into the stronger
    statement "the file the index was built from has not been touched since",
    which is the closest thing to original-source integrity that is available
    without hashing every provider file on every scan.

    Cost is one ``os.stat`` -- no open, no read, no write.  Its limits are the
    point of returning the individual signals rather than a verdict: a touch
    changes mtime without changing bytes, and an edit can keep the size.  So a
    mismatch means "possibly changed", never "changed", and a match means
    "not demonstrably changed", never "unchanged".
    """
    out = {"recorded_mtime": None, "recorded_size": None,
           "current_mtime": None, "current_size": None,
           "path": None, "exists": None,
           "mtime_matches": None, "size_matches": None}
    try:
        src = store.con.execute(
            "SELECT path, mtime, size FROM sources WHERE sid=? "
            "ORDER BY last_seen DESC LIMIT 1", (session_row["id"],)).fetchone()
    except Exception:
        return out
    if src is None:
        return out
    path, mtime, size = src[0], src[1], src[2]
    out["path"] = path
    out["recorded_mtime"], out["recorded_size"] = mtime, size
    try:
        st = os.stat(path)
    except OSError:
        out["exists"] = False
        return out
    out["exists"] = True
    out["current_mtime"], out["current_size"] = st.st_mtime, st.st_size
    out["mtime_matches"] = (mtime is not None and
                            abs(st.st_mtime - mtime) < 1e-6)
    out["size_matches"] = (size is not None and st.st_size == size)
    return out


def verify(store, session_id: str, seq: Any,
           digest: Optional[str] = None) -> Dict[str, Any]:
    """Check one reference against the index.

    ``store`` must be opened read-only; this function writes nothing, scans
    nothing and runs nothing.  The returned dict always carries ``status``,
    ``exit_code`` and ``source_available`` so a caller can require *both*
    a content match and a live source before trusting a reference.
    """
    result: Dict[str, Any] = {
        "status": None,
        "exit_code": None,
        "session_id": session_id,
        "seq": seq,
        "digest": None,
        "found_digest": None,
        "source_available": None,
        "message": "",
    }

    # ---- the reference itself -------------------------------------------------
    try:
        seq_int = int(seq)
    except (TypeError, ValueError):
        result["status"] = INVALID_REFERENCE
        result["message"] = ("sequence %r is not an integer; a reference is "
                             "(session, seq, digest)" % (seq,))
        return _finish(result)

    if digest is not None and parse_digest(digest) is None:
        result["status"] = INVALID_REFERENCE
        result["message"] = ("digest %r is not 8 hex characters; a reference is "
                            "(session, seq, digest)" % (digest,))
        return _finish(result)

    safe_digest = parse_digest(digest)
    result["digest"] = safe_digest
    result["seq"] = seq_int

    # ---- the session ----------------------------------------------------------
    row = None
    try:
        for candidate in store.con.execute(
                "SELECT * FROM sessions WHERE id=? OR native_id=? "
                "OR provider||':'||native_id=?",
                (session_id, session_id, session_id)):
            row = candidate
            break
    except Exception:
        row = None
    if row is None:
        result["status"] = NOT_FOUND
        result["message"] = ("no indexed session matches %r -- it was never "
                             "scanned, or the index was rebuilt without it"
                             % (session_id,))
        return _finish(result)

    result["session"] = dict(row)
    result["source_available"] = row["source_state"] == "ACTIVE_SOURCE"
    result["source_stat"] = _source_stat(store, row)

    # ---- the event ------------------------------------------------------------
    found = None
    for ev in store.events(row["id"]):
        if ev["seq"] == seq_int:
            found = ev
            break
    if found is None:
        result["status"] = NOT_FOUND
        result["message"] = ("session %r has no event at seq %d -- the "
                             "transcript was rewritten and that position now "
                             "holds something else, or never did"
                             % (session_id, seq_int))
        return _finish(result)

    result["found_digest"] = event_digest(
        found["kind"], found["content"], found["command"],
        found["tool_output"], found["tool_call_id"], found["exit_code"])

    if safe_digest is None:
        # Without a digest the only thing checkable is the slot, which is
        # precisely what is *not* stable.  Report what is there so the caller
        # can adopt a digest, but do not call it verified.
        result["status"] = UNVERIFIABLE
        result["message"] = ("no digest given, so only the position was "
                             "checked -- and a position is exactly what a "
                             "mid-transcript edit renumbers.  The event here "
                             "is #%s; verify against that to pin it."
                             % result["found_digest"])
        return _finish(result)

    result["found_kind"] = found["kind"]
    result["found_summary"] = _summarise(found)
    if safe_digest == result["found_digest"]:
        result["status"] = MATCH
        result["message"] = "the event at seq %d is the one the reference named" % seq_int
        if not result["source_available"]:
            result["message"] += (
                " — but its source is %s, so this confirms the *indexed* copy "
                "only; the original transcript can no longer be re-read"
                % row["source_state"])
    else:
        result["status"] = MISMATCH
        result["message"] = ("seq %d now holds a different event: recorded "
                             "#%s, found #%s -- the transcript was rewritten "
                             "under this reference"
                             % (seq_int, safe_digest,
                                result["found_digest"]))
    return _finish(result)


def _summarise(ev) -> str:
    """One line saying what kind of event is at a position, and what it was.

    A MISMATCH that says only "the hash differs" leaves the reader unable to
    judge the drift: a position that moved from a tool result to a different
    tool result is a different problem from one that now holds a greeting.
    """
    kind = ev["kind"]
    if ev["command"]:
        body = str(ev["command"])
    elif ev["content"]:
        body = str(ev["content"])
    elif ev["tool_output"]:
        body = str(ev["tool_output"])
    else:
        body = ""
    body = " ".join(body.split())
    if len(body) > 100:
        body = body[:100] + "…"
    if ev["exit_code"] is not None and kind == "tool_result":
        return "%s (exit %s): %s" % (kind, ev["exit_code"], body)
    return ("%s: %s" % (kind, body)) if body else kind


def _finish(result: Dict[str, Any]) -> Dict[str, Any]:
    result["exit_code"] = EXIT_CODES[result["status"]]
    return result


def render(result: Dict[str, Any]) -> str:
    """Human-readable rendering.  Never shorter than the status warrants."""
    lines = []
    head = {
        MATCH: "MATCH",
        MISMATCH: "MISMATCH",
        NOT_FOUND: "NOT_FOUND",
        UNVERIFIABLE: "UNVERIFIABLE",
        INVALID_REFERENCE: "INVALID REFERENCE",
    }[result["status"]]
    lines.append("%s  %s" % (head, result["message"]))

    session = result.get("session")
    if session:
        lines.append("  session : %s:%s" % (session["provider"],
                                            session["native_id"]))
        if not result["source_available"]:
            lines.append("  source  : %s — retained history: searchable, "
                         "not re-checkable against the original" %
                         session["source_state"])
    stat = result.get("source_stat") or {}
    if stat.get("exists") is False:
        lines.append("  file    : no longer on disk at the recorded path")
    elif stat.get("exists") is True:
        changed = []
        if stat.get("mtime_matches") is False:
            changed.append("mtime")
        if stat.get("size_matches") is False:
            changed.append("size")
        if changed:
            lines.append("  file    : %s differ from what the scan recorded — "
                         "the transcript may have changed since it was indexed"
                         % " and ".join(changed))
        elif stat.get("mtime_matches") is True:
            lines.append("  file    : same size and mtime as when it was "
                         "indexed (not proof of identical bytes; see "
                         "`voyager evidence` limits in docs)")
    if result.get("found_digest"):
        lines.append("  found   : seq %s  #%s" % (result["seq"],
                                                 result["found_digest"]))
        if result.get("found_kind"):
            lines.append("  there   : %s" % result["found_summary"])
        lines.append("  cited   : seq %s  #%s" % (result["seq"],
                                                 result["digest"] or "-"))
    lines.append("")
    lines.append("  MATCH means the indexed event is the one the reference was")
    lines.append("  made from.  It does not mean the claim it carries is true.")
    return "\n".join(lines)
