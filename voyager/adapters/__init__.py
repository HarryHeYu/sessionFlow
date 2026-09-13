"""Adapter registry — importing this module registers all built-in adapters."""

from __future__ import annotations

import importlib

_MODULES = ["codex", "claude", "zcode", "dsh"]

_loaded = False


def load_all() -> None:
    global _loaded
    if _loaded:
        return
    for m in _MODULES:
        try:
            importlib.import_module(f"voyager.adapters.{m}")
        except ImportError as e:
            # optional dependencies missing (e.g. zstandard) — skip adapter
            import sys
            print(f"voyager: skipping adapter '{m}': {e}", file=sys.stderr)
    _loaded = True
