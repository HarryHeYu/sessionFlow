"""sessionFlow — local session manager for AI coding agents.

``__version__`` is resolved lazily on first access, not at import: the CLI is
startup-sensitive, and `importlib.metadata` walks `sys.path` looking for the
distribution.  Importing the package therefore stays free, and the value still
comes from one place (see `voyager._version`).
"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "__version__":
        from ._version import get_version

        return get_version()
    raise AttributeError("module {0!r} has no attribute {1!r}".format(__name__, name))


def __dir__() -> list:
    return sorted(list(globals()) + ["__version__"])
