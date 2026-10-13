"""O6 architecture hygiene — the shared helper module (``voyager/util.py``).

Before O6, ``_same_repo`` existed in **five** places with **two** behaviours
(``startup`` matched exact/suffix only; ``api``/``auto``/``cli`` also matched
by substring) and ``_fmt_ts`` in **four** with two formats.  The same name hid
different meaning, which is exactly how two call sites end up disagreeing about
what "the same repo" is.

These tests pin two things:

1. **Behaviour** — ``same_repo`` (strict) and ``same_repo_loose`` (substring)
   are genuinely different, and the difference is a deliberate, tested choice
   rather than an accident of copy-paste.
2. **No re-duplication** — no module under ``voyager/`` defines its own copy of
   the helpers, and every former duplicator imports the canonical one.
"""

from __future__ import annotations

import ast
from pathlib import Path

from voyager.util import (fmt_ts, fmt_ts_seconds, parse_ts, same_repo,
                          same_repo_loose)

VOYAGER = Path(__file__).resolve().parent.parent / "voyager"

#: Names that must have exactly one definition, in ``voyager/util.py``.
SHARED = {"same_repo", "same_repo_loose", "fmt_ts", "fmt_ts_seconds",
          "parse_ts"}
#: The private names the consolidation removed; must never come back.
RETIRED = {"_same_repo", "_fmt_ts"}


# --- behaviour --------------------------------------------------------------

def test_same_repo_is_strict():
    assert same_repo("E:/code/voyager", "E:/code/voyager") is True
    assert same_repo("E:/code/voyager", "voyager") is True           # path suffix
    assert same_repo("E:/code/voyager", "code") is False             # NOT substring
    assert same_repo("E:/code/voyager_old", "voyager") is False      # NOT a prefix trap
    assert same_repo("E:\\code\\voyager", "e:/code/voyager") is True  # separator + case
    assert same_repo("", "x") is False
    assert same_repo("x", "") is False
    assert same_repo(None, "x") is False


def test_same_repo_loose_allows_substring():
    assert same_repo_loose("E:/models/black_box", "black_box") is True
    assert same_repo_loose("E:/code/voyager", "code") is True


def test_loose_is_a_superset_of_strict():
    """Anything the strict match accepts, the loose match must accept too."""
    cases = [
        ("E:/code/voyager", "E:/code/voyager"),
        ("E:/code/voyager", "voyager"),
        ("/code/voyager", "voyager"),
        ("E:/models/black_box", "black_box"),
        ("x/y", "y"),
        ("x/y", "x"),
        ("a", "b"),
        ("", "x"),
    ]
    for a, b in cases:
        if same_repo(a, b):
            assert same_repo_loose(a, b), (a, b)


def test_the_two_variants_genuinely_differ():
    """The divergence that used to be silent is now a named, tested choice.

    The distinguishing case is a substring that is **not** a path suffix:
    ``code`` sits inside ``E:/code/voyager`` but is not a suffix, so strict
    rejects it and loose accepts it.  (``black_box`` would NOT be a good
    example — it *is* a suffix of ``E:/models/black_box``, so both accept it.)
    """
    assert same_repo("E:/code/voyager", "code") is False
    assert same_repo_loose("E:/code/voyager", "code") is True


def test_fmt_ts_shapes():
    assert fmt_ts(None) == "?"
    assert fmt_ts(0) == "?"
    assert fmt_ts_seconds(None) == "?"
    t = 1757000000.0
    # "YYYY-MM-DD HH:MM" has one colon; the seconds variant has two.
    assert fmt_ts(t).count(":") == 1
    assert fmt_ts_seconds(t).count(":") == 2
    assert len(fmt_ts_seconds(t)) > len(fmt_ts(t))


def test_parse_ts_accepts_the_shapes_the_providers_emit():
    """claude / codex / grok all emit ISO-8601, with either Z or an offset."""
    assert parse_ts("2026-08-02T10:00:00Z") == parse_ts("2026-08-02T10:00:00+00:00")
    assert parse_ts("2026-08-02T10:00:00Z") is not None
    # an explicit offset is honoured, not treated as UTC
    assert parse_ts("2026-08-02T10:00:00+02:00") != parse_ts("2026-08-02T10:00:00Z")


def test_parse_ts_is_lenient_about_missing_and_bad_values():
    """A bad timestamp must cost one event, never the whole session."""
    for bad in (None, "", "not a timestamp", "2026-13-45T99:99:99Z"):
        assert parse_ts(bad) is None, bad


# --- no re-duplication ------------------------------------------------------

def _defined_function_names(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name


def _util_imported_names(path: Path):
    """Names imported from ``voyager.util``.

    Handles both ``from .util import x`` (level 1, same package) and
    ``from ..util import x`` (level 2, one package down, as the adapters use).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "util" \
                and node.level in (1, 2):
            names.update(a.name for a in node.names)
    return names


def test_helpers_are_defined_only_in_util():
    """A second definition of a shared helper is the bug this guards against."""
    offenders = []
    for path in sorted(VOYAGER.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for name in _defined_function_names(path):
            if name in RETIRED:
                offenders.append(f"{path.name}: def {name}")
            if name in SHARED and path.name != "util.py":
                offenders.append(f"{path.name}: def {name}")
    assert offenders == [], (
        "shared helpers must live only in voyager/util.py:\n  "
        + "\n  ".join(offenders))


def test_former_duplicators_import_the_canonical_helper():
    """Every module that used to carry a copy imports the canonical one.

    This is the positive half of the guard: it is not enough that the copy is
    gone, the module has to actually use the shared definition.
    """
    expected = {
        "api.py": "same_repo_loose",
        "auto.py": "same_repo_loose",
        "cli.py": "same_repo_loose",
        "startup.py": "same_repo",
        "continuity.py": "fmt_ts",
        "handoff.py": "fmt_ts",
        "export.py": "fmt_ts_seconds",
        # P7: parse_ts was byte-identical in these three adapters
        "adapters/claude.py": "parse_ts",
        "adapters/codex.py": "parse_ts",
        "adapters/grok.py": "parse_ts",
    }
    missing = []
    for mod, fn in expected.items():
        imported = _util_imported_names(VOYAGER / mod)
        if fn not in imported:
            missing.append(f"{mod} does not import {fn} from .util (got {sorted(imported)})")
    assert missing == [], "\n  ".join(missing)
