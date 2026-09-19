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

# D13-adjacent D10 rule: token estimates are chars/4, no tokenizer dep.
BUDGET_PRESETS = {
    "compact": 4000,
    "balanced": 20000,
    "full": 100000,
}

# "auto" resolves per target agent; known continuation targets are
# large-context agentic CLIs -> balanced. Unknown targets also get balanced.
AUTO_BUDGET_TOKENS = 20000

# Fixed section priority, prefix-matched against "## ..." headings.
# Earlier = kept first under budget pressure. Evidence & Provenance is
# special: it may shrink to its header but is never dropped entirely.
SECTION_PRIORITY = [
    "## Goal",
    "## User goal / instructions",
    "## Current verified state",
    "## Where the work stopped",
    "## Current repository state (live snapshot)",
    "## Goal-ranked evidence",
    "## Prior assistant conclusions",
    "## Decisions",
    "## Errors encountered",
    "## Files touched across sessions",
    "## Files this session touched",
    "## Commands executed",
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
    """Resolve `--budget auto` for a target agent. All known continuation
    targets are large-context agentic CLIs -> balanced."""
    return AUTO_BUDGET_TOKENS


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
    for sec in sections:
        sec["priority"] = _priority_of(sec["title"])
        sec["text"] = "\n".join(sec["lines"]).rstrip() + "\n"
    return "\n".join(header_lines), sections


def _priority_of(title: str) -> int:
    for i, p in enumerate(SECTION_PRIORITY):
        if title.startswith(p):
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
    kept: List[Tuple[int, str]] = []

    for sec in sections:
        if sec["title"] == _EVIDENCE_TITLE:
            keep_lines = ev["lines"][:_EVIDENCE_KEEP_LINES] if ev else []
            ev_text = ("\n".join([sec["title"]] + keep_lines).rstrip() + "\n")
            kept.append((sec["priority"], ev_text))
            remaining = max(0, remaining - estimate_tokens(ev_text))
            continue
        t = estimate_tokens(sec["text"])
        if t <= remaining:
            kept.append((sec["priority"], sec["title"] + "\n" + sec["text"]))
            remaining -= t
            continue
        shrunk = _shrink_section(sec, remaining)
        st = estimate_tokens(shrunk)
        if st <= remaining and st > estimate_tokens(sec["title"] + "\n"):
            kept.append((sec["priority"], shrunk))
            remaining -= st
            trimmed.append(sec["title"])
        else:
            dropped.append(sec["title"])

    kept.sort(key=lambda p: p[0])
    body = header.rstrip() + "\n\n" + "\n".join(t for _, t in kept)

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
