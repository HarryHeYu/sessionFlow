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
