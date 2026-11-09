"""The consistency checker must fail on drift, not just pass on the happy path.

Each test builds a minimal repository tree and injects the tag list, so no git
repository or network is involved.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_consistency", ROOT / "scripts" / "check_consistency.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def make_repo(tmp_path, version="1.3.1", changelog="1.3.1",
              readme="1.3.0", readme_zh="1.3.0", notes=True,
              pin="1.3.0"):
    root = tmp_path / "repo"
    (root / "docs" / "releases").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nversion = "%s"\n' % version, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n(nothing yet)\n\n## [%s] — 2026-11-09\n" % changelog,
        encoding="utf-8")
    (root / "README.md").write_text(
        "| **Current release** | [v%s](https://github.com/o/r/releases/tag/v%s) |\n"
        'pipx install "voyager @ git+https://github.com/HarryHeYu/sessionFlow.git@v%s"\n'
        % (readme, readme, pin), encoding="utf-8")
    (root / "README.zh-CN.md").write_text(
        "| **当前版本** | [v%s](https://github.com/o/r/releases/tag/v%s) |\n"
        % (readme_zh, readme_zh), encoding="utf-8")
    if notes:
        (root / "docs" / "releases" / ("v%s.md" % version)).write_text(
            "# notes\n", encoding="utf-8")
    return root


def failures(result):
    return [l for l in result.lines if l.startswith("FAIL")]


def test_a_consistent_tree_passes(tmp_path):
    root = make_repo(tmp_path)
    r = cc.check(root, tags=["1.2.0", "1.3.0"])
    assert failures(r) == [], r.lines


def test_pyproject_ahead_of_changelog_fails(tmp_path):
    root = make_repo(tmp_path, version="1.3.1", changelog="1.3.0")
    r = cc.check(root, tags=["1.3.0"])
    assert any("CHANGELOG newest heading" in l for l in failures(r))


def test_missing_release_notes_fails(tmp_path):
    root = make_repo(tmp_path, notes=False)
    r = cc.check(root, tags=["1.3.0"])
    assert any("release notes" in l for l in failures(r))


def test_readme_lagging_behind_the_newest_tag_fails(tmp_path):
    """This is the drift that shipped: prose kept an old version."""
    root = make_repo(tmp_path, readme="1.2.0", readme_zh="1.2.0", pin="1.2.0")
    r = cc.check(root, tags=["1.2.0", "1.3.0"])
    assert any("README names the newest tag" in l for l in failures(r))


def test_readme_languages_disagreeing_fails(tmp_path):
    root = make_repo(tmp_path, readme="1.3.0", readme_zh="1.1.0")
    r = cc.check(root, tags=["1.3.0"])
    assert any("README en/zh agree" in l for l in failures(r))


def test_install_pin_to_an_unknown_tag_fails(tmp_path):
    root = make_repo(tmp_path, pin="9.9.9")
    r = cc.check(root, tags=["1.3.0"])
    assert any("install pins" in l for l in failures(r))


def test_without_git_the_tag_check_is_needs_online_not_a_pass(tmp_path):
    """Unknown must never read as OK."""
    root = make_repo(tmp_path)
    r = cc.check(root, tags=None)
    assert any(l.startswith("NEEDS_ONLINE") for l in r.lines)
    assert not any("names the newest tag" in l and l.startswith("OK")
                   for l in r.lines)


def test_bridge_schema_mismatch_fails(tmp_path):
    root = make_repo(tmp_path)
    plugin = tmp_path / "plugin"
    (plugin / "src").mkdir(parents=True)
    (plugin / "src" / "bridge.ts").write_text(
        "export const REQUIRED_SCHEMA_VERSION = 2;\n", encoding="utf-8")
    r = cc.check(root, plugin_root=plugin, tags=["1.3.0"])
    # In a synthetic tree the probe cannot run, which is itself a failure;
    # either message is acceptable — the point is that it does not pass.
    assert any("schema" in l.lower() for l in failures(r))
