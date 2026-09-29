"""One classification of a continuity result, shared by every provider handler.

The distinction that matters, and that the handlers used to lose:

    success    -> a WorkThread was resolved and context is available
    no_thread  -> the core *confirmed* there is nothing to continue here
    ambiguous  -> more than one WorkThread matches, so nothing may be chosen
    error      -> the continuity engine failed; the outcome is unknown

`no_thread` and `error` are not the same claim.  Reporting an engine failure as
"no thread" tells the caller there is no work here when the truth is that we do
not know -- and reporting ambiguity as either hides an actionable choice from the
user.  Every provider handler must therefore route through the same classifier.

The statuses come from `voyager.startup.startup_continuity`, which returns them as
`attach_status`:

    already_attached / auto_attached / pending_resolve / no_auto_attach   (not errors)
    ERROR_AMBIGUOUS_WORKTHREAD        (several threads match)
    ERROR_NO_REPO_MATCH               (confirmed: no thread matches this repo)
    ERROR_SESSION_IN_OTHER_THREAD     (documented; not produced today)
    ERROR_PENDING_CONFLICT            (documented; not produced today)
    ERROR_UNKNOWN_SESSION             (documented; not produced today)

Only a confirmed absence is `no_thread`; every other ERROR_* is an `error`.
"""

from __future__ import annotations

from typing import Any

#: An ambiguous match: never auto-select, never call it "no thread".
AMBIGUOUS = "ERROR_AMBIGUOUS_WORKTHREAD"

#: Statuses that mean "the core looked and there is genuinely nothing here".
#: Kept as an explicit set rather than a prefix test, because an ERROR_ code is
#: not automatically a failure -- ERROR_NO_REPO_MATCH is a confirmed absence.
CONFIRMED_ABSENT = frozenset({"ERROR_NO_REPO_MATCH"})

#: Statuses that carry no failure at all.
NON_ERROR = frozenset({"already_attached", "auto_attached",
                       "pending_resolve", "no_auto_attach"})

CLASSIFICATIONS = ("context", "no_thread", "ambiguous", "error")


def classify(result: Any) -> str:
    """Classify a `startup_continuity` result. Never raises."""
    if result is None:
        # No result at all is a failure, not a confirmed absence.
        return "error"
    try:
        has_context = bool(getattr(result, "continuity_available", False)) and bool(
            getattr(result, "context", None))
    except Exception:
        return "error"
    if has_context:
        return "context"

    attach_status = ""
    try:
        attach_status = getattr(result, "attach_status", None) or ""
    except Exception:
        pass

    if attach_status == AMBIGUOUS:
        return "ambiguous"
    if attach_status in CONFIRMED_ABSENT:
        return "no_thread"
    if attach_status.startswith("ERROR_"):
        # Any other error code: the engine failed, so the outcome is unknown.
        return "error"
    if attach_status in NON_ERROR:
        # No context yet nothing failed: the thread has no bundle to hand over.
        return "no_thread"
    # An unrecognised status is treated as a failure rather than as absence --
    # the safe direction, because "no thread" would suppress an injection.
    return "error" if attach_status else "no_thread"


def build_result(result: Any, classification: str, cwd: str) -> dict:
    """The handler-facing dict for a non-context outcome.

    `ambiguous` and `error` both surface as status "error" with the machine-readable
    `attach_status` and `classification` kept alongside, so a caller can tell them
    apart without parsing the message.  Nothing here is ever auto-selected.
    """
    try:
        attach_status = getattr(result, "attach_status", None) or ""
    except Exception:
        attach_status = ""
    info = {
        "context_source": getattr(result, "context_source", None),
        "recommended_action": getattr(result, "recommended_action", None),
    }
    if classification in ("ambiguous", "error"):
        return {"status": "error", "classification": classification,
                "attach_status": attach_status,
                "message": ("voyager could not resolve a WorkThread for %s (%s). "
                            "Pick one explicitly with `voyager continue --thread <id>`."
                            % (cwd, attach_status)),
                "continuity_info": info}
    return {"status": "no_thread", "classification": "no_thread",
            "attach_status": attach_status, "continuity_info": info}
