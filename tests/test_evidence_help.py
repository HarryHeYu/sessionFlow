"""`evidence verify --help` must state the five outcomes and their exit codes.

A script branches on the exit code, so the mapping is part of the contract. It
lived only in the README until 2026-11-09 (MAINTENANCE #12): a user writing a
script had to trigger each state to learn the numbers.

The test compares the help text against ``evidence.EXIT_CODES`` itself, so a
future change to either side fails here instead of drifting quietly.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from voyager.evidence import EXIT_CODES

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def help_output(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData/Roaming"),
        "LOCALAPPDATA": str(home / "AppData/Local"),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    proc = subprocess.run(
        [sys.executable, "-m", "voyager.cli", "evidence", "verify", "--help"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_help_lists_every_state_with_its_code(help_output):
    for state, code in EXIT_CODES.items():
        assert state in help_output, "help does not name %s" % state
        assert re.search(r"%s\s*\(%d\)" % (re.escape(state), code), help_output), (
            "%s is not shown with exit code %d" % (state, code))


def test_exit_codes_are_unchanged(help_output):
    """The numbers are the contract; this is the place that says so."""
    assert EXIT_CODES == {
        "MATCH": 0,
        "MISMATCH": 1,
        "NOT_FOUND": 2,
        "UNVERIFIABLE": 3,
        "INVALID_REFERENCE": 4,
    }


def test_help_does_not_claim_match_means_true(help_output):
    """A MATCH is reference consistency, never fact-checking."""
    assert "does not mean the claim is true" in help_output
