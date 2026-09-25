"""Tests for provider-native integration implementations."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestHookStartupHandler:
    """Tests for voyager hook startup command."""
    
    def test_import_success(self):
        from voyager.integrations.hook import startup_handler, cmd_hook_startup
        assert callable(startup_handler)
        assert callable(cmd_hook_startup)
    
    def test_no_thread_case(self, tmp_path):
        """Test when no active WorkThread exists."""
        from voyager.integrations.hook import startup_handler
        
        db_path = tmp_path / "index.db"
        
        result = startup_handler(
            provider="codex",
            cwd=str(tmp_path),
            session_id=None,
            db=db_path,
        )
        
        assert result["status"] == "no_thread"
        assert "[Voyager Continuity]" in result["context"]
    
    def test_ambiguous_threads_case(self, tmp_path):
        """Test that multiple active WorkThreads in same repo → AMBIGUOUS.
        
        SECURITY INVARIANT: Never silently pick one by updated_at.
        Multiple active threads must be rejected with clear diagnostic.
        """
        from voyager.store import Store
        from voyager.auto import discover_continuity
        from voyager.integrations.hook import startup_handler
        
        db_path = tmp_path / "index.db"
        store = Store(db_path)
        
        # Create two active threads in same repo
        tid1 = store.thread_create(
            title="Thread 1 - Refactoring",
            repo_root=str(tmp_path),
            goal="Refactor auth module",
        )
        tid2 = store.thread_create(
            title="Thread 2 - New Feature",
            repo_root=str(tmp_path),
            goal="Implement caching layer",
        )
        
        # Set both as active (even with different timestamps)
        now = 1726742400.0
        store.con.execute(
            "UPDATE threads SET status='active', updated_at=? WHERE id IN (?,?)",
            (now, tid1, tid2),
        )
        store.con.commit()
        
        # discover_continuity should return ambiguous
        disc = discover_continuity(store, cwd=str(tmp_path), repo=None)
        
        assert disc.get("continuity_available") is False
        assert disc.get("status") == "ambiguous"
        assert len(disc.get("candidate_threads", [])) == 2
        
        # startup_handler should also reject ambiguity
        result = startup_handler(
            provider="codex",
            cwd=str(tmp_path),
            session_id=None,
            db=db_path,
        )
        
        assert result["status"] == "ambiguous"
        assert "Ambiguous WorkThread resolution" in result["context"]
        assert tid1 in result["context"]
        assert tid2 in result["context"]
    
    def test_single_active_thread_selected(self, tmp_path):
        """Test that exactly one active thread → selected (even with closed threads present)."""
        from voyager.store import Store
        from voyager.auto import discover_continuity
        
        db_path = tmp_path / "index.db"
        store = Store(db_path)
        
        # Create one active + one closed thread in same repo
        active_tid = store.thread_create(
            title="Active Thread",
            repo_root=str(tmp_path),
            goal="Current work",
        )
        closed_tid = store.thread_create(
            title="Closed Thread",
            repo_root=str(tmp_path),
            goal="Old work",
        )
        
        # Set only one as active; close the other
        store.con.execute(
            "UPDATE threads SET status='closed', updated_at=? WHERE id=?",
            (1726742400.0, closed_tid),
        )
        store.con.commit()
        
        # Should select the active one
        disc = discover_continuity(store, cwd=str(tmp_path), repo=None)
        
        assert disc.get("continuity_available") is True
        assert disc.get("status") == "success"
        assert disc.get("active_thread", {}).get("id") == active_tid
    
    def test_ambiguous_with_different_timestamps(self, tmp_path):
        """Test that multiple active threads → ambiguous regardless of timestamps.
        
        This verifies we don't use max(updated_at) heuristics.
        """
        from voyager.store import Store
        from voyager.auto import discover_continuity
        
        db_path = tmp_path / "index.db"
        store = Store(db_path)
        
        # Create two active threads with very different timestamps
        older_tid = store.thread_create(
            title="Older Thread",
            repo_root=str(tmp_path),
            goal="Work from last week",
        )
        newer_tid = store.thread_create(
            title="Newer Thread",
            repo_root=str(tmp_path),
            goal="Work from today",
        )
        
        # Set different update times
        older_time = 1726742400.0  # Sept 18
        newer_time = 1726828800.0  # Sept 19
        
        store.con.execute(
            "UPDATE threads SET status='active', updated_at=? WHERE id=?",
            (older_time, older_tid),
        )
        store.con.execute(
            "UPDATE threads SET status='active', updated_at=? WHERE id=?",
            (newer_time, newer_tid),
        )
        store.con.commit()
        
        # Should still be ambiguous despite timestamp difference
        disc = discover_continuity(store, cwd=str(tmp_path), repo=None)
        
        assert disc.get("continuity_available") is False
        assert disc.get("status") == "ambiguous"
        assert len(disc.get("candidate_threads", [])) == 2
    
    def test_format_output(self, tmp_path):
        """Test continuation bundle format."""
        from voyager.integrations.hook import format_voyager_continuation
        
        mock_bundle = {
            "goal": "Test goal",
            "current_state": {"branch": "main"},
            "relevant_files": ["file1.py", "file2.py"],
            "next_steps": ["Step 1", "Step 2"],
            "decisions": ["Decision A"],
            "sessions": [{"id": "s1"}],
        }
        
        mock_thread = {
            "id": "thr_xxx",
            "title": "Test Thread",
            "goal": None,
        }
        
        mock_disc = {
            "repo_root": str(tmp_path),
            "warnings": [],
        }
        
        context = format_voyager_continuation(mock_bundle, mock_thread, mock_disc)
        
        assert "[Voyager Continuation]" in context
        assert "## Goal" in context
        assert "## Current State" in context
        assert "thr_xxx" in context
        assert "Test Thread" in context


class TestClaudeIntegration:
    """Tests for Claude SessionStart hook installation."""
    
    def test_install_creates_hooks_config(self, tmp_path):
        from voyager.integrations.claude import ClaudeIntegration
        
        claude = ClaudeIntegration(home=tmp_path)
        
        result = claude.install()
        
        assert result["status"] == "installed"
        assert result["strategy"] == "NATIVE_SESSIONSTART_HOOK"
        assert result["matcher"] == "startup"
        # The obsolete bash wrapper must NOT be created any more.
        assert not (tmp_path / ".claude/voyager_session_start.sh").exists()
    
    def test_install_additive_merge(self, tmp_path):
        """Test that existing hooks are preserved."""
        from voyager.integrations.claude import ClaudeIntegration
        
        settings_file = tmp_path / ".claude/settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        
        existing_config = {
            "env": {"TEST": "value"},
            "hooks": {
                "SessionStart": [
                    {"name": "existing-hook", "command": "/bin/old"},
                ]
            },
        }
        settings_file.write_text(json.dumps(existing_config))
        
        claude = ClaudeIntegration(home=tmp_path)
        result = claude.install()
        
        # Verify both hooks present
        config = json.loads(settings_file.read_text())
        hooks = config["hooks"]["SessionStart"]
        
        assert len(hooks) == 2
        assert any(h.get("name") == "existing-hook" for h in hooks)
        voyager = [h for h in hooks if h.get("matcher") == "startup"]
        assert len(voyager) == 1
        inner = voyager[0]["hooks"]
        assert inner[0]["type"] == "command"
        assert "claude_session_start.py" in inner[0]["command"]
        assert isinstance(inner[0]["timeout"], int) and inner[0]["timeout"] > 0
    
    def test_remove_only_voyager_entry(self, tmp_path):
        """Test remove preserves user hooks."""
        from voyager.integrations.claude import ClaudeIntegration
        
        settings_file = tmp_path / ".claude/settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        
        config = {
            "hooks": {
                "SessionStart": [
                    {"name": "user-hook", "command": "/bin/user"},
                    {"name": "voyager-session-start", "command": "/path/to/voyager"},
                    {"name": "another-user-hook", "command": "/bin/another"},
                ]
            },
        }
        settings_file.write_text(json.dumps(config))
        
        claude = ClaudeIntegration(home=tmp_path)
        claude.remove()
        
        # Verify only Voyager removed
        loaded = json.loads(settings_file.read_text())
        hooks = loaded["hooks"]["SessionStart"]
        
        assert len(hooks) == 2
        names = [h.get("name") for h in hooks]
        assert "user-hook" in names
        assert "another-user-hook" in names
        assert "voyager-session-start" not in names
    
    def test_malformed_config_refusal(self, tmp_path):
        """Test malformed JSON triggers backup and refusal."""
        from voyager.integrations.claude import ClaudeIntegration
        
        settings_file = tmp_path / ".claude/settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("{ invalid json }")
        
        claude = ClaudeIntegration(home=tmp_path)
        result = claude.install()
        
        assert result["status"] == "error"
        assert result["strategy"] == "FAILED_MALFORMED_CONFIG"
        assert "malformed" in result.get("message", "").lower()
    
    def test_verify_installed_status(self, tmp_path):
        """Test verify() correctly reports installed state."""
        from voyager.integrations.claude import ClaudeIntegration
        
        claude = ClaudeIntegration(home=tmp_path)
        
        install_result = claude.install()
        verify_result = claude.verify()
        
        assert verify_result["verified"] is True
        assert verify_result["checks"]["voyager_entry_present"] is True

    def test_install_writes_claude_code_schema(self, tmp_path):
        """The entry must use Claude Code's documented SessionStart shape.

        An earlier revision emitted a flat entry
        ({"name", "event", "command", "priority", "timeout_ms"}), which Claude
        Code ignores because it looks for `hooks[].hooks[].type == "command"`.
        """
        from voyager.integrations.claude import ClaudeIntegration

        claude = ClaudeIntegration(home=tmp_path)
        claude.install()

        config = json.loads((tmp_path / ".claude/settings.json").read_text())
        matchers = config["hooks"]["SessionStart"]
        assert len(matchers) == 1

        matcher = matchers[0]
        assert matcher["matcher"] == "startup"
        assert set(matcher) == {"matcher", "hooks"}

        inner = matcher["hooks"]
        assert len(inner) == 1
        assert inner[0]["type"] == "command"
        assert "claude_session_start.py" in inner[0]["command"]
        # `timeout` is seconds in Claude Code; the old shape used `timeout_ms`.
        assert inner[0]["timeout"] == 120
        assert "timeout_ms" not in inner[0]

    def test_install_command_is_absolute_and_quoted(self, tmp_path):
        from voyager.integrations.claude import ClaudeIntegration

        claude = ClaudeIntegration(home=tmp_path)
        command = claude.hook_command(interpreter="/usr/bin/python3")

        assert command == f'"/usr/bin/python3" "{claude.entrypoint}"'
        assert command.count('"') == 4

    def test_install_upgrades_legacy_flat_entry(self, tmp_path):
        """A pre-2026-09-21 Voyager entry is replaced, never duplicated."""
        from voyager.integrations.claude import ClaudeIntegration

        settings_file = tmp_path / ".claude/settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({
            "hooks": {
                "SessionStart": [
                    {"name": "voyager-session-start",
                     "event": "sessionStart",
                     "command": str(tmp_path / ".claude/voyager_session_start.sh"),
                     "timeout_ms": 30000},
                ]
            }
        }))

        ClaudeIntegration(home=tmp_path).install()

        matchers = json.loads(settings_file.read_text())["hooks"]["SessionStart"]
        assert len(matchers) == 1
        assert matchers[0]["matcher"] == "startup"
        assert "claude_session_start.py" in matchers[0]["hooks"][0]["command"]

    def test_install_preserves_unrelated_settings_and_backs_up(self, tmp_path):
        from voyager.integrations.claude import ClaudeIntegration

        settings_file = tmp_path / ".claude/settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({
            "env": {"ANTHROPIC_MODEL": "x"},
            "permissions": {"allow": ["Bash(cat)"]},
            "theme": "dark-ansi",
        }))

        result = ClaudeIntegration(home=tmp_path).install()

        config = json.loads(settings_file.read_text())
        assert config["env"] == {"ANTHROPIC_MODEL": "x"}
        assert config["permissions"] == {"allow": ["Bash(cat)"]}
        assert config["theme"] == "dark-ansi"
        # A backup of the pre-install file must exist and hold the original.
        assert result["backup"] and Path(result["backup"]).exists()
        assert "hooks" not in json.loads(Path(result["backup"]).read_text())

    def test_verify_rejects_legacy_flat_entry(self, tmp_path):
        """Present-but-inert is not the same as verified."""
        from voyager.integrations.claude import ClaudeIntegration

        settings_file = tmp_path / ".claude/settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({
            "hooks": {"SessionStart": [{"name": "voyager-session-start",
                                        "command": "/old/voyager"}]}
        }))

        checks = ClaudeIntegration(home=tmp_path).verify()["checks"]
        assert checks["voyager_entry_present"] is False
        assert checks["command_uses_entrypoint"] is False

    def test_remove_leaves_no_empty_containers(self, tmp_path):
        from voyager.integrations.claude import ClaudeIntegration

        claude = ClaudeIntegration(home=tmp_path)
        claude.install()
        result = claude.remove()

        assert result["removed"] == 1
        config = json.loads((tmp_path / ".claude/settings.json").read_text())
        assert "hooks" not in config

    def test_install_errors_when_entrypoint_missing(self, tmp_path):
        from voyager.integrations.claude import ClaudeIntegration

        claude = ClaudeIntegration(home=tmp_path)
        claude.entrypoint = tmp_path / "not_there.py"
        result = claude.install()

        assert result["status"] == "error"
        assert result["strategy"] == "FAILED_MISSING_ENTRYPOINT"


class TestCursorIntegration:
    """Tests for Cursor sessionStart hook."""
    
    def test_install_cre_hook_config(self, tmp_path):
        from voyager.integrations.cursor import CursorIntegration
        
        cursor = CursorIntegration(home=tmp_path)
        
        result = cursor.install()
        
        assert result["status"] == "installed"
        assert "sessionStart" in str(result)
    
    def test_install_preserves_other_hooks(self, tmp_path):
        from voyager.integrations.cursor import CursorIntegration
        
        settings_file = tmp_path / ".cursor/settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        
        existing = {
            "hooks": {
                "sessionStart": [
                    {"name": "pre-existing", "event": "sessionStart"},
                ]
            },
        }
        settings_file.write_text(json.dumps(existing))
        
        cursor = CursorIntegration(home=tmp_path)
        cursor.install()
        
        config = json.loads(settings_file.read_text())
        hooks = config["hooks"]["sessionStart"]
        
        assert len(hooks) >= 2
        assert any(h.get("name") == "pre-existing" for h in hooks)
        assert any(h.get("name") == "voyager-session-start" for h in hooks)
    
    def test_returns_additional_context_format(self, tmp_path):
        """Verify hook configuration supports additional_context response."""
        from voyager.integrations.cursor import CursorIntegration
        
        cursor = CursorIntegration(home=tmp_path)
        result = cursor.install()
        
        # Check configuration mentions additional_context field
        assert "response_format" in result
        assert "additional_context" in str(result.get("response_format", {}))


class TestKiroSplitIntegration:
    """Tests for Kiro IDE and CLI split implementation."""
    
    def test_install_both_modes(self, tmp_path):
        from voyager.integrations.kiro import KiroIntegration
        
        kiro = KiroIntegration(home=tmp_path)
        
        result = kiro.install()
        
        assert "ide" in result
        assert "cli" in result
        assert result["ide"]["strategy"] == "SESSION_START_ZERO_TOUCH"
        assert result["cli"]["strategy"] == "LAUNCHER_ZERO_TOUCH"
    
    def test_ide_hook_creates_script(self, tmp_path):
        from voyager.integrations.kiro import KiroIntegration
        
        kiro = KiroIntegration(home=tmp_path)
        kiro.install_ide()
        
        settings_file = tmp_path / ".kiro/settings.json"
        assert settings_file.exists()
        
        config = json.loads(settings_file.read_text())
        assert "hooks" in config
        assert "sessionStart" in config["hooks"]
    
    def test_cli_launcher_created(self, tmp_path):
        from voyager.integrations.kiro import KiroIntegration
        
        kiro = KiroIntegration(home=tmp_path)
        
        # Mock shutil.which to return fake path
        with patch("shutil.which", return_value="/fake/kiro"):
            result = kiro.install_cli()
        
        launcher = tmp_path / ".voyager/bin/kiro-cli"
        assert launcher.exists()
        
        # On Windows, executable check doesn't work the same way - just verify file exists and has content
        import sys
        if sys.platform == "win32":
            assert launcher.read_text(encoding="utf-8").strip()
        else:
            assert launcher.stat().st_mode & 0o111 != 0  # Executable


class TestLauncherPrelaunch:
    """Tests for voyager launcher prelaunch command."""
    
    def test_prelaunch_success_no_thread(self, tmp_path):
        from voyager.launcher import prelaunch
        
        result = prelaunch(
            provider="test",
            cwd=str(tmp_path),
            db=str(tmp_path / "test.db"),
        )
        
        assert result["provider"] == "test"
        assert result["status"] in ("success", "no_thread")
    
    def test_prelaunch_json_output(self, tmp_path):
        """Test launcher returns valid JSON."""
        import subprocess
        import os
        
        # Set up environment so subprocess can find voyager module
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        
        result = subprocess.run(
            [
                "python", "-m", "voyager.cli", "launcher", "prelaunch",
                "--provider=test",
                f"--cwd={tmp_path}",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        
        # Combine stdout and stderr (launcher may output to either)
        output = (result.stdout + result.stderr).strip()
        assert output, f"Should produce output. stderr: {result.stderr}"
        
        try:
            data = json.loads(output)
            assert "provider" in data
            assert "status" in data
        except json.JSONDecodeError:
            pytest.fail(f"Output not valid JSON: {output}")


class TestCapabilitySeparation:
    """Tests for platform_supported vs locally_configured separation."""
    
    def test_capability_has_three_statuses(self):
        from voyager.integrations.capabilities import ProviderCapabilityStatus
        
        status = ProviderCapabilityStatus()
        
        assert hasattr(status, "platform_supported")
        assert hasattr(status, "locally_configured")
        assert hasattr(status, "live_verified")
    
    def test_default_all_false(self):
        from voyager.integrations.capabilities import ProviderCapabilityStatus
        
        status = ProviderCapabilityStatus()
        
        assert status.platform_supported is False
        assert status.locally_configured is False
        assert status.live_verified is False

    def test_provider_capabilities_method_is_not_shadowed(self, tmp_path):
        """`capabilities()` must be reachable on every provider class.

        Each class set `self.capabilities = None` in `__init__` while *also*
        defining `def capabilities(self)`, so the instance attribute shadowed
        the method and calling it raised
        `TypeError: 'NoneType' object is not callable`.  Nothing called it, so
        it went unnoticed; this locks the contract before something does.
        """
        from voyager.integrations import (
            AntigravityIntegration, ClaudeIntegration, CodexIntegration,
            DSHIntegration, GrokIntegration, ZCodeIntegration,
        )
        from voyager.integrations.capabilities import ProviderCapabilities

        classes = [
            AntigravityIntegration, ClaudeIntegration, CodexIntegration,
            DSHIntegration, GrokIntegration, ZCodeIntegration,
        ]
        for cls in classes:
            profile = cls(home=tmp_path).capabilities()
            assert isinstance(profile, ProviderCapabilities), cls.__name__
            assert profile.provider, cls.__name__


class TestGrokCapabilityReporting:
    """Grok's reported capability has to match what was observed live.

    Live-verified 2026-09-25: Grok's native `SessionStart` hook fired, the
    environment carried `GROK_SESSION_ID`, and the pending row stored that
    identity.  The flags below were hardcoded from the pre-implementation
    investigation and had been contradicting that evidence.

    `mcp_supported` is deliberately **not** flipped: its contract (a native MCP
    surface vs. loading a compatibility layer vs. Voyager managing its own
    server) is unresolved, so it stays `False` until that is settled.  It is
    pinned here only so a later change has to be deliberate.
    """

    def test_capability_flags_match_the_live_evidence(self, tmp_path):
        from voyager.integrations.capabilities import (
            ZeroTouchLevel, detect_capabilities,
        )
        from voyager.skill import PROVIDER_CONFIG

        caps = detect_capabilities("grok", tmp_path)
        assert caps.has_session_start_hook is True
        assert caps.native_session_id_at_start is True
        assert caps.max_zero_touch_level is ZeroTouchLevel.SESSION_START_ZERO_TOUCH
        # unresolved contract -- unchanged on purpose
        assert caps.mcp_supported is False
        # The second capability table (`skill.PROVIDER_CONFIG`) is not read by
        # any code today, but it must not contradict the detector again.
        assert PROVIDER_CONFIG["grok"]["has_startup_hook"] is True

    def test_installed_native_hook_reports_h_not_y(self, tmp_path, monkeypatch):
        import shutil

        from voyager.skill import check_integration_status, install_integration

        home = tmp_path / "home"
        (home / ".grok").mkdir(parents=True)

        # Grok's installer needs the real binary to resolve; the letter must not
        # depend on this machine having Grok installed.
        real_which = shutil.which
        monkeypatch.setattr(
            "shutil.which",
            lambda name, *args, **kwargs: (
                "/opt/grok/bin/grok" if name == "grok"
                else real_which(name, *args, **kwargs)),
        )

        # With no hook on disk the pre-existing rule still applies.
        before = check_integration_status(providers=["grok"], home=home)[0]
        assert before["hook"]["registered"] is False
        assert before["startup_status"] == "N"

        installed = install_integration("grok", force=True, home=home)
        assert installed["hook"]["status"] == "installed"

        after = check_integration_status(providers=["grok"], home=home)[0]
        assert after["hook"]["registered"] is True
        assert after["startup_status"] == "H"
        # Registering a hook is not live evidence, so it must never be `Y`.
        assert after["startup_status"] != "Y"


class TestIdempotency:
    """Test install/remove idempotency."""
    
    def test_claude_install_twice_safe(self, tmp_path):
        """Installing twice should be safe and idempotent."""
        from voyager.integrations.claude import ClaudeIntegration
        
        claude = ClaudeIntegration(home=tmp_path)
        
        result1 = claude.install()
        result2 = claude.install()
        
        # Both should succeed
        assert result1["status"] == "installed"
        assert result2["status"] == "installed"
        
        # Should have exactly one voyager hook (idempotent - doesn't duplicate)
        import json
        config = json.loads((tmp_path / ".claude/settings.json").read_text())
        hooks = config["hooks"]["SessionStart"]
        assert len(hooks) == 1  # Only the voyager matcher (no duplicates)
        assert hooks[0]["matcher"] == "startup"
        assert "claude_session_start.py" in hooks[0]["hooks"][0]["command"]
    
    def test_claude_remove_twice_safe(self, tmp_path):
        """Removing twice should be safe."""
        from voyager.integrations.claude import ClaudeIntegration
        
        claude = ClaudeIntegration(home=tmp_path)
        
        claude.install()
        result1 = claude.remove()
        result2 = claude.remove()
        
        # Both should succeed
        assert result1["status"] == "removed"
        assert result2["status"] == "removed"


class TestPathHandling:
    """Test Windows vs POSIX path handling."""
    
    def test_temp_path_works(self, tmp_path):
        from voyager.integrations.hook import startup_handler
        
        result = startup_handler(
            provider="test",
            cwd=str(tmp_path),
        )
        
        # Should handle any valid path
        assert "status" in result
        assert "context" in result


# Run with: python -m pytest tests/test_integrations.py -v
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
