"""The capability matrix must not promise what the adapter denies.

`native_resume` is a claim about code, not about this machine: the adapter is
the thing that would have to build the resume command.  A hand-written table
that says SUPPORTED while the adapter sets `can_resume = False` tells the user
to run a command that does not exist.

This was real: `capability_matrix.DECLARED["zcode"]["native_resume"]` said
SUPPORTED ("desktop resume is documented") while `ZCodeAdapter.can_resume` was
False, and `voyager verify --matrix` reported the table's answer because
`native_resume` was not in the machine-dependent set that gets capped by
evidence.
"""

from __future__ import annotations

import pytest

from voyager.adapters import load_all
from voyager.adapters.base import get_adapter
from voyager.capability_matrix import (
    DECLARED,
    NOT_FOUND,
    STATE_ORDER,
    SUPPORTED,
    adapter_can_resume,
    resolve_cell,
)

PROVIDERS = ["codex", "claude", "grok", "zcode", "dsh",
             "cursor", "kiro", "antigravity"]


@pytest.fixture(scope="module", autouse=True)
def _registered():
    load_all()


def test_every_adapter_is_discoverable():
    """Without this the comparison below would pass vacuously."""
    missing = [p for p in PROVIDERS if get_adapter(p) is None]
    assert not missing, f"adapters not registered: {missing}"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_native_resume_never_exceeds_the_adapter(provider):
    """SUPPORTED requires the adapter to agree, per provider."""
    adapter = get_adapter(provider)
    can = adapter_can_resume(provider)
    assert can is not None, f"{provider}: adapter_can_resume returned None"
    assert can == bool(adapter.can_resume), \
        f"{provider}: adapter_can_resume disagrees with the adapter"

    state, reason = resolve_cell(provider, "native_resume")
    if can:
        assert STATE_ORDER.get(state, 0) >= STATE_ORDER.get(NOT_FOUND, 0)
    else:
        assert state == NOT_FOUND, (
            f"{provider}: adapter sets can_resume=False but the matrix reports "
            f"{state} ({reason})")


def test_zcode_specifically_is_not_advertised_as_resumable():
    """The exact case that was wrong; kept as its own test so a regression
    names the provider instead of only failing a parametrised case."""
    assert get_adapter("zcode").can_resume is False
    state, reason = resolve_cell("zcode", "native_resume")
    assert state == NOT_FOUND
    assert "can_resume=False" in reason, \
        f"the reason should name the adapter as the source: {reason!r}"
    # and the declaration itself is still the optimistic one -- the fix is in
    # resolve_cell, not in hiding the declaration
    assert DECLARED["zcode"]["native_resume"][0] == SUPPORTED


def test_a_declared_not_found_is_left_alone():
    """The adapter gate must not rewrite a pessimistic declaration upward."""
    for provider in ("cursor", "kiro", "antigravity"):
        state, _ = resolve_cell(provider, "native_resume")
        assert state == NOT_FOUND, f"{provider} was unexpectedly upgraded to {state}"


def test_non_resume_dimensions_are_unaffected():
    """The gate is scoped to native_resume."""
    for provider in PROVIDERS:
        for dim in ("session_discovery", "session_indexing", "startup_hook"):
            if dim not in DECLARED.get(provider, {}):
                continue
            state, _ = resolve_cell(provider, dim)
            assert isinstance(state, str) and state, f"{provider}/{dim} resolved empty"
