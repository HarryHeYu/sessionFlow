"""`voyager demo --db` must seed the requested index, not the default one.

The global `--db` flag is documented as valid on either side of the
subcommand.  `demo` was the exception: the subparser did not inherit the common
arguments, and `_cmd_demo` called `demo.main()` with no path, so an explicit
database was silently ignored and the synthetic story landed in the default
home.  Regression for R14 ENG-1.
"""

from __future__ import annotations

import pytest

from voyager.cli import main
from voyager.demo import DEMO_DB_NAME


@pytest.mark.parametrize("db_before_subcommand", [True, False])
def test_demo_honours_explicit_db(tmp_path, monkeypatch,
                                  db_before_subcommand, capsys):
    import voyager.store as store_mod

    fake_home_db = tmp_path / "home" / ".voyager" / "index.db"
    monkeypatch.setattr(store_mod, "default_db_path", lambda: fake_home_db)

    explicit = tmp_path / "custom" / "demo.db"
    argv = (["--db", str(explicit), "demo"] if db_before_subcommand
            else ["demo", "--db", str(explicit)])

    assert main(argv) == 0
    out = capsys.readouterr().out
    assert str(explicit) in out
    assert explicit.exists()
    assert explicit.parent / DEMO_DB_NAME == explicit

    default_demo = fake_home_db.parent / DEMO_DB_NAME
    assert not default_demo.exists()
    assert "Seeded a synthetic demo index" in out
