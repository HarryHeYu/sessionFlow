"""Small helpers shared across layers.

One home for the tiny functions that used to be copy-pasted into several
modules and then silently drifted:

* ``_same_repo`` existed in five places with **two different behaviours** —
  ``startup`` matched only exact/suffix paths, while ``api`` / ``auto`` /
  ``cli`` also matched by substring.  Same name, different meaning: a repo
  that one code path considered "the same" another did not.
* ``_fmt_ts`` existed in four places with two formats — one with seconds and
  a timezone conversion (``export``), the rest without.

Copy-paste plus a "close enough" edit is how two call sites end up disagreeing
about what "the same repo" means.  The fix is a single definition with the
variants *named*, so a reader can tell which one a call site wants instead of
having to read the body.

Nothing here imports anything but the standard library, so every layer —
``store``, ``continuity``, ``cli``, ``api``, the startup handlers — can use it
without an import cycle.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional


def _norm_repo(p: Optional[str]) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()


def same_repo(a: Optional[str], b: Optional[str]) -> bool:
    """Strict repo identity: equal, or one is a path-suffix of the other.

    Case- and separator-insensitive.  ``E:/code/voyager`` matches
    ``voyager`` and ``/code/voyager``, but **not** ``code`` and **not**
    ``E:/code/voyager_old``.

    Use this where a *false match* is harmful — e.g. deciding whether two
    active WorkThreads belong to the same repo before auto-attaching a
    session.  Over-matching there attaches a session to the wrong thread.
    """
    a = _norm_repo(a)
    b = _norm_repo(b)
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def same_repo_loose(a: Optional[str], b: Optional[str]) -> bool:
    """``same_repo`` **plus** raw substring containment.

    User-facing matching: a partial name typed on the command line must match a
    full path stored in the index — ``code`` must match ``E:/code/voyager``,
    which the strict :func:`same_repo` rejects because it is not a path suffix.
    Looser than :func:`same_repo`, so use it only where over-matching is
    acceptable (displaying, filtering, ranking) and never where it would
    authorise a write or an attach.
    """
    a = _norm_repo(a)
    b = _norm_repo(b)
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a) or a in b or b in a


def fmt_ts(ts: Optional[float]) -> str:
    """``YYYY-MM-DD HH:MM`` in local time; ``?`` for a missing timestamp."""
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_ts_seconds(ts: Optional[float]) -> str:
    """``YYYY-MM-DD HH:MM:SS`` in local time; ``?`` for a missing timestamp.

    The export path wants second precision — two events in the same minute
    must stay orderable in the exported Markdown.  Everything else uses
    :func:`fmt_ts`.
    """
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S")


def parse_when(value: Optional[str]) -> Optional[float]:
    """``2026-09-30``, ``2026-09-30T12:00``, or ``7d`` (days ago) -> epoch.

    Shared by the CLI's ``--since`` / ``--until`` and the machine-readable
    search op, so a client cannot interpret a time differently from the command
    line.  Raises ``ValueError`` on anything else.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.endswith("d") and text[:-1].isdigit():
        return time.time() - int(text[:-1]) * 86400
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    raise ValueError("unrecognised time %r (try 2026-09-30 or 7d)" % value)

def parse_ts(iso: Optional[str]) -> Optional[float]:
    """An ISO-8601 timestamp as a POSIX float, or ``None``.

    Was three byte-identical copies (claude / codex / grok adapters).  Providers
    differ in plenty of ways that matter; parsing a timestamp is not one of
    them, and three copies is three places to fix when a provider starts
    emitting a format ``fromisoformat`` does not accept.
    """
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def event_digest(kind, content, command, tool_output, tool_call_id,
                 exit_code=None) -> str:
    """A short, stable fingerprint of what an event *is*.

    An event's row id is not an identity: `replace_session` deletes and
    re-inserts, and SQLite reallocates rowids, so a reference built on `id`
    still resolves after a re-scan -- to a different event.  `(sid, seq)` is
    better but not sufficient either: measured on the codex and grok adapters,
    inserting or deleting a line in the middle of a transcript leaves only
    3/5 and 2/5 of recorded references pointing at the same content.

    So a reference carries this digest too.  It cannot say whether a claim is
    true -- only whether the event a reference points at is still the event it
    was recorded from.  Content identity is not fact verification, and this
    function must never be used as if it were.

    Fields, and why each is here:

    ``kind``       -- user / assistant / tool_call / tool_result / error.  A
                      reference to the wrong *kind* of event is a broken lead.
    ``content``    -- the message body, for the kinds that have one.
    ``command``    -- what a tool call ran.
    ``tool_output``-- what it printed.
    ``tool_call_id`` -- binds a result to its call, so the pair is cited
                      together rather than as two loose rows.
    ``exit_code``  -- **why the tool failed or passed.**  Without it a result
                      that flipped from `1` to `0` with unchanged output would
                      keep the same digest, so the one change most likely to
                      turn a failure into a claimed success would be invisible.

    Deliberately *not* fields: provider, session id and timestamp.  The digest
    describes an event's content, and the reference's other parts already say
    where it lives; folding the location in would make the same event hash
    differently per index, which is the opposite of what a reader comparing two
    spellings of one event needs.

    Not a security primitive.  It is 8 hex characters (32 bits) of SHA-256,
    truncated for display.  It is compared *pairwise* -- the event a reference
    names against the event sitting at that spot now -- so the bound that
    matters is 2^-32 per comparison, not the birthday bound across an index.
    Anyone who can rewrite the transcript can recompute it: this detects
    accidental drift, never a determined forger.
    """
    import hashlib

    body = "|".join("" if v is None else str(v) for v in
                    (kind, content, command, tool_output, tool_call_id,
                     exit_code))
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:8]
