"""Kiro integration - IDE vs CLI separate strategies."""
from pathlib import Path
from typing import Any, Dict, Optional
from .capabilities import ProviderCapabilities, ZeroTouchLevel


class KiroIntegration:
    """Kiro integration (IDE and CLI split).
    
    Strategies:
      - CLI: LAUNCHER_ZERO_TOUCH
      - IDE: WATCHER_ATTACH_ONLY
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities: Optional[ProviderCapabilities] = None
    
    def install(self) -> Dict[str, Any]:
        return {
            "provider": "kiro",
            "status": "installed",
            "strategy": "LAUNCHER_ZERO_TOUCH/IDE_WATCHER",
            "notes": [
                "CLI launcher strategy available",
                "IDE uses process + JSON file watcher",
                "Different capabilities per deployment mode",
            ],
        }
    
    def remove(self) -> Dict[str, Any]:
        return {"provider": "kiro", "status": "removed"}
    
    def capabilities(self) -> ProviderCapabilities:
        from .capabilities import detect_capabilities
        if self.capabilities is None:
            self.capabilities = detect_capabilities("kiro", self.home)
        return self.capabilities
    
    def verify(self) -> Dict[str, Any]:
        return {"verified": False, "checks": {}, "strategy": "PENDING_VERIFICATION"}
