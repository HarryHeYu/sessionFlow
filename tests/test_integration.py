"""Integration installation/uninstallation tests for MCP and bootstrap."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from voyager.cli import main
from voyager.skill import (
    install_integration, uninstall_integration, 
    check_integration_status, _register_codex_mcp, _register_claude_mcp
)


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """Isolated Codex home with config.toml but no Voyager entry."""
    h = tmp_path / "home"
    codex_dir = h / ".codex"
    codex_dir.mkdir(parents=True)
    
    # Create an empty config.toml
    config_file = codex_dir / "config.toml"
    config_file.write_text("# Empty config\n", encoding="utf-8")
    
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def codex_with_voyager(codex_home):
    """Codex home that already has Voyager registered."""
    config = codex_home / ".codex/config.toml"
    content = config.read_text(encoding="utf-8")
    if "[mcp_servers.voyager]" not in content:
        content += "\n[mcp_servers.voyager]\ncommand = \"python\"\nargs = [\"-m\", \"voyager.mcp_server\"]\nenv = {}\n"
    config.write_text(content, encoding="utf-8")
    return codex_home


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Isolated Claude Code home with mcp.json but no Voyager entry."""
    h = tmp_path / "home"
    claude_dir = h / ".claude"
    claude_dir.mkdir(parents=True)
    
    # Create an empty mcp.json
    mcp_file = claude_dir / "mcp.json"
    mcp_file.write_text(json.dumps({"MCP_SERVERS": []}, indent=2), encoding="utf-8")
    
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def claude_with_voyager(claude_home):
    """Claude home that already has Voyager registered."""
    mcp_file = claude_home / ".claude/mcp.json"
    config = json.loads(mcp_file.read_text(encoding="utf-8"))
    if "python -m voyager.mcp_server" not in config["MCP_SERVERS"]:
        config["MCP_SERVERS"].append("python -m voyager.mcp_server")
    mcp_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return claude_home


class TestCodexMCPRegistration:
    """Test Codex MCP configuration path and format."""
    
    def test_config_path_is_codex_not_config_codex(self, codex_home):
        """Codex uses ~/.codex/config.toml, NOT ~/.config/codex/config.toml."""
        result = _register_codex_mcp(codex_home)
        
        assert result[0] == "registered"
        expected_path = str(codex_home / ".codex/config.toml")
        assert result[1] == expected_path
        
        # Verify file exists at correct location
        assert (codex_home / ".codex/config.toml").exists()
        assert not (codex_home / ".config/codex/config.toml").exists()
    
    def test_toml_syntax_single_brackets(self, codex_home):
        """Config uses [mcp_servers.voyager], NOT [[mcp_servers.voyager]]."""
        result = _register_codex_mcp(codex_home)
        assert result[0] == "registered"
        
        content = (codex_home / ".codex/config.toml").read_text(encoding="utf-8")
        
        # Must have single-bracket TOML table syntax
        assert "[mcp_servers.voyager]" in content
        
        # Must NOT have double-bracket array-of-tables syntax
        assert "[[mcp_servers.voyager]]" not in content
    
    def test_creates_parent_directory_if_missing(self, tmp_path, monkeypatch):
        """Creates ~/.codex/ directory if it doesn't exist."""
        h = tmp_path / "home"
        # Don't create .codex directory
        monkeypatch.setenv("USERPROFILE", str(h))
        monkeypatch.setenv("HOME", str(h))
        
        result = _register_codex_mcp(h)
        assert result[0] == "registered"
        assert (h / ".codex").is_dir()
        assert (h / ".codex/config.toml").exists()
    
    def test_idempotent_install(self, codex_home):
        """Install is idempotent - second call returns up-to-date."""
        first_result = _register_codex_mcp(codex_home)
        assert first_result[0] == "registered"
        
        second_result = _register_codex_mcp(codex_home)
        assert second_result[0] == "up-to-date"
        
        # File should not be duplicated
        content = (codex_home / ".codex/config.toml").read_text(encoding="utf-8")
        assert content.count("[mcp_servers.voyager]") == 1
    
    def test_additive_merge_preserves_existing_config(self, codex_home):
        """New entries are added without destroying existing configuration."""
        # Start with some custom config
        config = codex_home / ".codex/config.toml"
        original_content = """# Custom settings
[some_other_server]
command = "custom-command"
args = ["--flag"]

[mcp_servers.another]
command = "another"
"""
        config.write_text(original_content, encoding="utf-8")
        
        result = _register_codex_mcp(codex_home)
        assert result[0] == "registered"
        
        # Verify both Voyager and existing config present
        content = config.read_text(encoding="utf-8")
        assert "[some_other_server]" in content
        assert "[mcp_servers.another]" in content
        assert "[mcp_servers.voyager]" in content


class TestClaudeMCPRegistration:
    """Test Claude Code MCP configuration via CLI."""
    
    def test_cli_command_format(self, monkeypatch, claude_home):
        """Uses -- separator between name and executable."""
        # Mock subprocess.run to capture the command
        captured = []
        def mock_run(*args, **kwargs):
            captured.append(args[0] if args else kwargs.get('args', []))
            # Return success without actually running
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()
        
        monkeypatch.setattr('subprocess.run', mock_run)
        result = _register_claude_mcp(claude_home)
        
        assert result[0] == "registered"
        assert len(captured) == 1
        cmd = captured[0]
        
        # Verify format: claude mcp add voyager -- python -m voyager.mcp_server
        assert "claude" in cmd[0]
        assert "mcp" in cmd[1]
        assert "add" in cmd[2]
        assert "voyager" in cmd
        assert "--" in cmd  # Separator must be present
        assert "python" in cmd
        assert "-m" in cmd
        assert "voyager.mcp_server" in cmd
    
    def test_fallback_when_cli_unavailable(self, claude_home, monkeypatch):
        """When CLI fails, writes valid JSON directly."""
        # Make CLI fail
        def failing_run(*args, **kwargs):
            raise FileNotFoundError("claude not found")
        
        monkeypatch.setattr('subprocess.run', failing_run)
        
        result = _register_claude_mcp(claude_home)
        assert result[0] in ("registered", "manual_required")
        
        # Should still write to mcp.json if CLI unavailable
        mcp_file = claude_home / ".claude/mcp.json"
        if result[0] == "registered":
            assert mcp_file.exists()
            config = json.loads(mcp_file.read_text(encoding="utf-8"))
            assert "MCP_SERVERS" in config
            if isinstance(config["MCP_SERVERS"], list):
                assert any("voyager" in s.lower() for s in config["MCP_SERVERS"])
    
    def test_cli_success_creates_validated_config(self, claude_with_voyager, monkeypatch):
        """When CLI succeeds, uses its validated config format."""
        def success_run(*args, **kwargs):
            class Result:
                returncode = 0
                stdout = "Added MCP server 'voyager'"
                stderr = ""
            return Result()
        
        monkeypatch.setattr('subprocess.run', success_run)
        result = _register_claude_mcp(claude_with_voyager)
        
        # CLI succeeded, should get registered status
        assert result[0] == "registered"


class TestStatusChecksConsistentWithRegistration:
    """Status checks must see exactly what registration wrote."""
    
    def test_codex_status_sees_config_toml(self, codex_home):
        """Status checks .codex/config.toml, not .config/codex/mcp.json."""
        # Register via our function
        _register_codex_mcp(codex_home)
        
        # Status must see it
        status = check_integration_status(providers=["codex"], home=codex_home)[0]
        
        assert status["mcp"]["available"] is True
        assert status["mcp"]["registered"] is True
        assert "registered" in status["mcp"]["message"].lower()
    
    def test_codex_remove_then_status_reflects_absent(self, codex_home):
        """After remove, status shows not registered."""
        # First register
        _register_codex_mcp(codex_home)
        status_before = check_integration_status(providers=["codex"], home=codex_home)[0]
        assert status_before["mcp"]["registered"] is True
        
        # Then remove
        uninstall_integration(provider="codex", home=codex_home)
        
        # Status must reflect removal
        status_after = check_integration_status(providers=["codex"], home=codex_home)[0]
        assert status_after["mcp"]["registered"] is False


class TestFullLifecycle:
    """End-to-end: install → status → remove → verify cleanup."""
    
    def test_codex_full_lifecycle(self, codex_home):
        """install → idempotent install → status → remove → final status."""
        # Install
        result1 = install_integration(provider="codex", home=codex_home)
        assert result1["status"] == "success"
        assert result1["skill"]["status"] == "installed"
        assert result1["mcp"]["status"] == "registered"
        
        # Idempotent install
        result2 = install_integration(provider="codex", home=codex_home)
        assert result2["status"] in ("success", "partial")  # skill may be up-to-date or installed
        
        # Check status
        status = check_integration_status(providers=["codex"], home=codex_home)[0]
        assert status["skill"]["installed"] is True
        assert status["mcp"]["registered"] is True
        
        # Remove
        remove_result = uninstall_integration(provider="codex", home=codex_home)
        assert remove_result["status"] == "removed"
        assert remove_result["skill"]["status"] == "removed"
        assert remove_result["mcp"]["status"] in ("removed", "not-found")
        
        # Verify Skill removed
        assert not (codex_home / ".codex/skills/voyager/SKILL.md").exists()
        
        # Verify MCP entry removed from config
        config = codex_home / ".codex/config.toml"
        if config.exists():
            content = config.read_text(encoding="utf-8")
            assert "[mcp_servers.voyager]" not in content
    
    def test_claude_full_lifecycle(self, claude_home):
        """Claude install → status → remove cycle."""
        # Install
        result1 = install_integration(provider="claude", home=claude_home)
        assert result1["status"] == "success"
        assert result1["skill"]["status"] == "installed"
        
        # Check status
        status = check_integration_status(providers=["claude"], home=claude_home)[0]
        assert status["skill"]["installed"] is True
        
        # Remove
        remove_result = uninstall_integration(provider="claude", home=claude_home)
        assert remove_result["status"] == "removed"
        assert remove_result["skill"]["status"] == "removed"


class TestCrossPlatformExecutableDiscovery:
    """Test shutil.which vs which command."""
    
    def test_uses_shutil_which(self, monkeypatch):
        """Find executable uses platform-independent shutil.which."""
        from voyager.skill import _find_executable
        
        # Python itself should always be found
        result = _find_executable("python")
        assert result is not None
        assert result.exists()
        
        # Non-existent exe should return None (not crash)
        result = _find_executable("this-definitely-does-not-exist-xyz123")
        assert result is None
    
    def test_platform_independent(self, monkeypatch):
        """Works on Windows (no which command)."""
        from voyager.skill import _find_executable
        
        # On Windows, 'which' command doesn't exist
        # But shutil.which works on all platforms
        result = _find_executable("python")
        assert result is not None


class TestRemovePreservesSharedConfig:
    """Remove only deletes Voyager entries, preserves user's other configs."""
    
    def test_codex_preserves_other_servers(self, codex_home):
        """Remove deletes only Voyager, keeps other MCP servers."""
        # Start with multiple servers
        config = codex_home / ".codex/config.toml"
        original = """# Multiple MCP servers
[some_other_server]
command = "custom"

[mcp_servers.google]
command = "google-mcp"

[mcp_servers.another]
command = "another"
"""
        config.write_text(original, encoding="utf-8")
        
        # Add Voyager
        _register_codex_mcp(codex_home)
        content = config.read_text(encoding="utf-8")
        assert "[mcp_servers.voyager]" in content
        
        # Remove Voyager
        uninstall_integration(provider="codex", home=codex_home)
        
        # Verify Voyager removed, others preserved
        content = config.read_text(encoding="utf-8")
        assert "[mcp_servers.voyager]" not in content
        assert "[some_other_server]" in content
        assert "[mcp_servers.google]" in content
        assert "[mcp_servers.another]" in content
    
    def test_claude_preserves_other_servers(self, claude_home):
        """Remove only Voyager entry, keeps all other MCP servers."""
        # Start with multiple servers
        mcp_file = claude_home / ".claude/mcp.json"
        config = {
            "MCP_SERVERS": [
                "python -m some.server",
                "npx another-mcp",
                "go run ./third-server"
            ]
        }
        mcp_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
        
        # Add Voyager
        _register_claude_mcp(claude_home)
        updated = json.loads(mcp_file.read_text(encoding="utf-8"))
        assert len(updated["MCP_SERVERS"]) >= 4  # 3 original + voyager
        
        # Remove Voyager
        uninstall_integration(provider="claude", home=claude_home)
        
        # Verify Voyager removed, others preserved
        final = json.loads(mcp_file.read_text(encoding="utf-8"))
        assert "python -m voyager.mcp_server" not in final["MCP_SERVERS"]
        assert "python -m some.server" in final["MCP_SERVERS"]
        assert "npx another-mcp" in final["MCP_SERVERS"]
        assert "go run ./third-server" in final["MCP_SERVERS"]


class TestMalformedConfigHandling:
    """Malformed config files should be handled safely."""
    
    def test_codex_malformed_toml_refused_safely(self, tmp_path, monkeypatch):
        """Corrupt TOML file shouldn't cause crashes."""
        h = tmp_path / "home"
        codex_dir = h / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        
        # Write invalid TOML
        config = codex_dir / "config.toml"
        config.write_text("{ invalid toml {{{{", encoding="utf-8")
        
        monkeypatch.setenv("USERPROFILE", str(h))
        monkeypatch.setenv("HOME", str(h))
        
        # Should handle gracefully
        result = _register_codex_mcp(h)
        # Will try to parse existing, fall back to append mode
        assert result[0] in ("registered", "up-to-date", "error")
    
    def test_claude_malformed_json_refused_safely(self, tmp_path, monkeypatch):
        """Corrupt JSON file shouldn't cause crashes."""
        h = tmp_path / "home"
        claude_dir = h / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        
        # Write invalid JSON
        mcp_file = claude_dir / "mcp.json"
        mcp_file.write_text("{ bad json ", encoding="utf-8")
        
        monkeypatch.setenv("USERPROFILE", str(h))
        monkeypatch.setenv("HOME", str(h))
        
        # Should handle gracefully
        result = _register_claude_mcp(h)
        # Will skip corrupted file and write new one
        assert result[0] in ("registered", "up-to-date", "error")


class TestUserModifiedSkillProtection:
    """User-modified skill files should be protected from overwrite."""
    
    def test_user_modified_skill_refused_without_force(self, codex_home):
        """Refuse to overwrite modified skill without --force."""
        # Install first time
        install_integration(provider="codex", home=codex_home)
        
        # User modifies skill
        skill_file = codex_home / ".codex/skills/voyager/SKILL.md"
        original = skill_file.read_text(encoding="utf-8")
        skill_file.write_text(original + "\n\n# USER CUSTOMIZATION", encoding="utf-8")
        
        # Second install should refuse
        result = install_integration(provider="codex", home=codex_home)
        assert result["skill"]["status"] == "refused"
        
        # File should be unchanged
        assert "# USER CUSTOMIZATION" in skill_file.read_text(encoding="utf-8")
    
    def test_user_modified_skill_overwritten_with_force(self, codex_home):
        """--force backs up then overwrites modified skill."""
        # Install first time
        install_integration(provider="codex", home=codex_home)
        
        # User modifies skill
        skill_file = codex_home / ".codex/skills/voyager/SKILL.md"
        original = skill_file.read_text(encoding="utf-8")
        skill_file.write_text("CUSTOMIZED BY USER", encoding="utf-8")
        
        # Force install should backup and overwrite
        result = install_integration(provider="codex", force=True, home=codex_home)
        assert result["status"] == "success"
        
        # Backup should exist
        backups = list((skill_file.parent).glob("SKILL.md.bak-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "CUSTOMIZED BY USER"
        
        # Original skill restored
        assert skill_file.read_text(encoding="utf-8") == original