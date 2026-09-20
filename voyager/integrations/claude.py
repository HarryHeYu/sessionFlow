"""Claude Code integration implementation."""
from pathlib import Path
from typing import Any, Dict, Optional
from .capabilities import ProviderCapabilities, ZeroTouchLevel


class ClaudeIntegration:
    """Claude Code-specific lifecycle integration.
    
    Strategy: FIRST_TURN_ZERO_TOUCH via CLAUDE.md + Skill instructions.
    NOTE: No verified native session-start hook in current version.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities: Optional[ProviderCapabilities] = None
    
    def install(self) -> Dict[str, Any]:
        """Install Claude-first-turn integration.
        
        Installs:
        - Skill file at ~/.claude/skills/voyager/SKILL.md
        - CLAUDE.md instruction for first-turn invocation
        - MCP registration (~/.claude/mcp.json)
        """
        mcp_file = self.home / ".claude/mcp.json"
        
        result = {
            "provider": "claude",
            "status": "installed",
            "skill": str(self.home / ".claude/skills/voyager/SKILL.md"),
            "mcp": str(mcp_file),
            "strategy": "FIRST_TURN_ZERO_TOUCH",
            "notes": [
                "CLAUDE.md will instruct Claude to call voyager_startup",
                "Before handling first user task in a repo:",
                "  1. query Voyager startup continuity",
                "  2. if active WorkThread exists, incorporate context",
                "  3. do not ask user to repeat available context",
            ],
            "warnings": [
                "No verified native session-start hook in Claude Code",
                "Relies on instruction-following mechanism",
            ],
        }
        return result
    
    def remove(self) -> Dict[str, Any]:
        """Remove Claude integration artifacts."""
        return {"provider": "claude", "status": "removed"}
    
    def capabilities(self) -> ProviderCapabilities:
        """Return capability profile."""
        from .capabilities import detect_capabilities
        if self.capabilities is None:
            self.capabilities = detect_capabilities("claude", self.home)
        return self.capabilities
    
    def verify(self) -> Dict[str, Any]:
        """Verify integration is correctly installed."""
        skill_file = self.home / ".claude/skills/voyager/SKILL.md"
        claude_md = self.home / ".claude/CLAUDE.md"
        mcp_file = self.home / ".claude/mcp.json"
        
        checks = {
            "skill_installed": skill_file.exists(),
            "claude_md_present": claude_md.exists(),
            "mcp_configured": mcp_file.exists(),
        }
        
        all_ok = all(checks.values())
        return {
            "verified": all_ok,
            "checks": checks,
            "strategy": "FIRST_TURN_ZERO_TOUCH" if all_ok else "STARTUP_ASSISTED",
        }
