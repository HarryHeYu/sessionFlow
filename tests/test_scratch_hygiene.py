"""Scratch policy tests.

The policy is **opt-in**: setting ``VOYAGER_SCRATCH_ROOT`` redirects every
temp artifact the suite produces under that root (the local Windows rule:
never leave test scratch on the system drive).  Without the variable the
suite uses pytest's and tempfile's platform-native defaults — CI, Linux and
other developers never see an invented drive letter, and nothing here
requires any particular drive to exist.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import apply_scratch_root_policy


def _fake_config(basetemp=None):
    return SimpleNamespace(option=SimpleNamespace(basetemp=basetemp))


# --- explicit override ------------------------------------------------------

def test_explicit_override_redirects_everything_under_the_root(tmp_path):
    """With VOYAGER_SCRATCH_ROOT set, basetemp, the TMP/TEMP environment and
    tempfile's resolver all move under that root.  Uses a scratch root the
    test creates itself — no particular drive is assumed."""
    scratch = tmp_path / "scratch"
    cfg = _fake_config()
    env = {"VOYAGER_SCRATCH_ROOT": str(scratch)}

    applied = apply_scratch_root_policy(cfg, env)

    root = scratch / "tmp" / "pytest"
    assert applied == root
    assert root.is_dir(), "the scratch root is created on demand"
    assert cfg.option.basetemp == str(root)
    for var in ("TMPDIR", "TMP", "TEMP"):
        assert env[var] == str(root), \
            f"{var} must follow the scratch root for child processes"
    assert tempfile.tempdir is None, \
        "tempfile's cache must reset so gettempdir() re-resolves"


def test_explicit_override_resolves_gettempdir_under_the_root(tmp_path,
                                                              monkeypatch):
    """After the reset, tempfile.gettempdir() re-resolves against the
    redirected environment and lands under the scratch root.  The
    environment change goes through the real os.environ (that is where
    tempfile reads), and the cache is left clean for later tests."""
    scratch = tmp_path / "scratch"
    root = scratch / "tmp" / "pytest"
    for var in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(var, str(root))
    try:
        apply_scratch_root_policy(
            _fake_config(), {"VOYAGER_SCRATCH_ROOT": str(scratch)})
        got = Path(tempfile.gettempdir())
        assert got == root.resolve() or got.parent == root, \
            f"gettempdir() = {got} — must resolve under {root}"
    finally:
        # the reset above re-resolved against the monkeypatched environment;
        # drop the cache so later tests re-resolve against the real one
        tempfile.tempdir = None


# --- no override ------------------------------------------------------------

def test_no_override_touches_nothing():
    """Without VOYAGER_SCRATCH_ROOT the policy is a no-op: basetemp is not
    forced, the platform temp environment is not rewritten, and tempfile's
    resolver state is left exactly as it was."""
    cfg = _fake_config(basetemp="SENTINEL")
    env = {"TMP": "keep", "TEMP": "keep", "TMPDIR": "keep"}
    before = tempfile.tempdir

    applied = apply_scratch_root_policy(cfg, env)

    assert applied is None
    assert cfg.option.basetemp == "SENTINEL", \
        "basetemp must not be forced without an override"
    assert env == {"TMP": "keep", "TEMP": "keep", "TMPDIR": "keep"}, \
        "platform temp env must not be rewritten without an override"
    assert tempfile.tempdir == before, \
        "tempfile's resolver must not be reset without an override"


def test_scratch_policy_hardcodes_no_drive_letter():
    """The scratch root may enter only through VOYAGER_SCRATCH_ROOT: the
    policy itself must carry no baked-in drive letter or path default, so
    it works on machines without this developer's drives.  (Synthetic
    fixture data elsewhere in the conftest may use any paths it likes.)"""
    import inspect
    import re
    src = inspect.getsource(apply_scratch_root_policy)
    assert "sessionflow-scratch" not in src, \
        "the default scratch root must not be baked into the policy"
    assert not re.search(r"[A-Za-z]:[\\/]", src), \
        "the scratch policy must not bake in any drive-letter path"


# --- the live session -------------------------------------------------------

def test_live_session_honors_a_set_scratch_root(request):
    """When this session was started with VOYAGER_SCRATCH_ROOT set (the local
    Windows run), its basetemp and tempfile root really sit under it.  On
    machines that did not opt in, platform defaults are correct and the test
    steps aside."""
    override = os.environ.get("VOYAGER_SCRATCH_ROOT")
    if not override:
        pytest.skip("VOYAGER_SCRATCH_ROOT not set — platform defaults apply")
    root = Path(override).resolve()

    base = Path(request.config.option.basetemp).resolve()
    try:
        base.relative_to(root)
    except ValueError:
        pytest.fail(f"pytest basetemp {base} is not under {root}")

    got = Path(tempfile.gettempdir()).resolve()
    try:
        got.relative_to(root)
    except ValueError:
        pytest.fail(f"gettempdir() = {got} is not under {root}")
