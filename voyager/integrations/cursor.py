"""Cursor integration - SESSION_START_ZERO_TOUCH pending verification."""
from pathlib import Path
from typing import Any, Dict, Optional
from .capabilities import ProviderCapabilities, ZeroTouchLevel


class CursorIntegration:
    """Cursor IDE extension integration.
    
    Strategy: SESSION_START_ZERO_TOUCH (pending real verification).
    NOTE: Cursor is IDE-only; needs extension API investigation.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities: Optional[ProviderCapabilities] = None
    
    def install(self) -> Dict[str, Any]:
        return {
            "provider": "cursor",
            "status": "installed",
            "strategy": "SESSION_START_ZERO_TOUCH",
            "notes": [
                "Extension API-based lifecycle events needed",
                "MCP support exists",
                "VERIFICATION NEEDED via Cursor extension docs",
            ],
        }
    
    def remove(self) -> Dict[str, Any]:
        return {"provider": "cursor", "status": "removed"}
    
    def capabilities(self) -> ProviderCapabilities:
        from .capabilities import detect_capabilities
        if self.capabilities is None:
            self.capabilities = detect_capabilities("cursor", self.home)
        return self.capabilities
    
    def verify(self) -> Dict[str, Any]:
        return {"verified": False, "checks": {}, "strategy": "PENDING_VERIFICATION"}
