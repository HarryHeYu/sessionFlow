"""Grok CLI integration implementation."""
import os
from pathlib import Path
from typing import Any, Dict, Optional
import shutil
from .capabilities import ProviderCapabilities, ZeroTouchLevel


class GrokIntegration:
    """Grok CLI launcher integration.
    
    Strategy: LAUNCHER_ZERO_TOUCH via opt-in wrapper script.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self._capabilities: Optional[ProviderCapabilities] = None
        self.voyager_bin = self.home / ".voyager/bin"
    
    def install(self) -> Dict[str, Any]:
        """Install Grok launcher wrapper.
        
        Creates:
        - ~/.voyager/bin/grok (wrapper script)
        - Installs at PATH prefix if possible (opt-in)
        """
        real_grok = shutil.which("grok")
        if not real_grok:
            return {
                "provider": "grok",
                "status": "error",
                "message": "Grok executable not found in PATH",
            }
        
        # Create launcher directory
        self.voyager_bin.mkdir(parents=True, exist_ok=True)
        
        # Generate wrapper script
        wrapper_script = self.voyager_bin / "grok"
        script_content = f"""#!/bin/sh
# Voyager launcher for Grok CLI
# Opt-in wrapper that provides continuity on launch

real_executable="{real_grok}"

# Prevent recursion
if [ -n "$VOYAGER_LAUNCHER_RUNNING" ]; then
    exec "$real_executable" "$@"
fi

export VOYAGER_LAUNCHER_RUNNING=1

# Run prelaunch hook
voyager launcher prelaunch --provider grok --cwd "$PWD" || true

# Launch real grok with original arguments
exec "$real_executable" "$@"
"""
        wrapper_script.write_text(script_content, encoding="utf-8")
        wrapper_script.chmod(0o755)
        
        return {
            "provider": "grok",
            "status": "installed",
            "launcher": str(wrapper_script),
            "real_executable": real_grok,
            "strategy": "LAUNCHER_ZERO_TOUCH",
            "notes": [
                "Opt-in launcher wrapper created",
                "Add ~/.voyager/bin to PATH prefix for automatic use",
                "Wrapper prevents recursion and runs voyager prelaunch",
            ],
        }
    
    def remove(self) -> Dict[str, Any]:
        """Remove Grok launcher artifacts."""
        wrapper_script = self.voyager_bin / "grok"
        try:
            if wrapper_script.exists():
                wrapper_script.unlink()
            return {"provider": "grok", "status": "removed"}
        except OSError:
            return {"provider": "grok", "status": "error"}
    
    def capabilities(self) -> ProviderCapabilities:
        """Return capability profile."""
        from .capabilities import detect_capabilities
        if self._capabilities is None:
            self._capabilities = detect_capabilities("grok", self.home)
        return self._capabilities
    
    def verify(self) -> Dict[str, Any]:
        """Verify launcher is correctly installed.

        Two traps this avoids, both of which made the method unusable:

        * ``stat()`` must not run unconditionally.  It raises
          ``FileNotFoundError`` when the launcher is missing — which is
          precisely the situation ``verify()`` exists to report, so the method
          crashed exactly when it was needed.
        * The execute bit is a POSIX notion.  On Windows ``chmod(0o755)``
          leaves ``st_mode`` at ``0o100666``, so testing ``st_mode & 0o111``
          reported a healthy install as broken and ``verified`` could never be
          true there.  The bit is only *required* where it means something.
        """
        launcher = self.voyager_bin / "grok"
        exists = launcher.exists()
        executable = bool(launcher.stat().st_mode & 0o111) if exists else False

        checks = {
            "launcher_exists": exists,
            "executable": executable,
        }
        required = ["launcher_exists"] + (["executable"] if os.name == "posix" else [])

        all_ok = all(checks[k] for k in required)
        return {
            "verified": all_ok,
            "checks": checks,
            "strategy": "LAUNCHER_ZERO_TOUCH" if all_ok else "ERROR",
        }
