"""Phase 5 Voyager Skill tests (roadmap #6).

Contracts:
- the packaged SKILL.md exists and routes (mentions the core commands)
- install succeeds on first run, is idempotent on repeat
- a user-modified target is refused without --force, backed up and
  overwritten with --force
- agents that are not installed are skipped, never fabricated
- unknown agents get a manual-install note
- provider session directories are never touched
"""

from __future__ import annotations

import json

import pytest

from voyager.cli import main
from voyager.skill import skill_source


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME with codex + claude installed, grok absent."""
    h = tmp_path / "home"
    for name in ("codex", "claude"):
        (h / f".{name}").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    return h


def test_skill_source_exists_and_routes():
    src = skill_source()
    assert src.is_file()
    text = src.read_text(encoding="utf-8")
    for cmd in ("voyager brief", "voyager search", "voyager show",
                "voyager continue", "voyager handoff", "voyager merge"):
        assert cmd in text, f"SKILL.md must route to {cmd}"
    # prohibitions are explicit
    assert "NEVER" in text
    assert "session director" in text      # covers directory/directories
    assert "lease" in text.lower()


def test_install_first_run(tmp_path, home, capsys):
    assert main(["skill", "install"]) == 0
    out = capsys.readouterr().out
    for name, status in (("codex", "installed"), ("claude", "installed"),
                         ("grok", "skipped")):
        assert f"{name:<8} {status}" in out, out
    installed = home / ".codex" / "skills" / "voyager" / "SKILL.md"
    assert installed.is_file()
    assert installed.read_text(encoding="utf-8") == \
        skill_source().read_text(encoding="utf-8")
    # grok is not installed -> no directory fabricated
    assert not (home / ".grok").exists()


def test_install_is_idempotent(tmp_path, home):
    assert main(["skill", "install"]) == 0
    first = (home / ".claude" / "skills" / "voyager" / "SKILL.md")
    content = first.read_text(encoding="utf-8")
    assert main(["skill", "install"]) == 0
    assert first.read_text(encoding="utf-8") == content


def test_install_refuses_user_modified_without_force(tmp_path, home, capsys):
    assert main(["skill", "install"]) == 0
    target = home / ".claude" / "skills" / "voyager" / "SKILL.md"
    target.write_text("my customized skill", encoding="utf-8")

    assert main(["skill", "install"]) == 0
    out = capsys.readouterr().out
    assert "refused" in out
    assert target.read_text(encoding="utf-8") == "my customized skill"

    # --force backs up then overwrites
    assert main(["skill", "install", "--force"]) == 0
    out = capsys.readouterr().out
    assert "updated" in out and "backup" in out
    assert skill_source().read_text(encoding="utf-8") in \
        target.read_text(encoding="utf-8")
    backups = list(target.parent.glob("SKILL.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "my customized skill"


def test_install_unknown_agent_gets_manual_path(tmp_path, home, capsys):
    assert main(["skill", "install", "--agent", "warp"]) == 0
    out = capsys.readouterr().out
    assert "unknown-agent" in out
    assert "manual" in out


def test_install_never_touches_provider_session_dirs(tmp_path, home, capsys):
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "rollout-x.jsonl").write_text("{}", encoding="utf-8")
    assert main(["skill", "install"]) == 0
    files = sorted(str(p) for p in sessions.rglob("*"))
    assert files == [str(sessions / "rollout-x.jsonl")]
