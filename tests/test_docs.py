"""Documentation and semantics guards.

Fast, deterministic, no network and no model.  Four things are checked:

1. every relative markdown link resolves;
2. ``docs/INDEX.md`` lists every document, and every link in it resolves;
3. the vocabulary and the lifecycle states named in the glossary still exist in
   the code, so a rename cannot land in one place only;
4. no install instruction that would fetch the *wrong* project from PyPI has
   been reintroduced into a code block.

Deliberately **not** checked: prose wording, sentence style, or whether a
document "sounds" consistent.  A linter that guesses at natural language reports
false positives, and a check people learn to ignore is worse than no check.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Directories that are not this repository's documentation.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".workbuddy-ai", "exports",
             "dist", "build", ".pytest_cache", ".venv", "dsh-sessionflow"}

#: Link targets that are not files in this repository.
REMOTE = ("http://", "https://", "mailto:", "#")

#: ``[text](target)`` and ``[text](<target>)``, with an optional title.
LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")

FENCE = re.compile(r"^```")


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def markdown_files() -> list[Path]:
    """Every tracked-looking markdown file in the repository."""
    out = []
    for p in ROOT.rglob("*.md"):
        if _is_skipped(p.relative_to(ROOT)):
            continue
        out.append(p)
    return sorted(out)


def repository_docs() -> list[Path]:
    """Markdown that belongs in the index: the root files and ``docs/*.md``.

    Deliberately one level deep.  ``docs/releases/`` and ``docs/archive/`` are
    listed by directory in the index rather than file by file, because they are
    histories: enumerating every release note there would make adding one a
    two-file change for no reader benefit.
    """
    out = []
    for p in markdown_files():
        rel = p.relative_to(ROOT)
        if len(rel.parts) == 1 or (rel.parts[0] == "docs" and len(rel.parts) == 2):
            out.append(p)
    return sorted(out)


def links_in(path: Path) -> list[str]:
    return LINK.findall(path.read_text(encoding="utf-8", errors="ignore"))


class TestLinks(unittest.TestCase):
    """A broken relative link is a defect in the document, not a style choice."""

    def test_every_relative_link_resolves(self):
        broken = []
        for md in markdown_files():
            for target in links_in(md):
                if target.startswith(REMOTE):
                    continue
                target = target.split("#", 1)[0]          # drop any fragment
                if not target:
                    continue
                resolved = (md.parent / target).resolve()
                if not resolved.exists():
                    broken.append(
                        "%s -> %s" % (md.relative_to(ROOT), target))
        self.assertEqual(
            broken, [],
            "these relative links do not resolve:\n  " + "\n  ".join(broken))

    def test_no_markdown_uses_a_backslash_path(self):
        """Windows separators in a link work locally and break on GitHub."""
        bad = []
        for md in markdown_files():
            for target in links_in(md):
                if target.startswith(REMOTE):
                    continue
                if "\\" in target:
                    bad.append("%s -> %s" % (md.relative_to(ROOT), target))
        self.assertEqual(bad, [], "use forward slashes in links:\n  " + "\n  ".join(bad))


class TestIndex(unittest.TestCase):
    """The index is only useful if it is complete."""

    INDEX = ROOT / "docs" / "INDEX.md"

    def test_index_exists(self):
        self.assertTrue(self.INDEX.exists(), "docs/INDEX.md is missing")

    def test_index_lists_every_document(self):
        text = self.INDEX.read_text(encoding="utf-8")
        missing = []
        for md in repository_docs():
            if md == self.INDEX:
                continue
            if md.name not in text:
                missing.append(str(md.relative_to(ROOT)))
        self.assertEqual(
            missing, [],
            "these documents are not listed in docs/INDEX.md:\n  " + "\n  ".join(missing))

    def test_index_links_resolve(self):
        broken = []
        for target in links_in(self.INDEX):
            if target.startswith(REMOTE):
                continue
            target = target.split("#", 1)[0]
            if target and not (self.INDEX.parent / target).resolve().exists():
                broken.append(target)
        self.assertEqual(broken, [], "docs/INDEX.md links that do not resolve: %s" % broken)

    def test_no_new_phase_reports_at_the_top_level(self):
        """Round reports belong in docs/releases/ or docs/archive/, never in docs/.

        The root and docs/ accumulated sixteen of them before P7 and P8 moved
        them; this is the guard that stops the pile rebuilding.
        """
        pattern = re.compile(r"(SUMMARY|STATUS|PROGRESS|COMPLETION|FINAL_REPORT|"
                             r"VERIFICATION_STATE|CHECKPOINT)", re.IGNORECASE)
        offenders = []
        for md in repository_docs():
            rel = md.relative_to(ROOT)
            if rel.parts[0] == "archive":
                continue
            if pattern.search(md.name):
                offenders.append(str(rel))
        self.assertEqual(
            offenders, [],
            "phase reports belong in docs/releases/ or docs/archive/: %s" % offenders)


class TestVocabulary(unittest.TestCase):
    """The glossary names things; the code has to still use those names."""

    GLOSSARY = ROOT / "docs" / "GLOSSARY.md"
    CONCEPT_MODEL = ROOT / "docs" / "CONCEPT_MODEL.md"

    #: Every term the glossary is required to define.
    REQUIRED_TERMS = [
        "Agent / Provider", "Session", "Native Session ID", "Source", "Event",
        "WorkThread", "Context", "Continuation Bundle", "Handoff", "Switch",
        "Merge", "Native Resume", "Source Missing", "Archived Canonical",
        "Provenance", "Lease", "Checkpoint",
    ]

    def test_reference_documents_exist(self):
        for p in (self.GLOSSARY, self.CONCEPT_MODEL):
            self.assertTrue(p.exists(), "%s is missing" % p.name)

    def test_every_required_term_is_defined(self):
        text = self.GLOSSARY.read_text(encoding="utf-8")
        missing = [t for t in self.REQUIRED_TERMS if t not in text]
        self.assertEqual(missing, [], "docs/GLOSSARY.md does not define: %s" % missing)

    def test_the_four_confusable_distinctions_are_stated(self):
        text = self.GLOSSARY.read_text(encoding="utf-8")
        for left, right in (("Context Handoff", "Native Resume"),
                            ("Source Missing", "Deleted Session"),
                            ("Assistant Conclusion", "Verified Fact"),
                            ("WorkThread", "Single Session")):
            self.assertTrue(left in text,
                            "the %s distinction is not stated" % left)
            self.assertTrue(right in text,
                            "the %s / %s distinction is not stated" % (left, right))

    def test_lifecycle_states_match_the_schema(self):
        """The three canonical states, as the schema comment defines them."""
        store = (ROOT / "voyager" / "store.py").read_text(encoding="utf-8")
        for state in ("ACTIVE_SOURCE", "SOURCE_MISSING", "ARCHIVED_CANONICAL"):
            self.assertTrue(state in store,
                            "%s is named in the docs but not in the schema" % state)
        for doc in (self.GLOSSARY, self.CONCEPT_MODEL):
            text = doc.read_text(encoding="utf-8")
            for state in ("ACTIVE_SOURCE", "SOURCE_MISSING", "ARCHIVED_CANONICAL"):
                self.assertTrue(state in text,
                                "%s does not mention the %s state" % (doc.name, state))

    def test_thread_resolution_statuses_match_the_code(self):
        auto = (ROOT / "voyager" / "auto.py").read_text(encoding="utf-8")
        for status in ('"no_thread"', '"success"', '"ambiguous"'):
            self.assertTrue(status in auto,
                            "%s is no longer a resolver status" % status)
        text = self.GLOSSARY.read_text(encoding="utf-8")
        self.assertTrue("ambiguous" in text,
                        "the glossary must warn that 'ambiguous' is not 'no_thread'")

    def test_public_error_codes_still_exist(self):
        """The core-side codes the docs and the plugin rely on.

        The plugin's own codes (``VOYAGER_NOT_FOUND``, ``INCOMPATIBLE``,
        ``TIMEOUT``, ``BAD_JSON``, ``CLI_FAILED``) live in the dsh-sessionflow
        repository and are not this suite's to check.
        """
        package = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in (ROOT / "voyager").rglob("*.py"))
        for code in ("thread_not_found", "thread_out_of_scope", "not_found",
                     "SOURCE_MISSING", "ARCHIVED_CANONICAL"):
            self.assertTrue(code in package,
                            "public error code or state %s disappeared" % code)

    def test_capability_levels_match_the_matrix(self):
        matrix = (ROOT / "voyager" / "capability_matrix.py").read_text(encoding="utf-8")
        for level in ("NOT_FOUND_IN_CURRENT_AUDIT", "SUPPORTED", "CONFIGURED",
                      "UNIT_VERIFIED", "LIVE_VERIFIED", "ZERO_TOUCH_LIVE_VERIFIED"):
            self.assertTrue(level in matrix,
                            "%s is not in the capability matrix" % level)
            self.assertTrue(level in self.CONCEPT_MODEL.read_text(encoding="utf-8"),
                            "%s is not in docs/CONCEPT_MODEL.md" % level)


class TestInstallInstructions(unittest.TestCase):
    """PyPI has an unrelated package called ``voyager``.

    Installing it by bare name gives the wrong project, so the only correct
    instruction names this repository.  Prose may *warn* about the bare form —
    the checks below look only inside fenced code blocks, where a command is
    meant to be run rather than read.
    """

    BARE = re.compile(r"^\s*(?:pip|pipx|python -m pip)\s+install\s+"
                      r"[\"']?voyager(?!\s*\[?[a-z,]*\]?\s*@|\s*@)")

    def _code_blocks(self, path: Path):
        inside, buffer, opened = False, [], 0
        for i, line in enumerate(path.read_text(encoding="utf-8",
                                                errors="ignore").splitlines(), 1):
            if FENCE.match(line):
                if not inside:
                    opened = i
                inside = not inside
                continue
            if inside:
                buffer.append((i, line))
        return buffer

    def test_no_bare_pypi_install_in_a_code_block(self):
        """Maintained documents only.

        A frozen release note records what was true on the day it shipped, and
        editing one to satisfy a linter would be rewriting history.  The check
        exists to stop a *current* install instruction from sending someone to
        the wrong project.
        """
        offenders = []
        for md in repository_docs():
            for lineno, line in self._code_blocks(md):
                if self.BARE.match(line):
                    offenders.append("%s:%d  %s" % (md.relative_to(ROOT), lineno,
                                                    line.strip()))
        self.assertEqual(
            offenders, [],
            "a bare `pip install voyager` fetches an unrelated PyPI project; "
            "use `voyager @ git+https://github.com/HarryHeYu/sessionFlow.git`:\n  "
            + "\n  ".join(offenders))

    def test_the_install_hint_points_at_the_repository(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertTrue("github.com/HarryHeYu/sessionFlow" in readme,
                        "README no longer points at the repository for install")
        quickstart = (ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
        self.assertTrue("github.com/HarryHeYu/sessionFlow" in quickstart,
                        "QUICKSTART no longer points at the repository for install")


if __name__ == "__main__":
    unittest.main()
