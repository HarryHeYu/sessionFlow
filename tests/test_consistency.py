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


def test_an_empty_tag_list_is_unknown_not_no_releases(tmp_path):
    """A shallow clone has no tags; that must not read as "nothing was ever released".
    """
    root = make_repo(tmp_path)
    r = cc.check(root, tags=[])
    assert any(l.startswith("NEEDS_ONLINE") for l in r.lines), r.lines
    assert not any(l.startswith("FAIL") and "tag" in l for l in r.lines), r.lines


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


# --- --strict: an unresolved critical check is not a success --------------

def test_strict_fails_when_a_critical_check_is_unresolved(tmp_path, capsys,
                                                          monkeypatch):
    """The release gate must not green-light metadata it could not verify."""
    root = make_repo(tmp_path)
    monkeypatch.setattr(cc, "git_tags", lambda _root: None)
    assert cc.main(["--repo", str(root), "--strict"]) == 1
    out = capsys.readouterr().out
    assert "NEEDS_ONLINE" in out
    assert "critical check(s) unresolved" in out


def test_non_strict_still_allows_a_local_run_with_unknowns(tmp_path, monkeypatch):
    """A developer with no tags fetched still gets useful output, not a red run."""
    root = make_repo(tmp_path)
    monkeypatch.setattr(cc, "git_tags", lambda _root: None)
    # The local default is lenient, but only outside CI. Clear the runner's
    # variables so this test measures the default rather than the environment.
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert cc.main(["--repo", str(root)]) == 0


def test_strict_is_the_default_inside_ci(tmp_path, monkeypatch):
    """A CI job that cannot verify the metadata must not report success.

    The default lives in the script, not in the workflow's command line, so the
    gate cannot be lost by editing one YAML line.
    """
    root = make_repo(tmp_path)
    monkeypatch.setattr(cc, "git_tags", lambda _root: None)
    monkeypatch.setenv("CI", "true")
    assert cc.main(["--repo", str(root)]) == 1


def test_no_strict_forces_leniency_even_inside_ci(tmp_path, monkeypatch):
    """The escape hatch exists for a CI leg that deliberately cannot see tags."""
    root = make_repo(tmp_path)
    monkeypatch.setattr(cc, "git_tags", lambda _root: None)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert cc.main(["--repo", str(root), "--no-strict"]) == 0


def test_strict_passes_when_every_check_resolves(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setattr(cc, "git_tags", lambda _root: ["1.2.0", "1.3.0"])
    assert cc.main(["--repo", str(root), "--strict"]) == 0


def test_strict_fails_on_a_hard_failure_even_without_unknowns(tmp_path, monkeypatch):
    """Strict adds a rule; it must not mask the existing FAIL path."""
    root = make_repo(tmp_path, readme="1.2.0", readme_zh="1.2.0", pin="1.2.0")
    monkeypatch.setattr(cc, "git_tags", lambda _root: ["1.2.0", "1.3.0"])
    assert cc.main(["--repo", str(root), "--strict"]) == 1


def test_result_exit_code_is_strict_aware():
    r = cc.Result()
    r.ok("something")
    assert r.exit_code(strict=False) == 0
    assert r.exit_code(strict=True) == 0
    r.needs_online("a critical version fact", "cannot tell")
    assert r.exit_code(strict=False) == 0
    assert r.exit_code(strict=True) == 1
    r.fail("a real drift", "nope")
    assert r.exit_code(strict=False) == 1


def test_noncritical_unknowns_do_not_gate_a_release():
    r = cc.Result()
    r.needs_online("informational", "no remote", critical=False)
    assert r.exit_code(strict=True) == 0


# --- --online: really ask the remote, and never move a tag -----------------

def test_online_fails_when_the_release_tag_already_exists(tmp_path):
    root = make_repo(tmp_path)
    r = cc.check(root, tags=["1.3.0"], online=True, remote_state="present")
    assert any("release tag v1.3.1 is unused" in l for l in failures(r))


def test_online_passes_when_the_release_tag_is_free(tmp_path):
    root = make_repo(tmp_path)
    r = cc.check(root, tags=["1.3.0"], online=True, remote_state="absent")
    assert failures(r) == [], r.lines
    assert any("release tag v1.3.1 is unused" in l and l.startswith("OK")
               for l in r.lines)


def test_online_probe_failure_is_a_critical_unknown(tmp_path):
    root = make_repo(tmp_path)
    r = cc.check(root, tags=["1.3.0"], online=True, remote_state="unknown")
    assert any(l.startswith("NEEDS_ONLINE") and "remote tag" in l for l in r.lines)
    assert r.exit_code(strict=True) == 1
    assert r.exit_code(strict=False) == 0


def test_exact_ref_does_not_match_a_longer_version(tmp_path, monkeypatch):
    """v1.3.10 / v1.3.1-rc1 must not read as 'v1.3.1 is taken'."""
    root = make_repo(tmp_path)

    class FakeOut:
        returncode = 0
        stdout = ("deadbeef\trefs/tags/v1.3.10\n"
                  "cafebabe\trefs/tags/v1.3.1-rc1^{}\n")
        stderr = ""

    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: FakeOut())
    assert cc._remote_tag_state(root, "1.3.1") == "absent"

    FakeOut.stdout = "deadbeef\trefs/tags/v1.3.1\n"
    assert cc._remote_tag_state(root, "1.3.1") == "present"

    FakeOut.stdout = "deadbeef\trefs/tags/v1.3.1^{}\n"
    assert cc._remote_tag_state(root, "1.3.1") == "present"


def test_remote_probe_error_is_unknown_not_absent(tmp_path, monkeypatch):
    root = make_repo(tmp_path)

    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(cc.subprocess, "run", boom)
    assert cc._remote_tag_state(root, "1.3.1") == "unknown"


# --- --require-plugin: a release gate cannot skip the bridge contract ------

def test_require_plugin_fails_when_no_checkout_was_given(tmp_path):
    root = make_repo(tmp_path)
    r = cc.check(root, tags=["1.3.0"], require_plugin=True)
    assert any("plugin checkout provided" in l for l in failures(r))


def test_without_require_plugin_the_skip_is_visible_but_not_a_failure(tmp_path):
    root = make_repo(tmp_path)
    r = cc.check(root, tags=["1.3.0"])
    assert not failures(r), r.lines
    assert any(l.startswith("SKIP") and "bridge schema" in l for l in r.lines)


def test_ci_does_not_need_a_flag_to_be_strict():
    """The workflow must not be the only thing enforcing strictness.

    It used to assert `check_consistency.py --strict` in the YAML; that made the
    gate a one-line edit away from silently disappearing. The script now
    defaults to strict under CI, so this test asserts the default instead.
    """
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8")
    assert "check_consistency.py" in workflow
    assert "--no-strict" not in workflow, (
        "CI must not opt out of strict mode")
