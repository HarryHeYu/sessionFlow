"""Simulate a core-only install (no zstandard / no mcp) and run the suite."""
import importlib.abc
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # `python scripts/...` puts scripts/ on the path

BLOCKED = {"mcp", "zstandard"}


class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


sys.meta_path.insert(0, Block())

import pytest  # noqa: E402

raise SystemExit(pytest.main(["tests/", "-q", "--no-header", "-rs"]))
