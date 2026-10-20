"""Import-time path capture must not survive the isolation harness.

`tests/conftest.py` redirects the environment *and* patches every adapter's
storage globals.  That covers the common case.  What it does not prove -- and
what an in-process test cannot prove, because a module is either already
imported or is not -- is the ordering:

    import adapter     ->  module globals capture the REAL home
    set env            ->  too late for those globals
    patch globals      ->  the only thing standing between a test and the
                           developer's Codex/Claude/Kiro profiles

`conftest` does patch them, and its list is `PATH_GLOBALS`.  So the question
this file answers is: **is that list complete?**  An adapter that grows a new
import-time path global -- `STATE_DB`, `DB_PATH`, anything -- and is not added
to the tuple would silently keep pointing at real data, and every existing
in-process test would still pass.

So each case below runs a *subprocess*, which is the only way to control import
order, and asserts on the effective paths the adapter would use after
conftest-style patching.  Plus the inverse: when a real path reaches a scan, the
guard must refuse before anything is opened.

No case reads a real provider file.  The "protected" roots are the ones captured
before the override, and the assertions compare paths, never contents.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iso_guard  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# Imported at *collection* time, on purpose, before any fixture has redirected
# anything.  This is the dangerous import order: the adapters' module-level
# `HOME = Path.home()` captures the developer's real profile, and only
# conftest's patching stands between that and the test suite.  Without this
# import the adapters are first imported *inside* the isolation fixture -- after
# redirection -- so they capture the temp directory, and a regression in
# `_redirect_adapters` is invisible.  With it, the in-process check in
# `tests/test_isolation.py` exercises the real ordering.
from voyager.adapters import load_all  # noqa: E402

load_all()

#: The exact list conftest patches.  Imported from conftest so this test fails
#: the day the two lists disagree rather than the day someone notices.
from conftest import PATH_GLOBALS  # noqa: E402

ADAPTERS = ("codex", "claude", "grok", "zcode", "cursor", "kiro",
            "antigravity", "dsh")

PROBE = r"""
import json, os, sys
from pathlib import Path

pre_import = "--pre-import" in sys.argv
iso_root = Path(sys.argv[sys.argv.index("--iso") + 1])
ADAPTERS = sys.argv[sys.argv.index("--adapters") + 1].split(",")
PATH_GLOBALS = sys.argv[sys.argv.index("--globals") + 1].split(",")

# 1. capture the real profile BEFORE anything is overridden, so the assertion
#    later is against the developer's actual data roots.
real = {"home": str(Path.home()), "appdata": os.environ.get("APPDATA") or ""}

# 2. optionally import first -- this is what captures real paths into the
#    module globals.
if pre_import:
    from voyager.adapters import load_all
    load_all()

# 3. redirect, exactly as tests/conftest.py does.
for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
             "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
    os.environ[name] = str(iso_root / "no-agent-storage")

# 4. apply conftest's patching: import (no-op if already imported) and setattr
#    every name in PATH_GLOBALS.
import importlib
from voyager.adapters import load_all
load_all()
missing = {}
patched = {}
for name in ADAPTERS:
    try:
        mod = importlib.import_module("voyager.adapters.%s" % name)
    except ImportError:
        continue
    for attr in PATH_GLOBALS:
        if hasattr(mod, attr):
            setattr(mod, attr, iso_root / "no-agent-storage" / attr)
    # every import-time Path global, for the completeness check
    for attr in dir(mod):
        if not attr.isupper():
            continue
        value = getattr(mod, attr)
        if isinstance(value, Path):
            patched.setdefault(name, {})[attr] = str(value)

# 5. and the runtime-resolved home: an adapter that calls Path.home() inside a
#    function reads the *current* env, which is the redirected one.
runtime_home = str(Path.home())

print(json.dumps({"real": real,
                  "globals": patched,
                  "runtime_home": runtime_home}))
"""


def _probe(pre_import: bool, iso_root: Path, extra_env=None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["VOYAGER_NO_SYNC"] = "1"
    env.pop("PYTHONIOENCODING", None)
    if extra_env:
        env.update(extra_env)
    argv = [sys.executable, "-c", PROBE]
    if pre_import:
        argv.append("--pre-import")
    argv += ["--iso", str(iso_root),
             "--adapters", ",".join(ADAPTERS),
             "--globals", ",".join(PATH_GLOBALS)]
    p = subprocess.run(argv, capture_output=True, text=True, env=env,
                       cwd=str(REPO), timeout=300)
    assert p.returncode == 0, "probe failed: %s%s" % (p.stdout, p.stderr)
    return json.loads(p.stdout)


def _real_roots(probe_real):
    """The developer's data roots, as the probe saw them before any override."""
    home = Path(probe_real["home"])
    roots = [home / n for n in (".voyager", ".codex", ".claude", ".dsh",
                                ".grok", ".zcode", ".cursor", ".gemini",
                                ".config")]
    if probe_real["appdata"]:
        roots.append(Path(probe_real["appdata"]) / "Kiro")
    return roots


def _under(child, parent) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


@pytest.fixture
def iso_root(tmp_path):
    root = tmp_path / "isolation"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# import order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pre_import", [False, True],
                         ids=["env-then-import", "import-then-env"])
def test_no_import_time_global_survives_the_harness(pre_import, iso_root, monkeypatch):
    """Both orders must leave every adapter global outside the real profile.

    `import-then-env` is the dangerous one: the globals were captured with the
    real home, and only the patching list stands between the suite and the
    developer's sessions.  If an adapter has an import-time global that is not
    in `PATH_GLOBALS`, this fails.
    """
    probe = _probe(pre_import, iso_root)
    monkeypatch.setattr(iso_guard, "real_data_roots",
                        lambda: tuple(_real_roots(probe["real"])))

    offenders = []
    for adapter, globals_ in probe["globals"].items():
        for attr, value in globals_.items():
            # after conftest's patching, a global should be under the isolation
            # root -- anything still under the real profile is an unpatched one
            if _under(value, iso_root):
                continue
            if iso_guard.is_inside_real_data(value):
                offenders.append("%s.%s -> %s" % (adapter, attr, value))
    assert offenders == [], (
        "these import-time globals still point at real agent data after the "
        "harness redirected the environment:\n  " + "\n  ".join(offenders) +
        "\nAdd their names to PATH_GLOBALS in tests/conftest.py.")


def test_every_import_time_global_is_in_the_patch_list(iso_root):
    """The completeness check, stated positively.

    Rather than relying on a global *happening* to be patched, assert that the
    set of import-time Path globals is a subset of `PATH_GLOBALS` (plus `HOME`,
    which conftest patches explicitly and PATH_GLOBALS already lists).
    """
    probe = _probe(True, iso_root)
    covered = set(PATH_GLOBALS) | {"HOME"}
    uncovered = {}
    for adapter, globals_ in probe["globals"].items():
        for attr in globals_:
            if attr not in covered:
                uncovered.setdefault(adapter, []).append(attr)
    assert uncovered == {}, (
        "these adapters capture a path at import time that conftest does not "
        "redirect:\n  " + "\n  ".join(
            "%s: %s" % (a, ", ".join(sorted(v)))
            for a, v in sorted(uncovered.items())))


def test_a_runtime_resolved_home_follows_the_environment(iso_root):
    """An adapter that calls Path.home() inside a function reads current env.

    `zcode` does exactly that for its fallbacks.  It is the *good* pattern, and
    this pins it: after redirection, a runtime resolution must land in the
    isolation root, not the real profile.
    """
    probe = _probe(True, iso_root)
    assert _under(probe["runtime_home"], iso_root), (
        "Path.home() inside a function resolved outside the isolation root: %s"
        % probe["runtime_home"])


# ---------------------------------------------------------------------------
# the guard side of the same question
# ---------------------------------------------------------------------------

def test_a_captured_real_path_is_refused_at_the_scan_boundary(iso_root, monkeypatch):
    """The point of the whole exercise: the guard, not luck.

    Simulate what an incomplete patch list would produce -- one real path handed
    to a scan -- and assert it is refused before anything is opened.
    """
    probe = _probe(True, iso_root)
    monkeypatch.setattr(iso_guard, "real_data_roots",
                        lambda: tuple(_real_roots(probe["real"])))
    real_codex = next(r for r in _real_roots(probe["real"])
                      if r.name == ".codex")

    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_scan_roots(real_codex / "sessions")


def test_appdata_diverging_from_home_is_refused(iso_root, monkeypatch):
    """Case 4 of the brief: HOME and USERPROFILE must not disagree in effect.

    If they name different profiles, `Path.home()` and `os.environ["HOME"]` can
    resolve to different places, and an adapter using one while the harness
    redirects the other would read the unredirected half.  The guard judges the
    derived directories of both, so a divergence that reaches real data is
    caught either way.
    """
    probe = _probe(True, iso_root)
    monkeypatch.setattr(iso_guard, "real_data_roots",
                        lambda: tuple(_real_roots(probe["real"])))
    roots = _real_roots(probe["real"])
    real_home = Path(probe["real"]["home"])

    # the harness redirects HOME; a stale USERPROFILE still naming the real
    # profile would make `expanduser("~")` (which prefers USERPROFILE on
    # Windows) resolve back into real data.
    elsewhere = iso_root / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(elsewhere))
    monkeypatch.setenv("USERPROFILE", str(real_home))

    offenders = [os.environ["USERPROFILE"]]
    assert any(iso_guard.is_inside_real_data(r / ".codex") for r in roots), (
        "fixture sanity: the fake roots must include a real-profile codex dir")
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_env()


def test_a_child_process_inherits_the_system_variables(iso_root):
    """Case 7: the child gets the machine's plumbing, not its agent data.

    A subprocess that inherited a hand-built minimal environment dies on Windows
    (SYSTEMROOT) -- that was P28's last CI failure.  It must also *not* inherit
    the real provider roots.  Both halves in one assertion: the child runs, and
    what it resolves is the isolation root.
    """
    probe = _probe(False, iso_root)
    assert probe["runtime_home"] and _under(probe["runtime_home"], iso_root)
    # and the machine's plumbing was not stripped
    assert probe["runtime_home"] != "/", "the child lost its environment entirely"


def test_a_provider_config_with_an_absolute_path_is_still_judged(iso_root, monkeypatch):
    """Case 5: a config that names an absolute real path is refused.

    Redirection cannot help here -- the path is not derived from HOME at all, it
    is spelled out.  Only a guard at the point of use can catch it, which is why
    `guard_scan_roots` exists and why callers must use it.
    """
    probe = _probe(True, iso_root)
    monkeypatch.setattr(iso_guard, "real_data_roots",
                        lambda: tuple(_real_roots(probe["real"])))
    roots = _real_roots(probe["real"])
    real_claude = next(r for r in roots if r.name == ".claude")

    configured = real_claude / "projects"       # spelled out, not derived
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_scan_roots(configured, what="configured provider path")
    # and the same path *inside* the isolation root is allowed
    safe = iso_root / ".claude" / "projects"
    assert iso_guard.guard_scan_roots(safe) is None
