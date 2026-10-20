"""The test suite must not read or write the developer's real agent data.

`tests/conftest.py` redirects HOME, USERPROFILE, APPDATA, the default index
path, the working directory and every adapter's storage globals.  These tests
pin that redirection, because it is invisible when it works and destructive when
it does not: a bug in the `--db` plumbing once let CLI tests run against — and
prune — the real `~/.voyager/index.db`, and the plugin's suite wrote 186 bundles
into a real `~/.voyager/bundles/` before anyone noticed.

The predicate is **"not inside a real provider data directory"**, deliberately
not "not inside the home".  pytest's own temp root lives under the home on some
systems — on a GitHub Windows runner it is
`C:\\Users\\runneradmin\\AppData\\Local\\Temp\\pytest-of-runneradmin` — so a
"not under the home" assertion fails there for the wrong reason and teaches
nothing.  What matters is that the *product's* locations are not touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Captured at import time, which happens during collection — before the
#: autouse fixture has run.  This is the developer's actual home.
REAL_HOME = Path(os.path.expanduser("~"))

#: The directories the product reads and writes for a user.  A redirected path
#: must not be inside any of them.
REAL_DATA_DIRS = tuple(
    REAL_HOME / name for name in (
        ".voyager", ".codex", ".claude", ".dsh", ".grok", ".zcode",
        ".cursor", ".gemini", ".config",
    )
)


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _in_real_data(path: Path) -> bool:
    return any(_under(path, d) for d in REAL_DATA_DIRS)


def test_home_is_redirected():
    home = Path.home()
    assert home != REAL_HOME, (
        "HOME/USERPROFILE still point at the real profile: a test that forgets "
        "to redirect an adapter would read and overwrite real agent sessions")
    assert home.exists()


def test_the_default_index_is_not_the_real_one():
    from voyager.store import default_db_path

    db = Path(default_db_path())
    assert not _in_real_data(db), "the default index resolves into real agent data"


def test_the_bundles_directory_is_not_the_real_one():
    from voyager.continuity import get_bundles_dir

    assert not _in_real_data(get_bundles_dir()), (
        "bundles would be written into the real ~/.voyager/bundles/")


def test_the_working_directory_is_not_the_checkout():
    """`continue`/`handoff` default to relative output paths.

    A test that ran in the checkout once dropped a package into it.  The
    assertion is about the checkout, not the home: pytest's temp root is under
    the home on a CI runner, and that is fine.
    """
    cwd = Path.cwd().resolve()
    repo = Path(__file__).resolve().parent.parent
    assert cwd != repo, "the test cwd is the repository root"
    assert repo not in cwd.parents, "the test cwd is inside the checkout"


def test_every_adapter_storage_global_points_somewhere_harmless(
        _isolated_from_the_real_machine):
    """A forgotten redirect must discover nothing, not damage something.

    ``_isolated_from_the_real_machine`` returns the non-existent directory the
    adapters were pointed at; the fixture is requested by name here so this test
    fails loudly if it is ever removed.
    """
    import importlib

    missing = _isolated_from_the_real_machine
    assert not missing.exists(), (
        "the isolation target exists, so a forgotten redirect could write to it")

    offenders = []
    for name in ("codex", "claude", "grok", "zcode", "cursor", "kiro",
                 "antigravity", "dsh"):
        try:
            mod = importlib.import_module("voyager.adapters.%s" % name)
        except ImportError:
            continue
        for attr in dir(mod):
            if not attr.isupper():
                continue
            value = getattr(mod, attr)
            if isinstance(value, Path) and _in_real_data(value):
                offenders.append("%s.%s -> %s" % (name, attr, value))
    assert offenders == [], (
        "these adapter globals still point inside real agent data:\n  "
        + "\n  ".join(offenders))


def test_a_real_bundle_write_cannot_reach_the_real_profile(synthetic_trio):
    """Run the actual write path and prove the real bundles dir is untouched.

    This is the behavioural half: the tests above assert the redirection is in
    place, this one runs a real `handoff_thread` through its **default** output
    path and checks the developer's profile afterwards.  It is exactly the leak
    the plugin suite had — 186 bundles in a real `~/.voyager/bundles/`.
    """
    from voyager.continuity import handoff_thread

    store, rows = synthetic_trio
    real_bundles = REAL_HOME / ".voyager" / "bundles"

    def snapshot():
        if not real_bundles.is_dir():
            return None
        return sorted(p.name for p in real_bundles.iterdir())

    before = snapshot()
    res = handoff_thread(store, sessions=rows, goal="isolation check",
                         style="continuation")          # no explicit output
    written = Path(res["context_path"])

    assert written.exists(), "the bundle was not written at all"
    assert not _in_real_data(written), (
        "the bundle landed inside real agent data: %s" % written)
    assert snapshot() == before, (
        "a bundle was written into the real ~/.voyager/bundles/")


def test_the_appdata_adapter_cannot_reach_real_kiro_sessions(
        _isolated_from_the_real_machine):
    """Kiro is the one adapter that reads %APPDATA% instead of deriving from HOME.

    Redirecting HOME and USERPROFILE alone does not move it, because `APPDATA` is
    an independent Windows variable.  A manual CLI run that redirects only HOME
    therefore still indexes the developer's real Kiro sessions — that happened
    during an audit, where `voyager scan` found seven real sessions in an
    otherwise "isolated" sandbox.  The autouse fixture redirects APPDATA as well,
    which is what closes it; this test pins that line, because deleting it would
    reintroduce the leak without failing anything else.

    It also documents why the fix is here and not in the adapter: for a real
    user, reading their own Kiro data is correct.  Only the isolation harness
    needs it moved.
    """
    from voyager.adapters.kiro import kiro_agent_dir

    assert kiro_agent_dir() is None, (
        "kiro discovery can reach real sessions under the isolation fixture: "
        "the fixture no longer redirects APPDATA")


def test_no_adapter_discovers_anything_under_the_isolation_fixture(
        _isolated_from_the_real_machine):
    """The whole point, stated once, over every adapter.

    `_isolated_from_the_real_machine` redirects HOME, USERPROFILE, APPDATA and
    every module-level path global.  After that, a full discovery pass must find
    nothing — otherwise a test that forgets to redirect could index real data.
    """
    from voyager.adapters import load_all
    from voyager.adapters.base import enabled_adapters

    load_all()
    found = {}
    for ad in enabled_adapters():
        try:
            sources = ad.discover()
        except Exception as exc:            # noqa: BLE001 - reported below
            found[ad.provider] = "raised {0}".format(exc)
            continue
        if sources:
            found[ad.provider] = len(sources)
    assert not found, (
        "these adapters still discover real sources under isolation: %s" % found)


# --- the guard's snapshot must come from the real profile --------------------
# `iso_guard.real_data_roots()` is a module-level snapshot.  If anything ever
# imports the module lazily -- inside a fixture, or a test -- the snapshot is
# taken *after* redirection and describes the temp directory instead.  The
# result is not a quiet pass: every redirection then looks like agent data and
# the whole suite is refused with a message about the environment, which is a
# symptom three steps away from the cause.  This assertion names the cause.

def test_iso_guard_snapshotted_the_real_profile_not_the_redirected_one():
    import iso_guard

    roots = [Path(r) for r in iso_guard.real_data_roots()]
    assert roots, "no data roots were captured at all"

    outside = [str(r) for r in roots
               if not _under(r, REAL_HOME)
               and not _under(r, Path(os.environ.get("APPDATA", str(REAL_HOME))))]

    assert outside == [], (
        "these data roots are not under the real profile, so iso_guard was "
        "imported after the harness redirected HOME/APPDATA:/n  "
        + "\n  ".join(outside)
        + "\nImport it at conftest module level (collection time), never "
          "lazily inside a fixture or a test.")


def test_the_guard_refuses_a_path_inside_a_real_root():
    """The behavioural half: the snapshot is wired to the predicate."""
    import iso_guard

    root = Path(iso_guard.real_data_roots()[0])
    with pytest.raises(iso_guard.IsolationViolation):
        iso_guard.guard_not_real_data(root, "scan root")
