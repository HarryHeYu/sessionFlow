"""Handoff tests: context package content, launch argv, and the CLI paths
that produce them (no target process is ever spawned here)."""

from __future__ import annotations

import pytest

from voyager.cli import main
from voyager.handoff import (HANDOFF_INSTRUCTION, PROMPT_TARGETS,
                             build_context_package, default_package_name,
                             handoff_command)


def test_context_package_has_every_section(indexed_store, codex_row):
    package = build_context_package(indexed_store, codex_row)

    assert package.startswith("# Agent Handoff — Context Package")
    assert "**codex** session `11111111-2222-3333-4444-555555555555`" in package
    assert "- Title: fix the parser" in package
    assert "- Repository: `E:/proj/demo`" in package
    assert "- Remote: git@github.com:u/demo.git" in package
    assert "- Model: gpt-5-codex" in package
    # goal + follow-ups
    assert "## User goal / instructions" in package
    assert "**Original request:**" in package and "fix the parser" in package
    # where it stopped
    assert "## Where the work stopped (last assistant message)" in package
    assert "parser fixed" in package
    # files / commands / errors
    assert "## Files this session touched" in package
    assert "`E:/proj/demo/parser.py`" in package
    assert "## Commands executed" in package
    assert "python -m pytest -q" in package
    assert "## Errors encountered" in package
    assert "flaky network call" in package
    assert "## Recent timeline (condensed)" in package


def test_context_package_without_user_messages_says_so(indexed_store, tmp_path):
    """A session with no user text must not produce an empty section."""
    from voyager.model import new_event, new_session
    from voyager.store import Store

    store = Store(tmp_path / "empty.db")
    src = tmp_path / "src.jsonl"
    src.write_text("{}", encoding="utf-8")
    s = new_session(id="codex:none", provider="codex", native_session_id="none",
                    title="tool only", updated_at=1757000000.0)
    store.replace_session(s, [new_event(sid="codex:none", seq=1, kind="tool_call",
                                        tool_name="shell", command="ls")],
                          "codex", src)
    row, _ = store.session("codex:none")
    package = build_context_package(store, row)
    assert "(no user messages captured)" in package
    assert "## Where the work stopped" not in package
    store.close()


def test_handoff_command_argv(tmp_path):
    pkg = tmp_path / "handoff.md"
    for target in PROMPT_TARGETS:
        argv = handoff_command(target, pkg)
        assert argv[0] == PROMPT_TARGETS[target]
        assert argv[1].startswith(HANDOFF_INSTRUCTION)
        assert str(pkg.resolve()) in argv[1]
    assert handoff_command("cursor", pkg) is None      # no launch path


def test_default_package_name_is_path_safe(codex_row, zcode_row):
    name = default_package_name(codex_row)
    assert name.startswith("handoff-codex-11111111-2222-3333-4444")
    assert name.endswith(".md")
    assert default_package_name(zcode_row) == "handoff-zcode-sess_z9.md"
    assert "/" not in name and "\\" not in name and ":" not in name


def test_cli_handoff_writes_package_and_prints_launch(tmp_path, indexed_store,
                                                      codex_row, capsys):
    out = tmp_path / "pkg.md"
    rc = main(["--db", str(indexed_store.db_path),
               "handoff", "11111111-2222", "--to", "codex", "-o", str(out)])
    printed = capsys.readouterr().out
    assert rc == 0
    assert out.is_file()
    assert "fix the parser" in out.read_text(encoding="utf-8")
    assert "context package:" in printed
    assert '$ codex "<handoff prompt>"' in printed
    assert "add --launch to start it now" in printed


def test_cli_handoff_to_unsupported_target_keeps_package(tmp_path, indexed_store,
                                                         capsys):
    out = tmp_path / "pkg.md"
    rc = main(["--db", str(indexed_store.db_path),
               "handoff", "11111111-2222", "--to", "cursor", "-o", str(out)])
    printed = capsys.readouterr().out
    assert rc == 1
    assert out.is_file(), "package must still be written for manual paste"
    assert "not supported for 'cursor'" in printed


def test_cli_handoff_without_target_suggests_one(tmp_path, indexed_store, capsys):
    out = tmp_path / "pkg.md"
    rc = main(["--db", str(indexed_store.db_path),
               "handoff", "11111111-2222", "-o", str(out)])
    printed = capsys.readouterr().out
    assert rc == 0
    assert "next: pick a target agent" in printed
    assert "claude, codex, grok" in printed


def test_cli_handoff_unknown_session_exits_2(indexed_store, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--db", str(indexed_store.db_path), "handoff", "does-not-exist"])
    assert e.value.code == 2
    assert "session not found" in capsys.readouterr().err
