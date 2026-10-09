"""Packing must keep evidence over assertion, and must not reorder the bundle.

Two properties, both wrong before:

1. `SECTION_PRIORITY` put `## Latest assistant conclusion` second, ahead of the
   repository snapshot, the ranked evidence and the errors.  Under a tight
   budget the unverified paragraph survived and the checkable material was
   dropped -- in a bundle whose own heading says the paragraph is "read as what
   it said rather than as verified fact".

2. `apply_budget` emitted kept sections sorted by keep-priority, so a bundle
   that went over budget came out in a different order than the compiler wrote
   it.  The same thread read differently depending on its size.
"""

from __future__ import annotations

from voyager.budget import (
    SECTION_PRIORITY,
    _priority_of,
    apply_budget,
    estimate_tokens,
)

EVIDENCE_SECTIONS = [
    "## Current repository state (live snapshot)",
    "## Goal-ranked evidence",
    "## Errors encountered",
]
ASSERTION_SECTIONS = [
    "## Latest assistant conclusion",
    "## Other sessions' conclusions",
]


def _bundle() -> str:
    """A bundle where the assistant prose is long and the evidence is short."""
    parts = ["# Continuation Bundle", "", "## Goal", "",
             "**Primary user goal:** ship refresh rotation", ""]
    for title in ASSERTION_SECTIONS:
        parts += [title, "", "word " * 900, ""]          # ~1800 tokens each
    for title in EVIDENCE_SECTIONS:
        parts += [title, "", "- `pytest -q` -> 3 failed", ""]
    parts += ["## Evidence & Provenance", "", "Compiled from 2 session(s):", "",
              "- **codex** `r1`: rotation"]
    return "\n".join(parts) + "\n"


def test_evidence_outranks_assertion_in_the_keep_order():
    """The rule, stated directly, so a future reorder has to argue with it."""
    for ev in EVIDENCE_SECTIONS:
        for asrt in ASSERTION_SECTIONS:
            assert _priority_of(ev) < _priority_of(asrt), (
                f"{ev} should be kept before {asrt}: an assistant statement is "
                f"the thing the bundle itself says may be wrong")


def test_goal_still_outranks_everything():
    assert _priority_of("## Goal") == 0
    for title in EVIDENCE_SECTIONS + ASSERTION_SECTIONS:
        assert _priority_of("## Goal") < _priority_of(title)


def test_tight_budget_keeps_the_evidence_and_drops_the_prose():
    """The behavioural consequence, on a bundle where they compete."""
    bundle = _bundle()
    # small enough that not everything fits
    packed, info = apply_budget(bundle, 2000)
    assert info["dropped"] or info["trimmed"], \
        "the fixture did not actually exceed the budget"

    kept_evidence = [t for t in EVIDENCE_SECTIONS if t in packed]
    assert kept_evidence, (
        "the repository snapshot / ranked evidence / errors were all dropped "
        f"while the assistant prose was kept: dropped={info['dropped']}")
    assert estimate_tokens(packed) <= 2000, "the packed bundle exceeded its budget"


def test_packing_preserves_the_compilers_order():
    """What is kept must be printed where the compiler put it."""
    bundle = _bundle()
    packed, info = apply_budget(bundle, 2000)
    assert info["dropped"] or info["trimmed"]

    # Take the order from the source bundle itself rather than hardcoding it:
    # the assertion is "packing preserves order", not "the compiler uses my
    # order", and a hardcoded list would just restate SECTION_PRIORITY.
    def titles(text: str) -> list[str]:
        return [ln for ln in text.splitlines() if ln.startswith("## ")]

    source_order = titles(bundle)
    packed_order = [t for t in titles(packed) if t in source_order]
    expected = [t for t in source_order if t in packed_order]
    assert packed_order == expected, (
        "packing reordered the bundle:\n"
        f"  compiler wrote: {source_order}\n"
        f"  packed has:     {packed_order}")


def test_an_under_budget_bundle_is_returned_untouched():
    """The fast path must stay a fast path."""
    bundle = _bundle()
    packed, info = apply_budget(bundle, 10 ** 6)
    assert packed == bundle
    assert info["dropped"] == [] and info["trimmed"] == []
