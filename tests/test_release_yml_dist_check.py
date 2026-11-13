"""Regression: the release workflow must flag *real* scratch directories only.

The release workflow (`.github/workflows/release.yml`, "Check the distributions")
asserts that neither the wheel nor the sdist carries scratch / private content.
It identifies scratch content by **path directory segment** -- a path component
that is exactly `scratch` -- so a legitimate file whose *name* merely contains the
substring "scratch" is left alone.

The previous check used the bare substring `"scratch" in n.lower()`, which falsely
flagged legitimate files such as `tests/test_scratch_hygiene.py` (a real test the
sdist ships on purpose) and any module named, say, `scratchpad.py`. This broke the
sdist leg of the release with no real leak present.

This test extracts the exact regex the workflow uses and exercises it against the
cases that matter, so the bug cannot silently return.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"

# The workflow calls re.search(r"<PATTERN>", n.lower()) on each archive member.
_PATTERN_RE = re.compile(r're\.search\(\s*r"((?:[^"\\]|\\.)*)"')


def _load_scratch_pattern() -> re.Pattern[str]:
    assert RELEASE_YML.is_file(), f"missing {RELEASE_YML}"
    text = RELEASE_YML.read_text(encoding="utf-8")
    # The fix must be in place: no bare substring check, real regex present.
    assert '"scratch" in n.lower()' not in text, (
        "release.yml still uses the bare substring check that false-flags "
        "legitimate files such as tests/test_scratch_hygiene.py"
    )
    matches = _PATTERN_RE.findall(text)
    assert matches, "could not find re.search(r'...') in release.yml"
    # Every scratch-check occurrence must use the directory-segment pattern.
    for pat in matches:
        assert re.search(r"\(\?:\^\|\/\)scratch\(\?:\/\|\$\)", pat), (
            f"scratch check uses an unexpected pattern: {pat!r}"
        )
    # All occurrences share the same pattern; compile the first.
    return re.compile(matches[0])


# (member name, should the workflow treat it as leaked scratch content?)
CASES = [
    # --- legitimate files that the OLD substring check wrongly flagged ---
    ("tests/test_scratch_hygiene.py", False),   # real test, shipped in the sdist
    ("voyager/scratchpad.py", False),           # module whose name contains "scratch"
    ("src/utils/scratch_buffer.py", False),     # same idea, nested
    ("myproject_scratch/session.py", False),    # dir name contains, but is not, "scratch"
    # --- real scratch / private content that MUST be blocked ---
    ("scratch/leaked.py", True),                # top-level scratch dir
    ("build/scratch/tmp.py", True),             # nested scratch dir
    ("a/b/scratch/c.py", True),                 # deeper scratch dir
    ("tests/scratch/foo.py", True),             # scratch dir under tests
]


def test_release_yml_scratch_check_is_segment_aware():
    pat = _load_scratch_pattern()
    for member, expect_leaked in CASES:
        got = pat.search(member.lower()) is not None
        assert got == expect_leaked, (
            f"{member!r}: expected leaked={expect_leaked}, got {got} "
            f"(pattern={pat.pattern!r})"
        )


def test_release_yml_sdist_still_blocks_git_and_matches_wheel_logic():
    # The sdist branch also guards /.git/; make sure that guard is intact and the
    # scratch logic is the directory-segment form in both branches.
    text = RELEASE_YML.read_text(encoding="utf-8")
    assert "/.git/" in text, "sdist private-path guard (/.git/) missing"
    # both the wheel and sdist branches reference the same directory-segment regex
    assert text.count('re.search(r"(?:^|/)scratch(?:/|$)"') >= 2, (
        "expected the directory-segment scratch regex in both the wheel and "
        "sdist branches"
    )


def _extract_leaked_exprs() -> tuple[str, str]:
    """Pull the two REAL `leaked = [...]` predicates out of the workflow.

    We do not re-implement the checks here: executing the workflow's own
    expressions is the only way to guarantee the test cannot drift away from
    what release.yml actually ships.  Each is a two-line list comprehension;
    we grab the source text from `leaked = [` through the matching `]`.
    """
    import ast

    text = RELEASE_YML.read_text(encoding="utf-8")
    lines = text.splitlines()
    exprs: list[str] = []
    for i, line in enumerate(lines):
        if line.strip().startswith("leaked = ["):
            # The comprehension spans this line and the following `if ...]`.
            block = line.strip()[len("leaked = "):]
            j = i + 1
            while "]" not in block:
                block += "\n" + lines[j].strip()
                j += 1
            # Keep only up to the closing bracket of the comprehension.
            block = block[: block.index("]") + 1]
            # Validate it is a real, parseable list comprehension.
            ast.parse(block, mode="eval")
            exprs.append(block)
    assert len(exprs) == 2, f"expected wheel + sdist leaked exprs, got {len(exprs)}"
    # wheel branch is the one that guards tests/; sdist is the one with /.git/.
    wheel = next(e for e in exprs if "startswith" in e)
    sdist = next(e for e in exprs if "/.git/" in e)
    return wheel, sdist


def test_wheel_and_sdist_predicates_match_their_distribution_contracts():
    """Execute the workflow's actual leaked expressions against both formats.

    The two checks are deliberately NOT identical:

    * a wheel must carry no ``tests/`` at all, and no real scratch directory;
    * an sdist ships ``tests/`` on purpose, so a legitimate test whose *name*
      merely contains "scratch" (``tests/test_scratch_hygiene.py``) must pass,
      while a real ``scratch/`` directory and any ``/.git/`` path must still be
      blocked.

    The old bare-substring check failed exactly the sdist/legitimate-file case,
    which is what broke the v1.3.1 release build.
    """
    wheel_expr, sdist_expr = _extract_leaked_exprs()
    import re as _re

    # (member, blocked-by-wheel?, blocked-by-sdist?)
    MATRIX = [
        # legitimate module names containing the substring "scratch" -> allowed
        ("voyager/scratchpad.py", False, False),
        ("voyager/scratch_buffer.py", False, False),
        ("sessionflow_scratch/session.py", False, False),
        # a real scratch DIRECTORY -> blocked in both, wherever it sits
        ("scratch/leaked.py", True, True),
        ("build/scratch/tmp.py", True, True),
        ("a/b/scratch/c.py", True, True),
        # private VCS metadata -> sdist-only guard (wheels never carry it)
        ("voyager-1.3.1/.git/config", False, True),
        # tests/ is deliberately allowed in the sdist, forbidden in the wheel
        ("tests/test_scratch_hygiene.py", True, False),  # name has "scratch"
        ("tests/test_store.py", True, False),
    ]

    # Each expression is a full list comprehension over `names` / `members`.
    # Evaluate it against a one-element archive; a non-empty result means the
    # workflow would treat that member as leaked.
    for member, want_wheel, want_sdist in MATRIX:
        got_wheel = bool(eval(wheel_expr, {"re": _re, "names": [member]}))  # noqa: S307
        got_sdist = bool(eval(sdist_expr, {"re": _re, "members": [member]}))  # noqa: S307
        assert got_wheel == want_wheel, (
            f"wheel: {member!r} blocked={got_wheel}, expected {want_wheel}"
        )
        assert got_sdist == want_sdist, (
            f"sdist: {member!r} blocked={got_sdist}, expected {want_sdist}"
        )
