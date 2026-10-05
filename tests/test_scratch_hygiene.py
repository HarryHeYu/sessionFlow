"""Scratch hygiene (O2 standing rule): the suite writes no test artifacts to
the C: temp dirs.

The rule: pytest temp trees, benchmark scratch, generated SQLite fixtures and
logs land under ``E:/sessionflow-scratch`` (overridable via
``VOYAGER_SCRATCH_ROOT``) — never ``C:\Windows\TEMP`` or the user profile's
``AppData\Local\Temp``.  ``tests/conftest.py::pytest_configure`` performs the
redirection; these tests fail loudly if it ever stops holding.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _allowed_roots() -> list[Path]:
    root = Path(os.environ.get("VOYAGER_SCRATCH_ROOT", "E:/sessionflow-scratch"))
    return [root.resolve()]


def _is_under(child: Path, parents: list[Path]) -> bool:
    try:
        child.resolve().relative_to(parents[0])
        return True
    except ValueError:
        return False


def test_tempfile_gettempdir_resolves_to_the_e_drive_scratch():
    got = Path(tempfile.gettempdir())
    assert _is_under(got, _allowed_roots()), \
        f"tempfile.gettempdir() = {got} — C: temp dirs are off limits"


def test_mkdtemp_creates_under_the_e_drive_scratch():
    d = tempfile.mkdtemp(prefix="o2-hygiene-")
    try:
        assert _is_under(Path(d), _allowed_roots()), \
            f"mkdtemp() = {d} — C: temp dirs are off limits"
    finally:
        os.rmdir(d)


def test_pytest_basetemp_is_on_the_e_drive_scratch(request):
    base = Path(request.config.option.basetemp)
    assert _is_under(base, _allowed_roots()), \
        f"pytest basetemp = {base} — pass --basetemp explicitly if you must"


def test_environment_tmp_variables_point_at_the_scratch():
    for var in ("TMP", "TEMP"):
        got = os.environ.get(var)
        assert got, f"{var} unset"
        assert _is_under(Path(got), _allowed_roots()), \
            f"{var}={got} — child processes would write C: temp"
