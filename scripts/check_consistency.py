#!/usr/bin/env python3
"""Check that release metadata tells one story.

The v1.1.0 → v1.2.0 drift happened because the same fact (which version is
current) lived in prose in four places and nothing compared them.  This script
compares them and fails CI when they disagree.

What is authoritative for what:

* ``pyproject.toml`` — the version being built. May be ahead of the last tag
  while a release is being prepared.
* ``CHANGELOG.md`` — the newest heading must equal that version.
* git tags — the versions actually released. README must name the newest one.
* ``docs/releases/v<version>.md`` — must exist for the version being built, because
  ``release.yml`` reads that file for the tag it publishes.
* ``voyager integration-info --json`` — the bridge schema, the only runtime
  authority the plugin negotiates against.

Historical documents are not checked: ``docs/archive/`` and older release notes
are records of a moment and are allowed to name old versions.

Usage:
    python scripts/check_consistency.py [--repo .] [--plugin-root ../dsh-sessionflow]
                                        [--online] [--strict]

``--strict`` is for CI and release gates.  A ``NEEDS_ONLINE`` result on a
*version* check means the release metadata was not actually verified; in strict
mode that is a failure, not an OK with a footnote.  Without ``--strict`` an
unknown is still printed but does not block a local developer run.

``--online`` really queries ``git ls-remote --tags origin v<version>``: an
already-published tag is a hard failure (tags are never moved), a missing tag
is an OK, and a failed query is an unknown.

Exit code 0 when every check passes, 1 when any check fails or (with
``--strict``) any check is unresolved.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

CHANGELOG_HEADING = re.compile(
    r"^## \[(\d+\.\d+\.\d+)\](?:\s+—\s+(\d{4}-\d{2}-\d{2}))?", re.MULTILINE)
PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
README_EN_CURRENT = re.compile(
    r"\|\s*\*\*Current release\*\*\s*\|[^|]*?/releases/tag/v([0-9]+\.[0-9]+\.[0-9]+)")
README_ZH_CURRENT = re.compile(
    r"\|\s*\*\*当前版本\*\*\s*\|[^|]*?/releases/tag/v([0-9]+\.[0-9]+\.[0-9]+)")
INSTALL_PIN = re.compile(
    r"sessionFlow\.git@v([0-9]+\.[0-9]+\.[0-9]+)")
PLUGIN_SCHEMA = re.compile(r"REQUIRED_SCHEMA_VERSION\s*=\s*(\d+)")


class Result:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.failures = 0
        self.unresolved: List[str] = []

    def ok(self, what: str, detail: str = "") -> None:
        self.lines.append("OK   %-46s %s" % (what, detail))

    def fail(self, what: str, detail: str) -> None:
        self.failures += 1
        self.lines.append("FAIL %-46s %s" % (what, detail))

    def skip(self, what: str, detail: str = "") -> None:
        """Visible, but not a pass and not a failure: nothing was checked."""
        self.lines.append("SKIP %-46s %s" % (what, detail))

    def needs_online(self, what: str, detail: str,
                     critical: bool = True) -> None:
        """Unknown. Critical unknowns are version facts a release gate needs.

        ``critical=False`` is for informational gaps that cannot be resolved
        locally and must not gate a release (there are none today).
        """
        if critical:
            self.unresolved.append(what)
        self.lines.append("NEEDS_ONLINE %-37s %s" % (what, detail))

    def exit_code(self, strict: bool = False) -> int:
        if self.failures:
            return 1
        if strict and self.unresolved:
            return 1
        return 0


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def pyproject_version(root: Path) -> Optional[str]:
    m = PYPROJECT_VERSION.search(_read(root / "pyproject.toml"))
    return m.group(1) if m else None


def changelog_versions(root: Path) -> List[str]:
    return CHANGELOG_HEADING.findall(_read(root / "CHANGELOG.md")) and [
        v for v, _ in CHANGELOG_HEADING.findall(_read(root / "CHANGELOG.md"))] or []


def git_tags(root: Path) -> Optional[List[str]]:
    """Local tags, or None when git is unavailable (then we cannot know)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "tag", "--list", "v*"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [t[1:] for t in out.stdout.split() if t.startswith("v")]


def _remote_tag_state(root: Path, version: str) -> str:
    """'present' / 'absent' / 'unknown' for exactly ``refs/tags/v<version>``.

    Asking for one exact ref (not a pattern) is important: ``git ls-remote
    --tags origin v1.3.1`` also matches ``v1.3.10`` or ``v1.3.1-rc1`` by
    substring, which would make a free tag look occupied.
    """
    ref = "refs/tags/v%s" % version
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-remote", "--tags", "origin", ref],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if out.returncode != 0:
        return "unknown"
    for line in out.stdout.splitlines():
        # "<sha>\t<ref>" — the peeled annotated tag ends in "^{}".
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].removesuffix("^{}") == ref:
            return "present"
    return "absent"


def _newest(versions: Iterable[str]) -> Optional[str]:
    def key(v: str):
        return tuple(int(x) for x in v.split("."))
    vs = [v for v in versions if re.fullmatch(r"\d+\.\d+\.\d+", v)]
    return max(vs, key=key) if vs else None


def check(root: Path, plugin_root: Optional[Path] = None,
          tags: Optional[Sequence[str]] = None,
          online: bool = False,
          remote_state: Optional[str] = None,
          require_plugin: bool = False) -> Result:
    """Build a Result; ``remote_state`` injects the ls-remote answer for tests.

    ``strict`` is deliberately *not* here: a Result describes what was found,
    and :meth:`Result.exit_code` (or :func:`main`) decides whether an unresolved
    critical check is allowed. Keeping the two apart makes it impossible for a
    caller to accidentally get a pass by forgetting the flag at collection time.
    """
    r = Result()

    version = pyproject_version(root)
    if not version:
        r.fail("pyproject version", "not found")
        return r
    r.ok("pyproject version", version)

    # CHANGELOG: the newest heading is the version being built.
    cl = changelog_versions(root)
    if not cl:
        r.fail("CHANGELOG has no released version heading", "")
    elif cl[0] != version:
        r.fail("CHANGELOG newest heading", "%s != pyproject %s" % (cl[0], version))
    else:
        r.ok("CHANGELOG newest heading", cl[0])

    # Release notes for the version being built: release.yml reads them.
    notes = root / "docs" / "releases" / ("v%s.md" % version)
    if notes.exists():
        r.ok("release notes for %s" % version, str(notes.relative_to(root)))
    else:
        r.fail("release notes for %s" % version, "missing %s" % notes.name)

    # README: the current release must be a tag that exists, and the newest one.
    known = list(tags) if tags is not None else git_tags(root)
    if known is not None and not known:
        # A shallow clone returns an empty list, not an error. Treating that as
        # "no release exists" turned a CI checkout into three false failures
        # (run 42594999041). Unknown must stay unknown.
        known = None
    en = README_EN_CURRENT.search(_read(root / "README.md"))
    zh = README_ZH_CURRENT.search(_read(root / "README.zh-CN.md"))
    if not en:
        r.fail("README current release", "row not found")
    if not zh:
        r.fail("README.zh-CN current version", "row not found")
    if en and zh and en.group(1) != zh.group(1):
        r.fail("README en/zh agree", "%s != %s" % (en.group(1), zh.group(1)))
    elif en and zh:
        r.ok("README en/zh agree", en.group(1))

    if known is None:
        r.needs_online("README current release vs tags", "git unavailable")
    elif en:
        newest = _newest(known)
        if en.group(1) not in known:
            r.fail("README current release is a real tag",
                   "v%s not in %s" % (en.group(1), sorted(known)))
        elif newest and en.group(1) != newest:
            r.fail("README names the newest tag",
                   "README v%s, newest tag v%s" % (en.group(1), newest))
        else:
            r.ok("README names the newest tag", "v%s" % en.group(1))

    # Install commands must pin a released version, not a stale one.
    for name in ("README.md", "README.zh-CN.md"):
        pins = set(INSTALL_PIN.findall(_read(root / name)))
        if known is None:
            r.needs_online("install pins in %s" % name, "git unavailable")
            continue
        bad = sorted(p for p in pins if p not in known)
        if bad:
            r.fail("install pins in %s are released" % name, "unknown: %s" % bad)
        elif pins:
            r.ok("install pins in %s are released" % name, ",".join(sorted(pins)))

    # Bridge schema: runtime authority vs what the plugin demands.
    if plugin_root is not None:
        bridge = plugin_root / "src" / "bridge.ts"
        m = PLUGIN_SCHEMA.search(_read(bridge)) if bridge.exists() else None
        if not m:
            r.fail("plugin REQUIRED_SCHEMA_VERSION", "not found in %s" % bridge)
        else:
            required = int(m.group(1))
            core_schema = _integration_info_schema(root)
            if core_schema is None:
                r.fail("core integration-info schema_version", "probe failed")
            elif core_schema != required:
                r.fail("bridge schema agreement",
                       "core %s != plugin %s" % (core_schema, required))
            else:
                r.ok("bridge schema agreement", str(required))
    elif require_plugin:
        # A release gate must not silently skip the cross-repo contract just
        # because the plugin checkout was not passed to this one invocation.
        r.fail("plugin checkout provided", "--plugin-root is required here")
    else:
        r.skip("bridge schema agreement", "--plugin-root not provided")

    if online:
        state = remote_state if remote_state is not None else _remote_tag_state(root, version)
        if state == "present":
            # Tags are never moved. If v<version> already exists on the remote,
            # either the release was published or an old tag is in the way.
            r.fail("release tag v%s is unused" % version,
                   "already exists on origin")
        elif state == "absent":
            r.ok("release tag v%s is unused" % version, "not present on origin")
        else:
            r.needs_online("remote tag v%s" % version,
                           "ls-remote failed; run `git ls-remote --tags origin v%s` by hand" % version)

    return r


def _integration_info_schema(root: Path) -> Optional[int]:
    """Ask the tree itself, not a literal, so the answer cannot be copied wrong."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "voyager.cli", "integration-info", "--json"],
            capture_output=True, text=True, timeout=120, cwd=str(root),
            env={**_clean_env(), "PYTHONPATH": str(root)},
        )
        if out.returncode != 0:
            return None
        return int(json.loads(out.stdout)["schema_version"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def _clean_env() -> dict:
    import os
    env = os.environ.copy()
    # The probe must not read a real index; it only prints metadata.
    env["VOYAGER_NO_SYNC"] = "1"
    return env


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="core repository root")
    ap.add_argument("--plugin-root", default=None,
                    help="plugin checkout, to compare the bridge schema")
    ap.add_argument("--online", action="store_true",
                    help="query the remote for whether v<version> is already tagged")
    ap.add_argument("--strict", action="store_true",
                    help="CI/release mode: an unresolved critical check fails "
                         "instead of printing NEEDS_ONLINE and exiting 0")
    ap.add_argument("--require-plugin", action="store_true",
                    help="fail when --plugin-root was not given (release gates "
                         "must prove the bridge schema, not skip it)")
    args = ap.parse_args(argv)

    root = Path(args.repo).resolve()
    plugin = Path(args.plugin_root).resolve() if args.plugin_root else None
    result = check(root, plugin_root=plugin, online=args.online,
                   require_plugin=args.require_plugin)
    print("\n".join(result.lines))
    rc = result.exit_code(args.strict)
    if result.failures:
        print("\n%d check(s) failed" % result.failures)
    if args.strict and result.unresolved:
        print("%d critical check(s) unresolved — strict mode treats "
              "NEEDS_ONLINE as failure" % len(result.unresolved))
    if rc:
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
