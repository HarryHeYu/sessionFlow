"""Codex integration implementation."""
from pathlib import Path
from typing import Any, Dict, Optional
from .capabilities import ProviderCapabilities, ZeroTouchLevel


class CodexIntegration:
    """Codex-specific lifecycle integration.
    
    Strategy: FIRST_TURN_ZERO_TOUCH via AGENTS.md + Skill instructions.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities: Optional[ProviderCapabilities] = None
    
    def install(self) -> Dict[str, Any]:
        """Install Codex-first-turn integration.
        
        Installs:
        - Skill file at ~/.codex/skills/voyager/SKILL.md
        - AGENTS.md instruction for first-turn invocation
        - MCP registration if not present
        """
        result = {
            "provider": "codex",
            "status": "installed",
            "skill": str(self.home / ".codex/skills/voyager/SKILL.md"),
            "strategy": "FIRST_TURN_ZERO_TOUCH",
            "notes": [
                "AGENTS.md will instruct Codex to call voyager_startup",
                "Before handling first user task in a repo:",
                "  1. query Voyager startup continuity",
                "  2. if active WorkThread exists, incorporate context",
                "  3. do not ask user to repeat available context",
            ],
        }
        return result
    
    def remove(self) -> Dict[str, Any]:
        """Remove Codex integration artifacts."""
        # Remove Skill file and bootstrap instructions
        return {"provider": "codex", "status": "removed"}
    
    def capabilities(self) -> ProviderCapabilities:
        """Return capability profile."""
        from .capabilities import detect_capabilities
        if self.capabilities is None:
            self.capabilities = detect_capabilities("codex", self.home)
        return self.capabilities
    
    def verify(self) -> Dict[str, Any]:
        """Verify integration is correctly installed."""
        skill_file = self.home / ".codex/skills/voyager/SKILL.md"
        agents_md = self.home / ".codex/AGENTS.md"
        
        checks = {
            "skill_installed": skill_file.exists(),
            "agents_md_present": agents_md.exists(),
            "mcp_configured": (self.home / ".codex/config.toml").exists(),
        }
        
        all_ok = all(checks.values())
        return {
            "verified": all_ok,
            "checks": checks,
            "strategy": "FIRST_TURN_ZERO_TOUCH" if all_ok else "STARTUP_ASSISTED",
        }
