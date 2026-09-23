"""DSH integration implementation."""
from pathlib import Path
from typing import Any, Dict, Optional
import shutil
from .capabilities import ProviderCapabilities, ZeroTouchLevel


class DSHIntegration:
    """DSH launcher + session watcher integration.
    
    Strategy: LAUNCHER_ZERO_TOUCH with session file polling fallback.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self._capabilities: Optional[ProviderCapabilities] = None
        self.voyager_bin = self.home / ".voyager/bin"
        self.session_dir = self.home / ".dsh/sessions"
    
    def install(self) -> Dict[str, Any]:
        """Install DSH launcher wrapper and watcher setup."""
        real_dsh = shutil.which("dsh")
        if not real_dsh:
            return {
                "provider": "dsh",
                "status": "error",
                "message": "DSH executable not found in PATH",
            }
        
        # Create launcher directory
        self.voyager_bin.mkdir(parents=True, exist_ok=True)
        
        # Generate wrapper script
        wrapper_script = self.voyager_bin / "dsh"
        script_content = f"""#!/bin/sh
# Voyager launcher for DSH
# Opt-in wrapper that provides continuity on launch

real_executable="{real_dsh}"

# Prevent recursion
if [ -n "$VOYAGER_LAUNCHER_RUNNING" ]; then
    exec "$real_executable" "$@"
fi

export VOYAGER_LAUNCHER_RUNNING=1

# Run prelaunch hook
voyager launcher prelaunch --provider dsh --cwd "$PWD" || true

# Launch real dsh with original arguments
exec "$real_executable" "$@"
"""
        wrapper_script.write_text(script_content, encoding="utf-8")
        wrapper_script.chmod(0o755)
        
        return {
            "provider": "dsh",
            "status": "installed",
            "launcher": str(wrapper_script),
            "real_executable": real_dsh,
            "strategy": "LAUNCHER_ZERO_TOUCH",
            "notes": [
                "Launcher wrapper created at ~/.voyager/bin/dsh",
                "Session watcher can monitor ~/.dsh/sessions/",
                "Add ~/.voyager/bin to PATH prefix for automatic use",
            ],
        }
    
    def remove(self) -> Dict[str, Any]:
        """Remove DSH launcher artifacts."""
        wrapper_script = self.voyager_bin / "dsh"
        try:
            if wrapper_script.exists():
                wrapper_script.unlink()
            return {"provider": "dsh", "status": "removed"}
        except OSError:
            return {"provider": "dsh", "status": "error"}
    
    def capabilities(self) -> ProviderCapabilities:
        """Return capability profile."""
        from .capabilities import detect_capabilities
        if self._capabilities is None:
            self._capabilities = detect_capabilities("dsh", self.home)
        return self._capabilities
    
    def verify(self) -> Dict[str, Any]:
        """Verify launcher is correctly installed."""
        launcher = self.voyager_bin / "dsh"
        
        checks = {
            "launcher_exists": launcher.exists(),
            "session_dir_accessible": self.session_dir.is_dir(),
        }
        
        all_ok = all(checks.values())
        return {
            "verified": all_ok,
            "checks": checks,
            "strategy": "LAUNCHER_ZERO_TOUCH" if all_ok else "ERROR",
        }
