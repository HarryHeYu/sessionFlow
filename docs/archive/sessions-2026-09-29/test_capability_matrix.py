#!/usr/bin/env python3
"""Test suite for capability matrix functionality."""

import sys
import json

sys.path.insert(0, 'E:\\code\\voyager')

from voyager.capability_matrix import (
    PROVIDERS, DIMENSIONS, STATE_ORDER,
    collect_evidence, summary, matrix, provider_state, ZERO_TOUCH_OBSERVED
)


def test_all_providers_exist():
    """Verify all expected providers are in the matrix."""
    expected = {"codex", "claude", "grok", "zcode", "cursor", "kiro", "antigravity", "dsh"}
    actual = set(PROVIDERS)
    assert expected == actual, f"Provider mismatch: expected {expected}, got {actual}"
    print("PASS: All expected providers present")


def test_zero_touch_observed():
    """Verify zero-touch observation tracking."""
    assert "codex" in ZERO_TOUCH_OBSERVED
    assert "claude" in ZERO_TOUCH_OBSERVED
    assert "grok" in ZERO_TOUCH_OBSERVED
    print(f"PASS: ZERO_TOUCH_OBSERVED correctly identifies {ZERO_TOUCH_OBSERVED}")


def test_evidence_collection():
    """Test that evidence collection works for each provider."""
    for provider in PROVIDERS:
        ev = collect_evidence(provider)
        # Should not raise exceptions
        assert hasattr(ev, 'installed')
        assert hasattr(ev, 'hook_registered')
        assert hasattr(ev, 'hook_fired')
        assert hasattr(ev, 'zero_touch_observed')

        # State ordering should be consistent
        state_order = STATE_ORDER.get(ev.zero_touch_observed and 5 or (ev.hook_fired and 4 or 3), 0)
        print(f"  {provider}: installed={ev.installed}, hook_registered={ev.hook_registered}, hook_fired={ev.hook_fired}, zero_touch={ev.zero_touch_observed}")

    print("PASS: Evidence collection works for all providers")


def test_summary_output():
    """Test summary() returns correct structure."""
    result = summary()
    assert "providers" in result
    assert "dimensions" in result
    assert "states" in result

    for provider in PROVIDERS:
        assert provider in result["providers"]
        info = result["providers"][provider]
        assert "state" in info
        assert "installed" in info
        assert "hook_registered" in info
        assert "hook_fired" in info

    print("PASS: Summary output has correct structure")


def test_matrix_resolution():
    """Test that matrix() resolves cells correctly."""
    m = matrix()
    assert len(m) == len(PROVIDERS)

    for provider in PROVIDERS:
        assert provider in m
        for dimension in DIMENSIONS:
            assert dimension in m[provider]
            cell = m[provider][dimension]
            assert "state" in cell
            assert "note" in cell

    print(f"PASS: Matrix contains {len(m)} providers x {len(DIMENSIONS)} dimensions")


def test_state_hierarchy():
    """Verify state hierarchy is correctly ordered."""
    states = list(STATE_ORDER.keys())
    for i in range(len(states) - 1):
        assert STATE_ORDER[states[i]] < STATE_ORDER[states[i + 1]], \
            f"State order violation: {states[i]} should be less than {states[i + 1]}"

    print("PASS: State hierarchy is properly ordered")


def main():
    """Run all tests."""
    print("=" * 70)
    print("Testing Capability Matrix")
    print("=" * 70)

    try:
        test_all_providers_exist()
        test_zero_touch_observed()
        test_evidence_collection()
        test_summary_output()
        test_matrix_resolution()
        test_state_hierarchy()

        print("\n" + "=" * 70)
        print("ALL TESTS PASSED")
        print("=" * 70)
        return 0

    except AssertionError as e:
        print(f"\nFAIL: Test failed: {e}")
        return 1
    except Exception as e:
        print(f"\nFAIL: Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
