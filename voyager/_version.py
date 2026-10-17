"""Where the version comes from — one answer, no second constant.

Installed, the answer is the **distribution metadata** (`pip show voyager`,
`importlib.metadata.version`), which is what a bug report needs and what the
release actually shipped.

Run from a source checkout that was never installed, there is no metadata to
read — `*.egg-info/` is gitignored, so a fresh clone has none.  In that case the
answer comes from ``pyproject.toml``, which is the same single source the build
reads.  Nothing here duplicates a literal version, so nothing can drift out of
step with a release.

Deliberately *not* a module-level ``VERSION = "1.1.0"``: that is the drift this
module exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

#: Names the distribution may be installed under.  `voyager` is the current one;
#: `sessionflow` is the name the project would need in order to publish (the
#: `voyager` name on PyPI belongs to Spotify's approximate-nearest-neighbour
#: library, so it can never be ours).  Kept only as a fallback — see
#: `_from_metadata`, which prefers the distribution that actually owns this file.
DIST_NAMES = ("voyager", "sessionflow")

#: What to report when neither source answers.  Should not happen in a normal
#: install or checkout; saying so is better than inventing a number.
UNKNOWN = "unknown"


def _owns_this_module(dist: object) -> bool:
    """True when `dist` is the distribution that installed `voyager/_version.py`.

    This is the reliable way to identify ourselves.  Looking the version up by
    *name* is not: a different, unrelated package called `voyager` exists on
    PyPI, so `version("voyager")` can answer for the wrong project when both are
    installed — and it answers for nobody once this one is renamed.
    """
    try:
        files = dist.files or ()
    except Exception:  # noqa: BLE001 - a broken RECORD should not crash --version
        return False
    for entry in files:
        if str(entry).replace("\\", "/").endswith("voyager/_version.py"):
            return True
    return False


def _from_metadata() -> Optional[str]:
    """The installed distribution's version, or None when not installed."""
    try:
        from importlib.metadata import PackageNotFoundError, distributions, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return None

    # 1. Whoever owns this file.  Correct under any distribution name, and
    #    immune to the PyPI collision above.
    try:
        for dist in distributions():
            if _owns_this_module(dist):
                found = dist.version
                if found:
                    return found
    except Exception:  # noqa: BLE001 - metadata can fail oddly on broken installs
        pass

    # 2. Fall back to the declared name.  Needed for installs that do not record
    #    their files (some editable installs), where step 1 cannot answer.
    for name in DIST_NAMES:
        try:
            found = version(name)
        except PackageNotFoundError:
            continue
        except Exception:  # noqa: BLE001
            continue
        if found:
            return found
    return None


def _pyproject_path() -> Path:
    return Path(__file__).resolve().parent.parent / "pyproject.toml"


def _from_pyproject() -> Optional[str]:
    """`project.version` from the checkout's pyproject.toml, or None."""
    path = _pyproject_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        tomllib = None
    if tomllib is not None:
        try:
            project = tomllib.loads(text).get("project") or {}
            value = project.get("version")
            if isinstance(value, str) and value:
                return value
        except Exception:  # noqa: BLE001 - fall through to the line scan
            pass

    # Python 3.10 has no tomllib and this project does not depend on tomli for
    # it, so read the one key we need.  Anchored to the `[project]` table so a
    # `version` under `[tool.*]` can never be mistaken for it.
    in_project = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_project = line == "[project]"
            continue
        if in_project:
            m = re.match(r'version\s*=\s*["\']([^"\']+)["\']', line)
            if m:
                return m.group(1)
    return None


def get_version() -> str:
    """The version to show a user.  Metadata first, then the checkout."""
    return _from_metadata() or _from_pyproject() or UNKNOWN
