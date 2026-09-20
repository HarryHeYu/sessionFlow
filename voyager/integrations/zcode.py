"""ZCode integration - stub for WATCHER_ATTACH_ONLY strategy."""
from pathlib import Path
from typing import Any, Dict, Optional
from .capabilities import ProviderCapabilities, ZeroTouchLevel


class ZCodeIntegration:
    """ZCode watcher-based integration.
    
    Strategy: WATCHER_ATTACH_ONLY (no proven injection mechanism).
    BLOCKER: Need robust source discovery beyond hardcoded paths.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities: Optional[ProviderCapabilities] = None
    
    def install(self) -> Dict[str, Any]:
        return {
            "provider": "zcode",
            "status": "installed",
            "strategy": "WATCHER_ATTACH_ONLY",
            "notes": ["Process + SQLite watcher pending"],
            "blockers": ["Source discovery needs environment variable fallback"],
        }
    
    def remove(self) -> Dict[str, Any]:
        return {"provider": "zcode", "status": "removed"}
    
    def capabilities(self) -> ProviderCapabilities:
        from .capabilities import detect_capabilities
        if self.capabilities is None:
            self.capabilities = detect_capabilities("zcode", self.home)
        return self.capabilities
    
    def verify(self) -> Dict[str, Any]:
        return {"verified": False, "checks": {}, "strategy": "WATCHER_ATTACH_ONLY"}
