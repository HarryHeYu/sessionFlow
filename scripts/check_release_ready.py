#!/usr/bin/env python3
"""Pre-tag release gate: verify the tree a NEW tag will point at.

Why this exists
---------------

``check_consistency.py`` requires every install pin and the README "current
release" row to name a tag that already exists. That is correct for CI, but it
leaves a hole at the exact moment a version is cut: if the tag is created from
a tree whose README still says ``@v1.3.1``, the published source tells users to
install the previous version. Nothing in ``release.yml`` catches that — it only
compares ``pyproject`` to the tag, and a tag cannot be moved afterwards.

This gate answers one question without weakening the default check:

    "If v<version> existed right now, would this tree be a valid release?"

It does that by injecting the pending version into the tag list passed to
``check_consistency.check()`` — the same injection the checker's own unit tests
use — and then adding the assertions the default check cannot make, because it
has no notion of a *pending* release. ``check_consistency.py`` itself is NOT
modified; CI still runs the strict, real-tag-only form.

What this tool never does
-------------------------

* It never creates, moves or pushes a tag.
* It never creates or publishes a Release, and it never builds an artefact.
* It refuses to run when the tag already exists locally or on origin, and when
  the remote state cannot be determined (unknown is not "absent").
* It never relaxes ``check_consistency.py``: a pre-tag run of *this* tool is
  not a substitute for the post-tag strict check in CI.

Exit code 0 means the tree is ready to be tagged, NOT that the version is
released.

Usage:
    python scripts/check_release_ready.py --version 1.4.0 \
        [--repo .] [--plugin-root ../dsh-sessionflow]
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import List, Optional, Sequence

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]

_spec = importlib.util.spec_from_file_location(
    "check_consistency", ROOT / "scripts" / "check_consistency.py")
cc = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(cc)


class Gate:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.failures = 0

    def ok(self, what: str, detail: str = "") -> None:
        self.lines.append("OK   %-46s %s" % (what, detail))

    def fail(self, what: str, detail: str) -> None:
        self.failures += 1
        self.lines.append("FAIL %-46s %s" % (what, detail))

    def report(self) -> int:
        print("\n".join(self.lines))
        if self.failures:
            print("\n%d pre-tag check(s) failed — DO NOT TAG" % self.failures)
            return 1
        print("\npre-tag gate passed — this did NOT create or publish anything")
        return 0


def all_install_pins(root: Path, checker=cc) -> dict:
    """Every pinned version in both READMEs, by filename (never a sample)."""
    out = {}
    for name in ("README.md", "README.zh-CN.md"):
        out[name] = set(checker.INSTALL_PIN.findall(checker._read(root / name)))
    return out


def run(root: Path, version: str, plugin_root: Optional[Path] = None,
        checker=cc, gate: Optional[Gate] = None) -> Gate:
    """Run every pre-tag assertion. ``checker`` is injectable for unit tests."""
    g = gate or Gate()

    # --- 1. the version we are about to tag is the version being built ------
    actual = checker.pyproject_version(root)
    if actual == version:
        g.ok("pyproject version", version)
    else:
        g.fail("pyproject version", "%r != requested %r" % (actual, version))

    headings = checker.changelog_versions(root)
    if headings and headings[0] == version:
        g.ok("CHANGELOG newest heading", version)
    else:
        g.fail("CHANGELOG newest heading",
               "%r != %r" % (headings[0] if headings else None, version))

    notes = root / "docs" / "releases" / ("v%s.md" % version)
    if notes.is_file():
        g.ok("release notes for v%s" % version, str(notes.name))
    else:
        g.fail("release notes for v%s" % version, "missing %s" % notes.name)

    # --- 2. the published source must tell users to install THIS version ---
    for name, pattern in (("README.md", cc.README_EN_CURRENT),
                          ("README.zh-CN.md", cc.README_ZH_CURRENT)):
        m = pattern.search(cc._read(root / name))
        if m and m.group(1) == version:
            g.ok("%s current release" % name, "v%s" % version)
        else:
            g.fail("%s current release" % name,
                   "%r != v%s" % (m.group(1) if m else None, version))

    pins = all_install_pins(root, checker)
    for name, found in pins.items():
        if found == {version}:
            g.ok("install pins in %s" % name, "v%s" % version)
        else:
            g.fail("install pins in %s" % name,
                   "expected only v%s, got %s" % (version, sorted(found) or "none"))

    # --- 3. the tag must not exist; unknown is a hard failure ---------------
    local = checker.git_tags(root)
    if local is None:
        g.fail("local tags readable", "git tag --list failed")
    elif version in local:
        g.fail("local tag v%s is unused" % version, "already exists locally")
    else:
        g.ok("local tag v%s is unused" % version, "not present")

    state = checker._remote_tag_state(root, version)
    if state == "absent":
        g.ok("remote tag v%s is unused" % version, "not on origin")
    elif state == "present":
        g.fail("remote tag v%s is unused" % version, "already on origin")
    else:
        g.fail("remote tag v%s is unused" % version,
               "ls-remote failed; unknown is not 'absent'")

    # --- 4. simulate the post-tag consistency check, without changing CI ---
    simulated = sorted(set(local or []) | {version})
    r = checker.check(root, plugin_root=plugin_root, tags=simulated,
                      online=False, require_plugin=plugin_root is not None)
    hard = [line for line in r.lines if line.startswith("FAIL")]
    if hard or r.exit_code(strict=True) != 0:
        g.fail("post-tag consistency (simulated)",
               "; ".join(hard) or "unresolved critical check")
    else:
        g.ok("post-tag consistency (simulated)",
             "%d checks with v%s treated as present" % (len(r.lines), version))

    return g


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True,
                    help="the version about to be tagged, e.g. 1.4.0")
    ap.add_argument("--repo", default=".", help="core repository root")
    ap.add_argument("--plugin-root", default=None,
                    help="plugin checkout, to verify the bridge schema")
    args = ap.parse_args(argv)

    root = Path(args.repo).resolve()
    plugin = Path(args.plugin_root).resolve() if args.plugin_root else None
    print("PRE-TAG GATE for v%s — simulation only, no tag/Release is created\n"
          % args.version)
    return run(root, args.version, plugin_root=plugin).report()


if __name__ == "__main__":
    raise SystemExit(main())
