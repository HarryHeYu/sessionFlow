"""Context Budget (roadmap Phase 4 / issue #5).

Deterministic, offline bundle packing. A budget is a token ceiling for a
Continuation Bundle / context package; tokens are estimated as chars/4
(no tokenizer dependency, D10).

Pipeline order matters: ranking (Phase 3) happens FIRST, then budgeting.
Packing keeps sections by a fixed priority:

    goal > current state > ranked evidence > prior conclusions /
    decisions > failures > artifact pointers (files) > commands >
    conversation > evidence & provenance (never dropped silently)

Sections that do not fit are dropped or trimmed, and every drop/trim is
named in a "## Budget notes" section at the end — nothing disappears
silently and the Evidence & Provenance header always survives, so source
session ids are never lost to trimming.

Shared pipeline: handoff (single session), merge and continue
(multi-session) all call :func:`apply_budget` on their rendered bundle.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Import provider-aware budget defaults from capability_matrix
try:
    from .capability_matrix import (
        PROVIDER_CONTEXT_BUDGETS, provider_auto_budget_target,
    )
except ImportError:
    # Fallback if capability_matrix not available
    PROVIDER_CONTEXT_BUDGETS = {}

    def provider_auto_budget_target(provider: str) -> Optional[str]:
        return None

# D13-adjacent D10 rule: token estimates are chars/4, no tokenizer dep.
BUDGET_PRESETS = {
    "compact": 4000,
    "balanced": 20000,
    "full": 100000,
}

# "auto" resolves per target agent; known continuation targets are
# large-context agentic CLIs -> balanced. Unknown targets also get balanced.
AUTO_BUDGET_TOKENS = 20000


def resolve_auto_budget(target: Optional[str]) -> int:
    """Resolve `--budget auto` for a target agent using provider-aware defaults.

    Priority order:
    1. Provider-specific startup_context_budget converted to tokens
    2. Provider's auto_budget_target preset name
    3. Global default

    Returns token count (chars/4 estimate).
    """
    if not target:
        return AUTO_BUDGET_TOKENS

    target_lower = target.lower().strip()

    # Check if target matches a known provider
    from .capability_matrix import PROVIDERS
    matched_provider = None
    for p in PROVIDERS:
        if target_lower == p or target_lower.startswith(p + ":"):
            matched_provider = p
            break

    if matched_provider:
        # 1. Try provider-specific char budget
        char_budget = PROVIDER_CONTEXT_BUDGETS.get(matched_provider, {}).get(
            "startup_context_budget")
        if char_budget:
            return max(1, char_budget // 4)

        # 2. Try provider's auto_budget_target preset
        auto_target = PROVIDER_CONTEXT_BUDGETS.get(matched_provider, {}).get(
            "auto_budget_target")
        if auto_target and auto_target in BUDGET_PRESETS:
            return BUDGET_PRESETS[auto_target]

    # 3. Fall back to global default
    return AUTO_BUDGET_TOKENS

# Fixed section priority, prefix-matched against "## ..." headings.
# Earlier = kept first under budget pressure. Evidence & Provenance is
# special: it may shrink to its header but is never dropped entirely.
#
# The ordering rule is evidence before assertion.  A section backed by tool
# output, a repository snapshot or a command log is worth more than an
# assistant's own prose, because the prose is exactly what may be wrong: the
# bundle labels it "read as what it said rather than as verified fact".  Under
# a tight budget the old order kept the unverified paragraph and dropped the
# repository snapshot, the ranked evidence and the errors -- the parts a reader
# could actually check.
#
# This list is a *keep* order.  It does not decide the order sections are
# printed in; that stays whatever the compiler wrote.
SECTION_PRIORITY = [
    "## Goal",
    "## User goal / instructions",
    # --- backed by evidence ------------------------------------------------
    "## Current repository state (live snapshot)",
    "## Goal-ranked evidence",
    "## Errors encountered",
    "## Files touched across sessions",
    "## Files this session touched",
    "## Commands executed",
    # --- assistant statements, including the [open] items -------------------
    "## Latest assistant conclusion",
    "## Where the work stopped",
    "## Other sessions' conclusions",
    "## Decisions",
    "## Conversation",
    "## Evidence & Provenance",
]

_EVIDENCE_TITLE = "## Evidence & Provenance"
_EVIDENCE_KEEP_LINES = 2      # heading + "Compiled from N session(s):"
_BUDGET_NOTE_TITLE = "## Budget notes"


def estimate_tokens(text: str) -> int:
    """chars/4 — the roadmap-sanctioned estimate, no tokenizer dep."""
    return (len(text or "") + 3) // 4


def parse_budget(spec: Optional[str]) -> Optional[int]:
    """Parse a --budget value into a token ceiling.

    compact/balanced/full -> presets; auto -> None (caller resolves per
    target agent); "12k"/"3k" -> *1000; bare int -> tokens. Returns None
    for None/empty. Raises ValueError on garbage so the CLI fails loudly.
    """
    if spec is None:
        return None
    spec = str(spec).strip().lower()
    if not spec:
        return None
    if spec in BUDGET_PRESETS:
        return BUDGET_PRESETS[spec]
    if spec == "auto":
        return None
    if spec.endswith("k") and spec[:-1].isdigit():
        return int(spec[:-1]) * 1000
    if spec.isdigit():
        return int(spec)
    raise ValueError(
        "invalid budget: {0!r} (use compact|balanced|full|auto|Nk|<int>)".format(spec))


def auto_budget(target: Optional[str]) -> int:
    """Resolve `--budget auto` for a target agent using provider-aware defaults.

    This is the legacy wrapper; all callers should use resolve_auto_budget() directly.

    Priority order:
    1. Provider-specific startup_context_budget converted to tokens
    2. Provider's auto_budget_target preset name
    3. Global default (20k tokens)

    Returns token count (chars/4 estimate).
    """
    return resolve_auto_budget(target)


def split_sections(bundle: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Split markdown into (header, [{title, lines, priority, text}]).

    Header is everything before the first '## ' heading.
    """
    header_lines: List[str] = []
    sections: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in bundle.splitlines():
        if ln.startswith("## "):
            cur = {"title": ln.rstrip(), "lines": []}
            sections.append(cur)
            continue
        if cur is None:
            header_lines.append(ln)
        else:
            cur["lines"].append(ln)
    for i, sec in enumerate(sections):
        sec["priority"] = _priority_of(sec["title"])
        # Where the section sat in the rendered bundle.  Packing decides what
        # to keep by priority, but must emit what it keeps in the order the
        # compiler wrote it: SECTION_PRIORITY is a keep-order, not a narrative
        # order, and the two differ.
        sec["order"] = i
        sec["text"] = "\n".join(sec["lines"]).rstrip() + "\n"
    return "\n".join(header_lines), sections


def _priority_of(title: str) -> int:
    # Prefix matching, but only at a word boundary.  A bare startswith() lets
    # "## Goal" capture "## Goal-ranked evidence", which silently gave the
    # evidence section the goal's own keep-priority -- so the section the
    # bundle exists to make checkable outranked everything, for the wrong
    # reason and with no entry in this table saying so.
    def matches(p: str) -> bool:
        if not title.startswith(p):
            return False
        rest = title[len(p):]
        return rest == "" or rest[0] in " \t"

    for i, p in enumerate(SECTION_PRIORITY):
        if matches(p):
            return i
    return len(SECTION_PRIORITY)      # unknown sections rank last


def _shrink_section(sec: Dict[str, Any], token_budget: int) -> str:
    """Keep the heading and as many body lines as fit (at least one line
    is always kept so a kept section is never an empty stub). The trim
    note is only appended when it fits the budget itself."""
    out = [sec["title"]]
    # +1 per line accounts for the newline that joins the body — skipping
    # it under-counted large sections by ~1500 tokens and got them dropped.
    used = estimate_tokens(sec["title"]) + 1
    kept = 0
    trimmed_note = None
    for ln in sec["lines"]:
        t = estimate_tokens(ln) + 1
        if used + t > token_budget and kept >= 1:
            trimmed_note = "  … ({} more lines trimmed by budget)".format(
                len(sec["lines"]) - kept)
            if used + estimate_tokens(trimmed_note) <= token_budget:
                out.append(trimmed_note)
            break
        out.append(ln)
        used += t
        kept += 1
    return "\n".join(out).rstrip() + "\n"


def apply_budget(bundle: str, budget_tokens: Optional[int],
                 target: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Pack a rendered bundle under a token ceiling.

    Returns (packed_text, info). budget=None or an already-under-budget
    bundle returns the text unchanged. info carries estimated_tokens,
    dropped/trimmed section titles and the resolved budget.
    """
    if budget_tokens is None:
        return bundle, {"estimated_tokens": estimate_tokens(bundle),
                        "dropped": [], "trimmed": [], "budget": None}

    est = estimate_tokens(bundle)
    if est <= budget_tokens:
        return bundle, {"estimated_tokens": est, "dropped": [],
                        "trimmed": [], "budget": budget_tokens}

    header, sections = split_sections(bundle)
    sections.sort(key=lambda s: s["priority"])

    # Evidence & Provenance is always represented (provenance contract):
    # reserve room for its heading + session-count lines.
    ev = next((s for s in sections if s["title"] == _EVIDENCE_TITLE), None)
    ev_reserved = 0
    if ev:
        ev_reserved = estimate_tokens("\n".join(
            [ev["title"]] + ev["lines"][:_EVIDENCE_KEEP_LINES])) + 10

    fixed = estimate_tokens(header) + ev_reserved + 200   # Budget-notes slack
    remaining = max(0, budget_tokens - fixed)

    dropped: List[str] = []
    trimmed: List[str] = []
    kept: List[Tuple[int, int, str]] = []

    for sec in sections:
        if sec["title"] == _EVIDENCE_TITLE:
            keep_lines = ev["lines"][:_EVIDENCE_KEEP_LINES] if ev else []
            ev_text = ("\n".join([sec["title"]] + keep_lines).rstrip() + "\n")
            kept.append((sec["priority"], sec["order"], ev_text))
            remaining = max(0, remaining - estimate_tokens(ev_text))
            continue
        t = estimate_tokens(sec["text"])
        if t <= remaining:
            kept.append((sec["priority"], sec["order"],
                         sec["title"] + "\n" + sec["text"]))
            remaining -= t
            continue
        shrunk = _shrink_section(sec, remaining)
        st = estimate_tokens(shrunk)
        if st <= remaining and st > estimate_tokens(sec["title"] + "\n"):
            kept.append((sec["priority"], sec["order"], shrunk))
            remaining -= st
            trimmed.append(sec["title"])
        else:
            dropped.append(sec["title"])

    # Emit in the order the compiler wrote the bundle, not in keep-priority
    # order: the reader follows a narrative, and reordering it under budget
    # pressure made the same thread read differently depending on how big it
    # happened to be.
    kept.sort(key=lambda k: k[1])
    body = header.rstrip() + "\n\n" + "\n".join(t for _, _, t in kept)

    if dropped or trimmed:
        note = [_BUDGET_NOTE_TITLE, "",
                "Context budget: {0} tokens (estimate before packing: {1}).".format(
                    budget_tokens, est)]
        if dropped:
            note.append("Dropped section(s): " + ", ".join(dropped))
        if trimmed:
            note.append("Trimmed section(s): " + ", ".join(trimmed))
        note.append("Full history remains in the index "
                    "(`voyager show <id>` / `voyager export`).")
        body = body.rstrip() + "\n\n" + "\n".join(note) + "\n"

    return body, {"estimated_tokens": estimate_tokens(body),
                  "dropped": dropped, "trimmed": trimmed,
                  "budget": budget_tokens}
