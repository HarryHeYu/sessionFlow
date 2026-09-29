"""Shared payload budget for provider SessionStart hooks.

Every provider that speaks the Claude-compatible envelope caps the injected
string field at **10,000 characters** measured in UTF-16 code units, and Codex
truncates silently when the payload exceeds it: the delivered developer record
starts with

    Warning: truncated output (original token count: 4296)
    Total output lines: 209

and keeps the head and the tail while eliding the middle.  For a tiered-v1
document that is the worst possible cut, because the L1 section sits at the end
and the newest turns are what the model needs.

So the handler must stay under the cap itself, and spill the full bundle to a
file so nothing is actually lost.  Spills go to Voyager's own directory, never
to ``%TEMP%``: that directory is cleaned by external processes and is not
durable storage.

Measuring and cutting live here rather than in each handler so every provider
gets identical semantics.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional, Tuple

# The providers cap at 10,000 characters; stay under it with a margin so the
# appended truncation note also fits.
MAX_ADDITIONAL_CONTEXT_CHARS = 9000

# Spilled bundles are diagnostic artefacts, not state — keep the newest few.
SPILL_KEEP = 10


def payload_len(text: str) -> int:
    """Length the way the providers measure it.

    They are JavaScript programs, so their `String.length` counts UTF-16 code
    units: an astral character (an emoji, say) counts as 2.  Python's `len()`
    counts code points, so measuring with `len()` means a payload of 9000 emoji
    looks safe here while actually occupying 18,000 units — the cap would
    silently fail to cap.
    """
    if text.isascii():  # C-level fast path for the overwhelmingly common case
        return len(text)
    return len(text) + sum(1 for ch in text if ch > "\uffff")


def truncate_to_budget(text: str, budget: int) -> str:
    """Cut `text` so its :func:`payload_len` is at most `budget`. Never raises.

    Walks character by character rather than slicing first: an overflow measured
    in UTF-16 units cannot be subtracted from a code-point count, because an
    astral character contributes two units but occupies one code point.
    """
    if budget <= 0:
        return ""
    total = 0
    for index, char in enumerate(text):
        total += 2 if char > "\uffff" else 1
        if total > budget:
            return text[:index]
    return text


def spill_dir() -> Path:
    override = os.environ.get("VOYAGER_CONTEXT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".voyager" / "context"


def spill_bundle(context: str, provider: str) -> Optional[Path]:
    """Write the full bundle outside the payload cap. Best effort, never raises."""
    try:
        out_dir = spill_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        # pid guards against two sessions starting within the same second.
        name = "%s-sessionstart-%d-%d.md" % (provider, int(time.time()), os.getpid())
        path = out_dir / name
        path.write_text(context, encoding="utf-8")
        _prune_spills(out_dir)
        return path
    except Exception:
        return None


def _prune_spills(out_dir: Path) -> None:
    """Keep the newest SPILL_KEEP bundles; never touch recent ones. Never raises."""
    try:
        files = sorted(
            (p for p in out_dir.glob("*-sessionstart-*.md") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        for stale in files[:-SPILL_KEEP]:
            try:
                stale.unlink()
            except Exception:
                pass
    except Exception:
        pass


def cap_with_note(
    context: str,
    provider: str,
    *,
    max_chars: int = MAX_ADDITIONAL_CONTEXT_CHARS,
    spill: bool = True,
) -> Tuple[str, Optional[Path]]:
    """Fit `context` into the provider's cap and spill the full copy.

    Returns ``(payload, spilled_path)``.  When the context already fits the
    payload is the context itself and nothing is spilled — the common case, and
    the one that keeps the model's view coherent.

    For a tiered-v1 document prefer :func:`cap_tiered_with_note`, which keeps the
    **newest** L1 turns instead of cutting the document's tail.
    """
    if payload_len(context) <= max_chars:
        return context, None

    spilled_path = spill_bundle(context, provider) if spill else None
    note = _truncation_note(context, spilled_path)
    if payload_len(note) >= max_chars:
        note = ""  # pathological path length — never blow the cap for a note
    head_budget = max_chars - payload_len(note)
    return truncate_to_budget(context, head_budget) + note, spilled_path


L1_MARKER = "[L1 Active Working Context]"


def _truncation_note(context: str, spilled_path: Optional[Path]) -> str:
    note = ("\n\n---\n[Voyager] Continuation bundle truncated for the hook payload "
            "(%d chars total)." % payload_len(context))
    if spilled_path:
        note += "\nFull bundle: %s" % spilled_path
    return note


def cap_tiered_with_note(
    context: str,
    provider: str,
    *,
    max_chars: int = MAX_ADDITIONAL_CONTEXT_CHARS,
    spill: bool = True,
) -> Tuple[str, Optional[Path]]:
    """Fit a tiered-v1 document into the cap **keeping the newest L1 turns**.

    A plain head cut is the wrong shape here: tiered-v1 puts L0, Runtime State
    and the retrieval hint first and the L1 working window last, so cutting the
    document's tail throws away exactly the newest work the model needs.  This
    keeps the whole preamble and as much of the L1 **tail** as fits, dropping the
    oldest L1 turns instead.  The full document is spilled either way.
    """
    if payload_len(context) <= max_chars:
        return context, None

    spilled_path = spill_bundle(context, provider) if spill else None
    note = _truncation_note(context, spilled_path)
    if payload_len(note) >= max_chars:
        note = ""
    room = max_chars - payload_len(note)

    index = context.find(L1_MARKER)
    if index < 0:
        # Not a tiered document — fall back to the plain head cut.
        return truncate_to_budget(context, room) + note, spilled_path

    head = context[:index + len(L1_MARKER)]   # keep the marker itself
    l1 = context[index + len(L1_MARKER):]
    head_len = payload_len(head)
    if head_len >= room:
        # The preamble alone busts the cap; keep its head and the note.
        return truncate_to_budget(context, room) + note, spilled_path

    # Keep both ends of the window and drop the middle, which is what the
    # provider itself does -- but with a deliberate shape.  The scheduler puts
    # "one newest turn per session, oldest session first" at the head, and the
    # newest turns overall at the tail, so keeping both ends preserves the
    # per-session minimum *and* the most recent work; only the middle (older
    # sessions' extra turns) is dropped.
    elision = "\n\n[... older L1 turns elided; full bundle spilled ...]\n\n"
    room_for_l1 = room - head_len - payload_len(elision)
    if room_for_l1 <= 0:
        return truncate_to_budget(context, room) + note, spilled_path

    head_share = room_for_l1 // 2
    keep_head = _drop_partial_last_turn(_head_within(l1, head_share))
    keep_tail = _drop_partial_first_turn(_tail_within(l1, room_for_l1 - payload_len(keep_head)))
    if not keep_tail:
        return head + keep_head + note, spilled_path
    if not keep_head:
        return head + keep_tail + note, spilled_path
    return head + keep_head + elision + keep_tail + note, spilled_path


def _head_within(text: str, budget: int) -> str:
    """The longest prefix of `text` whose :func:`payload_len` fits `budget`."""
    if budget <= 0:
        return ""
    total = 0
    for index, char in enumerate(text):
        total += 2 if char > "\uffff" else 1
        if total > budget:
            return text[:index]
    return text


def _tail_within(text: str, budget: int) -> str:
    """The longest suffix of `text` whose :func:`payload_len` fits `budget`."""
    if budget <= 0:
        return ""
    total = 0
    for index in range(len(text) - 1, -1, -1):
        total += 2 if text[index] > "\uffff" else 1
        if total > budget:
            return text[index + 1:]
    return text


def _drop_partial_first_turn(keep: str) -> str:
    """Start at a turn head so the window never begins mid-turn."""
    marker = keep.find("\n[")
    if marker > 0:
        return keep[marker + 1:]
    return keep


def _drop_partial_last_turn(keep: str) -> str:
    """Cut back to the last complete turn so the window never ends mid-turn."""
    marker = keep.rfind("\n[")
    if marker > 0:
        return keep[:marker]
    return keep
