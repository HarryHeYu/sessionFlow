"""Simulate a core-only install and run the suite.

`pip install voyager` with no extras is the supported core install, and CI covers
that for real -- the `core-only` job installs `-e .` plus pytest and nothing
else. This script exists for the other machine: a development checkout that
already has `.[all,dev]`, where the extras-gated tests would otherwise run and
report a pass/skip figure no core-only install could produce. It recreates the
core environment in-process by making the optional dependencies unimportable.

Every extra except pytest itself has to be listed in `BLOCKED`. `dev` pulls in
Pillow, and `test_diagram.py` gates on `PIL`, so blocking only `mcp` and
`zstandard` left those three tests running and silently inflated the count.
"""
import importlib.abc
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # `python scripts/...` puts scripts/ on the path

BLOCKED = {"mcp", "zstandard", "PIL"}


class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


def install_blocker() -> None:
    """Make the optional dependencies unimportable for this process."""
    sys.meta_path.insert(0, Block())


def main() -> int:
    install_blocker()
    import pytest  # imported after the blocker so it cannot be shadowed
    return pytest.main(["tests/", "-q", "--no-header", "-rs"])


if __name__ == "__main__":
    raise SystemExit(main())
