"""Kiro IDE and CLI split integration."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional


class KiroIntegration:
    """Kiro session lifecycle integration (IDE and CLI split).
    
    Strategies:
      - IDE: Session Start hook → context injection
      - CLI: Agent Spawn → stdout continuation
    
    Separate capabilities and verification per mode.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities_ide: Optional[Any] = None
        self.capabilities_cli: Optional[Any] = None
        
        # IDE settings (if exists)
        self.ide_settings = self.home / ".kiro/settings.json"
        
        # Workspace-session JSON (watcher target)
        self.workspace_session = self.home / ".kiro/workspace-session.json"
        
        # CLI launcher wrapper
        self.cli_launcher = self.home / ".voyager/bin/kiro-cli"
    
    def _create_ide_hook_script(self) -> Path:
        """Create Kiro IDE SessionStart hook wrapper."""
        script_content = '''#!/bin/bash
# Kiro IDE SessionStart hook

set -o pipefail

if [ -n "${VOYAGER_KIRO_HOOK_RUNNING}" ]; then
    exit 0
fi

export VOYAGER_KIRO_HOOK_RUNNING=1
export VOYAGER_KIRO_CWD="${PWD:-$HOME}"

CONTEXT=$(voyager hook startup \\
  --provider kiro-ide \\
  --cwd "$VOYAGER_KIRO_CWD" \\
  2>/dev/null || echo "[Voyager Continuity] No active WorkThread found.")

if [[ -n "$CONTEXT" && "$CONTEXT" != *"No active WorkThread"* ]]; then
    printf "\\n\\n=== Voyager Continuation ===\\n%s\\n\\n=== End ===\\n\\n" "$CONTEXT"
fi

exit 0
'''
        self.ide_settings.parent.mkdir(parents=True, exist_ok=True)
        script = self.ide_settings.with_name(self.ide_settings.name + "-hook.sh")
        script.write_text(script_content, encoding="utf-8")
        try:
            script.chmod(0o755)
        except OSError:
            pass  # Windows doesn't use execute bit
        return script
    
    def _create_cli_wrapper(self) -> Path:
        """Create Kiro CLI launcher wrapper."""
        real_kiro = shutil.which("kiro")
        if not real_kiro:
            raise RuntimeError("Kiro CLI executable not found in PATH")
        
        self.cli_launcher.parent.mkdir(parents=True, exist_ok=True)
        
        script_content = f'''#!/bin/bash
# Kiro CLI launcher wrapper for Voyager continuity

set -o pipefail

if [ -n "${{VOYAGER_KIRO_CLI_RUNNING}}" ]; then
    exec "{real_kiro}" "$@"
fi

export VOYAGER_KIRO_CLI_RUNNING=1

# Run prelaunch
voyager launcher prelaunch --provider kiro-cli --cwd "$PWD" || true

exec "{real_kiro}" "$@"
'''
        self.cli_launcher.write_text(script_content, encoding="utf-8")
        self.cli_launcher.chmod(0o755)
        return self.cli_launcher
    
    def install_ide(self) -> Dict[str, Any]:
        """Install Kiro IDE SessionStart hook."""
        result = {"mode": "ide", "strategy": "SESSION_START_ZERO_TOUCH"}
        
        try:
            script = self._create_ide_hook_script()
            
            # Add to settings.json
            config = {}
            if self.ide_settings.exists():
                config = json.loads(self.ide_settings.read_text(encoding="utf-8"))
            
            if "hooks" not in config:
                config["hooks"] = {}
            
            if "sessionStart" not in config["hooks"]:
                config["hooks"]["sessionStart"] = []
            
            entry = {
                "name": "voyager-kiro-session-start",
                "event": "sessionStart",
                "command": str(script),
                "priority": 100,
            }
            
            hooks = config["hooks"]["sessionStart"]
            if isinstance(hooks, dict):
                hooks = [hooks]
                config["hooks"]["sessionStart"] = hooks
            
            config["hooks"]["sessionStart"].append(entry)
            
            self.ide_settings.write_text(json.dumps(config, indent=2), encoding="utf-8")
            
            result["status"] = "installed"
            result["hook_script"] = str(script)
            result["settings_file"] = str(self.ide_settings)
            
            return result
            
        except Exception as e:
            return {"mode": "ide", "strategy": "SESSION_START_ZERO_TOUCH", "status": "error", "message": str(e)}
    
    def install_cli(self) -> Dict[str, Any]:
        """Install Kiro CLI launcher wrapper."""
        result = {"mode": "cli", "strategy": "LAUNCHER_ZERO_TOUCH"}
        
        try:
            wrapper = self._create_cli_wrapper()
            
            result["status"] = "installed"
            result["launcher"] = str(wrapper)
            result["notes"] = [
                "Add ~/.voyager/bin to PATH prefix for automatic use",
                "Wrapper runs voyager prelaunch before launching kiro",
            ]
            
            return result
            
        except Exception as e:
            return {"mode": "cli", "strategy": "LAUNCHER_ZERO_TOUCH", "status": "error", "message": str(e)}
    
    def remove_ide(self) -> Dict[str, Any]:
        """Remove Kiro IDE hook."""
        result = {"mode": "ide", "status": "removed"}
        
        try:
            if self.ide_settings.exists():
                config = json.loads(self.ide_settings.read_text(encoding="utf-8"))
                if "hooks" in config and "sessionStart" in config["hooks"]:
                    hooks = config["hooks"]["sessionStart"]
                    if isinstance(hooks, list):
                        original = len(hooks)
                        config["hooks"]["sessionStart"] = [
                            h for h in hooks 
                            if not (isinstance(h, dict) and h.get("name") == "voyager-kiro-session-start")
                        ]
                        if len(config["hooks"]["sessionStart"]) < original:
                            self.ide_settings.write_text(
                                json.dumps(config, indent=2),
                                encoding="utf-8"
                            )
            
            # Clean up hook script
            hook_script = self.ide_settings.with_name(self.ide_settings.name + "-hook.sh")
            if hook_script.exists():
                hook_script.unlink()
            
            return result
            
        except OSError as e:
            result["status"] = "error"
            result["message"] = str(e)
            return result
    
    def remove_cli(self) -> Dict[str, Any]:
        """Remove Kiro CLI wrapper."""
        result = {"mode": "cli", "status": "removed"}
        
        try:
            if self.cli_launcher.exists():
                self.cli_launcher.unlink()
            return result
        except OSError as e:
            result["status"] = "error"
            result["message"] = str(e)
            return result
    
    def install(self) -> Dict[str, Any]:
        """Install both IDE and CLI modes."""
        ide_result = self.install_ide()
        cli_result = self.install_cli()
        
        return {
            "ide": ide_result,
            "cli": cli_result,
            "summary": f"IDE: {ide_result['strategy']}, CLI: {cli_result['strategy']}",
        }
    
    def remove(self) -> Dict[str, Any]:
        """Remove all Kiro integrations."""
        return {
            "ide": self.remove_ide(),
            "cli": self.remove_cli(),
        }
    
    def verify(self) -> Dict[str, Any]:
        """Verify both modes."""
        checks = {
            "ide_settings_exists": self.ide_settings.exists(),
            "ide_hook_present": False,
            "cli_launcher_exists": self.cli_launcher.exists(),
        }
        
        if checks["ide_settings_exists"]:
            try:
                config = json.loads(self.ide_settings.read_text(encoding="utf-8"))
                if "hooks" in config and "sessionStart" in config["hooks"]:
                    hooks = config["hooks"]["sessionStart"]
                    if isinstance(hooks, list):
                        checks["ide_hook_present"] = any(
                            h.get("name") == "voyager-kiro-session-start"
                            for h in hooks
                        )
            except (json.JSONDecodeError, OSError):
                pass
        
        return {
            "ide": {
                "verified": checks["ide_hook_present"],
                "strategy": "SESSION_START_ZERO_TOUCH" if checks["ide_hook_present"] else "INCOMPLETE",
            },
            "cli": {
                "verified": checks["cli_launcher_exists"],
                "strategy": "LAUNCHER_ZERO_TOUCH" if checks["cli_launcher_exists"] else "INCOMPLETE",
            },
            "checks": checks,
        }
