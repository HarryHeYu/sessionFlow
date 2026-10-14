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
