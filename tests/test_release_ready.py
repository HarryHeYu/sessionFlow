"""The pre-tag gate must fail on every way a tag tree can mislead a user.

No git repository, no network and no real tag are involved: the tag helpers are
monkeypatched, exactly as ``test_consistency.py`` injects its tag list.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_release_ready", ROOT / "scripts" / "check_release_ready.py")
rr = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(rr)

VERSION = "1.4.0"


def make_tree(tmp_path, *, version=VERSION, pin=VERSION, extra_pin=None,
              changelog=VERSION, current=VERSION, notes=True):
    root = tmp_path / "core"
    (root / "docs" / "releases").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nversion = "%s"\n' % version, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "## [%s] — 2026-11-13\n\n## [1.3.1] — 2026-11-11\n" % changelog,
        encoding="utf-8")
    for name, label in (("README.md", "**Current release**"),
                        ("README.zh-CN.md", "**当前版本**")):
        pins = ['pipx install "voyager @ git+https://github.com/HarryHeYu/'
                'sessionFlow.git@v%s"' % pin]
        if extra_pin:
            pins.append('pip install "voyager @ git+https://github.com/'
                        'HarryHeYu/sessionFlow.git@v%s"' % extra_pin)
        (root / name).write_text(
            "| %s | [v%s](https://github.com/o/r/releases/tag/v%s) |\n%s\n"
            % (label, current, current, "\n".join(pins)), encoding="utf-8")
    if notes:
        (root / "docs" / "releases" / ("v%s.md" % version)).write_text(
            "# notes\n", encoding="utf-8")
    plugin = tmp_path / "plugin"
    (plugin / "src").mkdir(parents=True)
    (plugin / "src" / "bridge.ts").write_text(
        "export const REQUIRED_SCHEMA_VERSION = 1;\n", encoding="utf-8")
    return root, plugin


def prepare(monkeypatch, *, local=None, remote="absent"):
    monkeypatch.setattr(rr.cc, "git_tags",
                        lambda root: ["1.2.0", "1.3.0", "1.3.1"] if local is None else local)
    monkeypatch.setattr(rr.cc, "_remote_tag_state", lambda root, v: remote)
    # `check_consistency.check()` probes the tree for the bridge schema; a
    # synthetic tree has no runnable CLI, so answer the same way CI would.
    monkeypatch.setattr(rr.cc, "_integration_info_schema", lambda root: 1)


def failures(gate):
    return [line for line in gate.lines if line.startswith("FAIL")]


def test_a_coherent_tree_passes(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path)
    prepare(monkeypatch)
    gate = rr.run(root, VERSION, plugin_root=plugin)
    assert failures(gate) == [], gate.lines
    assert gate.report() == 0


def test_the_default_strict_check_still_requires_a_real_tag(tmp_path, monkeypatch):
    """The gate must not have weakened CI: without the pending tag it is red."""
    root, plugin = make_tree(tmp_path)
    prepare(monkeypatch)
    r = rr.cc.check(root, plugin_root=plugin,
                    tags=["1.2.0", "1.3.0", "1.3.1"], online=False,
                    require_plugin=True)
    assert r.exit_code(strict=True) == 1
    assert any("install pins" in line for line in r.lines if line.startswith("FAIL"))


def test_stale_pin_fails(tmp_path, monkeypatch):
    """The exact bug this gate exists for: the tag tree says install 1.3.1."""
    root, plugin = make_tree(tmp_path, pin="1.3.1")
    prepare(monkeypatch)
    assert any("install pins" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))


def test_mixed_pins_fail(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path, extra_pin="1.3.1")
    prepare(monkeypatch)
    assert any("install pins" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))


def test_current_release_row_lagging_fails(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path, current="1.3.1")
    prepare(monkeypatch)
    assert any("current release" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))


def test_pyproject_mismatch_fails(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path, version="1.3.1")
    prepare(monkeypatch)
    assert any("pyproject version" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))


def test_changelog_mismatch_fails(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path, changelog="1.3.1")
    prepare(monkeypatch)
    assert any("CHANGELOG" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))


def test_missing_release_notes_fails(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path, notes=False)
    prepare(monkeypatch)
    assert any("release notes" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))


def test_existing_local_tag_fails(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path)
    prepare(monkeypatch, local=["1.3.1", VERSION])
    assert any("local tag" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))


def test_existing_remote_tag_fails(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path)
    prepare(monkeypatch, remote="present")
    assert any("remote tag" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))


def test_unknown_remote_state_fails_closed(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path)
    prepare(monkeypatch, remote="unknown")
    assert any("ls-remote failed" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))


def test_unreadable_local_tags_fail_closed(tmp_path, monkeypatch):
    root, plugin = make_tree(tmp_path)
    prepare(monkeypatch, local=None)
    monkeypatch.setattr(rr.cc, "git_tags", lambda root: None)
    assert any("local tags readable" in line for line in failures(rr.run(root, VERSION, plugin_root=plugin)))
