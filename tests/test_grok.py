"""Grok CLI adapter regression test.

Fixture: tests/fixtures/grok/sessions/<urlencoded-cwd>/<uuid>/ with
chat_history.jsonl + summary.json (git metadata, summary title, model).

Also covers the opt-in launcher wrapper (`~/.voyager/bin/grok`), which had no
test at all: the repo-root `test_grok_wrapper*.py` scripts checked it, but they
were script-style (top-level code, no `def test_`), so pytest never collected
them and nothing ran on CI.
"""

from __future__ import annotations

import os

import pytest


def test_grok_parse(adapter_of, patch_paths, grok_fixture):
    ad = adapter_of("grok")
    patch_paths(ad, SESSIONS_DIR=grok_fixture)
    files = ad.discover()
    assert len(files) == 1
    r = ad.parse(files[0])
    s, evs = r["session"], r["events"]
    assert s["native_session_id"] == "01990000-1111-7777-8888-000000000000"
    assert s["title"] == "check server"
    assert s["git_remote"] == "git@github.com:u/demo.git"
    assert s["resume_cmd"] == "grok -r 01990000-1111-7777-8888-000000000000"
    kinds = [e["kind"] for e in evs]
    assert kinds.count("tool_call") == 1 and kinds.count("tool_result") == 1
    assert s["message_count"] == 3 and s["tool_count"] == 1


# --- launcher wrapper ------------------------------------------------------

FAKE_GROK = "/opt/grok/bin/grok"


@pytest.fixture
def grok_with_cli(monkeypatch):
    """Pretend a real `grok` binary is on PATH."""
    monkeypatch.setattr("shutil.which", lambda name: FAKE_GROK if name == "grok" else None)


def test_grok_install_without_cli_is_a_clean_error(tmp_path, monkeypatch):
    from voyager.integrations.grok import GrokIntegration

    monkeypatch.setattr("shutil.which", lambda name: None)
    result = GrokIntegration(home=tmp_path).install()

    assert result["status"] == "error"
    assert "not found" in result["message"]
    # Nothing may be written when the prerequisite is missing.
    assert not (tmp_path / ".voyager/bin").exists()


def test_grok_launcher_install_verify_remove(tmp_path, grok_with_cli):
    from voyager.integrations.grok import GrokIntegration

    gi = GrokIntegration(home=tmp_path)
    launcher = tmp_path / ".voyager/bin/grok"

    # verify() must *report* a missing launcher, not raise.  It used to call
    # launcher.stat() unconditionally and die with FileNotFoundError.
    before = gi.verify()
    assert before["verified"] is False
    assert before["checks"]["launcher_exists"] is False

    assert gi.install()["status"] == "installed"
    assert launcher.is_file()

    body = launcher.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/sh")
    assert f'real_executable="{FAKE_GROK}"' in body
    # The recursion guard and the prelaunch call are the entire point of the
    # wrapper; without them it is a no-op or an infinite loop.
    assert "VOYAGER_LAUNCHER_RUNNING" in body
    assert "voyager launcher prelaunch" in body
    assert 'exec "$real_executable" "$@"' in body

    assert gi.verify()["verified"] is True

    assert gi.remove()["status"] == "removed"
    assert not launcher.exists()
    assert gi.verify()["verified"] is False


def test_grok_verify_does_not_require_the_posix_execute_bit_on_windows(tmp_path, grok_with_cli):
    """`verified` must be reachable on Windows.

    `chmod(0o755)` on Windows leaves `st_mode` at `0o100666`, so a hard
    requirement on `st_mode & 0o111` made `verified` permanently False there.
    """
    from voyager.integrations.grok import GrokIntegration

    gi = GrokIntegration(home=tmp_path)
    gi.install()
    result = gi.verify()

    assert result["checks"]["launcher_exists"] is True
    if os.name == "posix":
        assert result["checks"]["executable"] is True
    else:
        assert result["checks"]["executable"] is False
    assert result["verified"] is True
