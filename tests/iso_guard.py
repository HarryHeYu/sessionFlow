"""Fail-closed isolation guards for the Voyager test suite.

``tests/conftest.py`` redirects the environment so a test that *forgets* to
point an adapter somewhere harmless discovers nothing instead of reading the
developer's agent history.  That is a safety net for **forgetting**.  It does
nothing about a test that *passes a real path explicitly* -- and that is what
happened once already: an identity-matrix harness set HOME and USERPROFILE,
left APPDATA alone, and scanned a real Kiro profile out of
``%APPDATA%\\Kiro``.

This module is the other half.  It answers one question, defensively, for any
path a test is about to hand to a scan, a reader or a writer:

    does this resolve into a directory the product treats as a user's data?

If the answer is yes the caller is refused -- before anything is opened.  A
guard that only warns would let the very accident it exists to prevent.

Design notes that are load-bearing, not decoration:

* **The predicate is about data directories, not about "the home".**  pytest's
  own temp root lives under the home on some hosts -- on a GitHub Windows
  runner it is ``C:\\Users\\runneradmin\\AppData\\Local\\Temp\\pytest-of-...``
  -- and the Python/Node executables used by tests live under ``Program
  Files`` or the home.  Rejecting the whole home would make CI fail for a
  reason unrelated to isolation and teach the next reader nothing.  Only the
  directories the product actually reads and writes for a user are roots.

* **Non-existent paths are compared through their nearest existing ancestor.**
  A guard that calls ``resolve()`` and gives up on a missing path would happily
  approve ``C:\\Users\\me\\.codex\\sessions\\new-file.jsonl`` -- exactly the
  path a scan creates next.

* **Windows comparison is case-insensitive and normalized.**  ``C:\\USERS\\
  ME\\.CODEX`` and ``c:\\users\\me\\.codex`` are the same directory, and a
  guard that disagrees is a guard with a hole in it.

* **Symlinks are followed.**  A link planted inside a temp dir pointing at the
  real profile is the obvious bypass, and ``resolve()`` closes it.

* **Real roots are captured at import time**, which happens during collection,
  before the autouse fixture has redirected anything.  Capturing after
  redirection would make every real root look like a temp root and the guard
  would approve everything.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Where the product keeps a user's data, relative to that user's home.  This
#: is a *targeted* list, not "everything under the home": see the module
# docstring for why the difference matters on CI runners.
_HOME_DATA_DIRS = (
    ".voyager", ".codex", ".claude", ".dsh", ".grok", ".zcode",
    ".cursor", ".gemini", ".config",
)

#: Agent data under ``%APPDATA%`` on Windows.  Kiro is the one adapter that
#: resolves through APPDATA instead of HOME, which is how the incident this
#: module exists for happened.
_APPDATA_DATA_DIRS = ("Kiro",)


class IsolationViolation(AssertionError):
    """Raised when a test is about to touch real agent data.

    An ``AssertionError`` on purpose: pytest reports it as a failure with the
    message intact, rather than as an error in collection.
    """


def _real_home() -> Path:
    return Path(os.path.expanduser("~"))


def _real_appdata() -> Path:
    value = os.environ.get("APPDATA")
    return Path(value) if value else Path(os.path.expanduser("~")) / "AppData" / "Roaming"


def _compute_roots() -> tuple:
    home = _real_home()
    roots = [home / name for name in _HOME_DATA_DIRS]
    roots += [_real_appdata() / name for name in _APPDATA_DATA_DIRS]
    # the profile root itself is not a data dir (temp and executables live
    # there), so it is deliberately absent.
    return tuple(r for r in roots if str(r).strip())


#: The snapshot.  Evaluated when the module is first imported, which
#: ``tests/conftest.py`` arranges to be collection time -- before the isolation
#: fixture redirects anything.  A function that recomputed this per call would
#: capture the temp directory instead and refuse the redirection it protects.
_REAL_ROOTS = _compute_roots()


def real_data_roots() -> tuple:
    """Directories the product reads and writes for the current user.

    Snapshotted once, at import.  ``conftest`` redirects HOME/APPDATA for each
    test, so recomputing this per call would return the *temp* directory as
    "the real profile" -- at which point the redirection the guard exists to
    protect looks like agent data and the whole suite is refused.  ``conftest``
    imports this module at collection time so the snapshot is taken before any
    fixture runs; ``tests/test_isolation.py`` pins that ordering.
    """
    return _REAL_ROOTS


def _normcase(p: Path) -> str:
    """Windows-fold a path so casing cannot smuggle a real root past us."""
    return os.path.normcase(os.path.normpath(str(p)))


def _nearest_existing(p: Path) -> Path:
    """``p`` if it exists, else its closest existing ancestor.

    A missing leaf must still be checked against the root it would be created
    under: ``~/.codex/sessions/not-yet-written.jsonl`` is real data.
    """
    p = p.expanduser()
    for candidate in (p, *p.parents):
        if candidate.exists():
            return candidate
    return p


def _target_keys(path) -> tuple:
    """Every spelling a target can be matched under.

    Two forms, because each catches a different bypass:

    * the **literal** absolute path -- ``~/.codex/sessions/new.jsonl`` does not
      exist yet, and resolving it through its nearest existing ancestor would
      collapse the missing leaf away;
    * the **resolved** path -- a symlink planted inside a temp tree pointing at
      the real profile has a harmless literal path and a damning resolved one.
    """
    p = path if isinstance(path, Path) else Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    keys = [_normcase(p)]
    try:
        resolved = _nearest_existing(p).resolve()
    except (OSError, ValueError, RuntimeError):
        resolved = None
    if resolved is not None:
        key = _normcase(resolved)
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _root_keys(root: Path) -> tuple:
    """Spellings for a data root -- literal, plus resolved only if it exists.

    The ``exists()`` test is load-bearing.  A root that does not exist yet
    (``~/.config`` on a fresh machine) resolves to the *home directory*, and
    adding that as a root would make the home itself -- pytest's temp root, the
    interpreters the suite runs under -- look like agent data.  The literal key
    still catches the case that matters: a target under a root the product has
    not created yet is still inside that root.
    """
    keys = [_normcase(root)]
    try:
        if root.exists():
            key = _normcase(root.resolve())
            if key not in keys:
                keys.append(key)
    except (OSError, ValueError, RuntimeError):
        pass
    return tuple(keys)


def is_inside_real_data(path) -> bool:
    """True if ``path`` resolves into a directory holding real agent data.

    ``path`` may be relative, missing, or a symlink; all three are common and
    all three are handled.  Non-path values (``None``, ``""``) are not real
    data paths and return False rather than raising, so callers can guard
    unconditionally.
    """
    if path is None or path == "":
        return False
    try:
        targets = _target_keys(path)
    except (OSError, ValueError, RuntimeError):
        # An unresolvable path (loop, permission, device) is not provably
        # safe, so it must not be waved through either.
        return True

    for root in real_data_roots():
        roots = _root_keys(root)
        for target in targets:
            for root_key in roots:
                if target == root_key or target.startswith(root_key + os.sep):
                    return True
    return False


def guard_not_real_data(path, what: str = "path"):
    """Return ``path`` as a ``Path``, or raise if it is real agent data."""
    if is_inside_real_data(path):
        raise IsolationViolation(
            "refusing to run: %s resolves into real agent data.\n"
            "  %s\n"
            "Point it at a synthetic tree inside tmp_path (or the fixture's\n"
            "generated root) instead of the developer's own sessions.\n"
            "The isolation harness in tests/conftest.py redirects HOME,\n"
            "USERPROFILE, APPDATA, LOCALAPPDATA and every adapter's storage\n"
            "globals; an explicit real path bypasses all of it."
            % (what, Path(path) if path is not None else path))
    return Path(path) if path is not None else None


def guard_scan_roots(*roots, what: str = "scan root"):
    """Refuse before a scan if any root would read real agent data."""
    for r in roots:
        guard_not_real_data(r, what)


# ---------------------------------------------------------------------------
# environment-side checks
# ---------------------------------------------------------------------------

#: Variables that steer discovery.  ``HOME`` and ``APPDATA`` are the two the
#: adapters actually read; the rest are here so a future adapter that adds
#: them cannot reintroduce the same hole unnoticed.
_DISCOVERY_ENV = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                  "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")

#: Which data directories each variable *derives*.  Checking the variable's
#: own value is not enough: ``HOME=C:\\Users\\me`` does not look like agent
#: data, but ``$HOME/.codex`` is, and that is what the adapters open.  This is
#: the other half of the Kiro incident -- HOME and USERPROFILE were redirected
#: and APPDATA was not, so the derived path was the real one.
_DERIVED_DATA_DIRS = {
    "HOME": _HOME_DATA_DIRS,
    "USERPROFILE": _HOME_DATA_DIRS,
    "APPDATA": _APPDATA_DATA_DIRS,
}


def guard_env(environ=None) -> None:
    """Refuse to run when discovery would reach real agent data.

    Each variable is judged on the data directories it *derives*, not merely on
    its own spelling -- ``HOME=<profile>`` is only dangerous because of what the
    adapters build underneath it.  Variables that derive nothing today are still
    checked for their literal value, so a future adapter that starts reading
    ``LOCALAPPDATA`` cannot quietly reopen the hole.
    """
    env = os.environ if environ is None else environ

    offenders = []
    for name in _DISCOVERY_ENV:
        value = env.get(name)
        if not value:
            continue
        base = Path(value)
        candidates = [base]
        candidates += [base / sub for sub in _DERIVED_DATA_DIRS.get(name, ())]
        for candidate in candidates:
            if is_inside_real_data(candidate):
                offenders.append("%s=%s   (derives %s)" % (name, value,
                                                           candidate))
                break
    if offenders:
        raise IsolationViolation(
            "refusing to run: discovery environment still reaches real "
            "agent data:\n  " + "\n  ".join(offenders) +
            "\nThe isolation harness in tests/conftest.py must redirect these "
            "before any test body runs.")


def _refuse_if_roots_were_captured_too_late(env) -> None:
    """Diagnose the one way this module can be imported wrongly.

    ``real_data_roots()`` is a snapshot taken on import.  Import the module
    after ``conftest`` has redirected ``HOME`` and it captures the *temp*
    directory as "the real profile" -- at which point every redirection looks
    like agent data and the whole suite is refused with a message about the
    environment, which is a symptom rather than the cause.

    The signal is that a root's parent is the currently-redirected home.  It is
    deliberately not raised from ``guard_env``: a monkeypatched ``real_data_roots``
    (the pattern the guard's own tests use) looks identical, and a false
    diagnosis is worse than none.  ``tests/test_isolation.py`` pins the correct
    ordering positively instead -- every root must sit under the real profile.
    """
    redirected = env.get("USERPROFILE") or env.get("HOME")
    if not redirected:
        return
    lowered = str(redirected).lower()
    for root in _REAL_ROOTS:
        parent = os.path.normcase(os.path.normpath(str(root.parent)))
        if parent == os.path.normcase(os.path.normpath(lowered)):
            raise IsolationViolation(
                "iso_guard was imported AFTER the isolation harness redirected "
                "HOME, so its 'real' data roots point at %s.\n"
                "Import tests/iso_guard.py at conftest module level -- "
                "collection time, before any fixture runs -- not lazily inside "
                "a fixture or a test." % redirected)
