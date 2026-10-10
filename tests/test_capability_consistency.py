"""One capability answer per fact.

Two capability models live in this repository:

* ``voyager.capability_matrix`` — declarations plus an evidence ladder.  Doctor,
  ``voyager verify --matrix``, the dashboard and ``status`` all resolve through
  it.  This is the canonical one.
* ``voyager.integrations.capabilities`` — a per-machine detector behind each
  integration class's ``capabilities`` property.

They are not required to be the same file: one declares, the other observes.
What is required is that a user never gets two different answers.  So this file
pins two things:

1. Doctor's per-capability explanation comes from the canonical resolver, not
   from a second set of hand-written rules.  (Two explanations of one fact is
   exactly how the ZCode ``native_resume`` claim drifted away from its adapter.)
2. No user-facing module reads the detector.  If a future change starts
   reporting its values, this fails — and then the two models have to be
   reconciled deliberately rather than by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voyager.adapters import load_all
from voyager.adapters.base import get_adapter
from voyager.capability_matrix import DIMENSIONS, NOT_FOUND, PROVIDERS
from voyager.doctor import check_verification

PKG = Path(__file__).resolve().parent.parent / "voyager"

#: Modules whose output a user reads.  `capability_reasons` and the matrix are
#: the only capability sources any of these may use.
USER_FACING_MODULES = (
    "cli.py", "doctor.py", "dashboard.py", "auto.py", "continuity.py",
    "store.py", "api.py", "timeline.py", "budget.py", "verification_harness.py",
    "startup.py",
)

#: The parallel detector, and the names that would give it away.
LEGACY_DETECTOR_MARKERS = (
    "detect_capabilities",
    "get_all_capabilities",
    "integrations.capabilities",
    "integrations import capabilities",
)


@pytest.fixture(scope="module", autouse=True)
def _registered():
    load_all()


@pytest.fixture(scope="module")
def verification():
    return check_verification(db_path=None)


def test_every_provider_reports_every_dimension(verification):
    """A missing dimension would leave the user with no explanation at all."""
    for provider in PROVIDERS:
        block = verification["providers"][provider]
        reasons = block.get("capability_reasons")
        assert reasons is not None, f"{provider}: no per-capability reasons"
        missing = [d for d in DIMENSIONS if d not in reasons]
        assert not missing, f"{provider}: no reason for {missing}"
        for dim, entry in reasons.items():
            assert set(entry) == {"state", "reason"}, f"{provider}/{dim}: {entry}"
            assert entry["state"], f"{provider}/{dim} has an empty state"
            assert entry["reason"], f"{provider}/{dim} has an empty reason"


def test_doctor_reports_the_adapters_denial_not_a_second_opinion(verification):
    """The ZCode case, seen from the surface a user actually reads.

    Whatever the hand-written declaration says, a provider whose adapter cannot
    build the resume command must not be reported as resumable — and there has
    to be a reason attached, so the user is not left guessing.
    """
    denied = [p for p in PROVIDERS
              if (get_adapter(p) is not None and not get_adapter(p).can_resume)]
    assert denied, "no adapter denies resume; this test would pass vacuously"

    for provider in denied:
        entry = verification["providers"][provider]["capability_reasons"]["native_resume"]
        assert entry["state"] == NOT_FOUND, (
            f"{provider}: adapter sets can_resume=False but doctor reports "
            f"{entry['state']} ({entry['reason']})")
        assert entry["reason"], f"{provider}: denial reported with no reason"


def test_the_gate_names_the_adapter_when_the_declaration_disagrees(verification):
    """ZCode specifically: declaration said SUPPORTED, the adapter said no.

    The reason has to say which of the two won, so a reader can tell a code
    fact (the adapter cannot build the command) from a machine fact.
    """
    entry = verification["providers"]["zcode"]["capability_reasons"]["native_resume"]
    assert entry["state"] == NOT_FOUND
    assert "can_resume" in entry["reason"], (
        "the reason must name the adapter as the authority, "
        f"got {entry['reason']!r}")


def test_capability_reason_is_the_resolvers_own_sentence(verification):
    """Doctor must not paraphrase the resolver into a second rule set.

    The startup summary (`blocked_reason`) is deliberately its own sentence and
    is scoped to startup continuity; every other capability has to reuse the
    canonical text, so the two surfaces can never diverge.
    """
    from voyager.capability_matrix import collect_evidence, resolve_cell

    provider = "zcode"
    ev = collect_evidence(provider)
    canonical = {d: resolve_cell(provider, d, ev) for d in DIMENSIONS}
    reported = verification["providers"][provider]["capability_reasons"]
    for dim, (state, reason) in canonical.items():
        assert reported[dim]["state"] == state, (
            f"{dim}: doctor says {reported[dim]['state']}, resolver says {state}")
        assert reported[dim]["reason"] == reason, (
            f"{dim}: doctor paraphrased the resolver")


def test_no_user_facing_module_reads_the_parallel_detector():
    """A guard, not a behaviour test.

    `integrations/capabilities.py` carries per-machine facts that have gone
    stale before, and its `has_session_start_hook` values do not line up with
    the matrix's `startup_hook` states for codex/zcode/cursor/kiro/antigravity —
    the two use similar words for different questions.  Nothing user-visible
    reads it today.  If something starts to, reconcile the models first; this
    test exists so that happens on purpose.
    """
    offenders = {}
    for name in USER_FACING_MODULES:
        path = PKG / name
        if not path.is_file():
            continue
        src = path.read_text(encoding="utf-8")
        hits = [m for m in LEGACY_DETECTOR_MARKERS if m in src]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        "these modules must resolve capabilities through capability_matrix, not "
        f"through the parallel detector: {offenders}")


def test_the_canonical_resolver_is_what_the_user_facing_modules_import():
    """The positive half of the guard: the matrix really is the shared source."""
    for name in ("doctor.py", "cli.py", "verification_harness.py", "budget.py"):
        src = (PKG / name).read_text(encoding="utf-8")
        assert "capability_matrix" in src, f"{name} does not use the canonical matrix"
