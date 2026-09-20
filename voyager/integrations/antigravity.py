"""Antigravity integration - BEST_EFFORT due to black-box storage."""
from pathlib import Path
from typing import Any, Dict, Optional
from .capabilities import ProviderCapabilities, ZeroTouchLevel


class AntigravityIntegration:
    """Antigravity watcher-based integration.
    
    Strategy: BEST_EFFORT (protobug storage format undocumented).
    NOTE: Adapter uses heuristic decode only.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities: Optional[ProviderCapabilities] = None
    
    def install(self) -> Dict[str, Any]:
        return {
            "provider": "antigravity",
            "status": "installed",
            "strategy": "BEST_EFFORT",
            "notes": [
                "Protobuf storage format undocumented",
                "SQLite watcher may work if schema stable",
                "Plugin API needs verification",
            ],
        }
    
    def remove(self) -> Dict[str, Any]:
        return {"provider": "antigravity", "status": "removed"}
    
    def capabilities(self) -> ProviderCapabilities:
        from .capabilities import detect_capabilities
        if self.capabilities is None:
            self.capabilities = detect_capabilities("antigravity", self.home)
        return self.capabilities
    
    def verify(self) -> Dict[str, Any]:
        return {"verified": False, "checks": {}, "strategy": "BEST_EFFORT"}
