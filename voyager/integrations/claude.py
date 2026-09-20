"""Claude Code integration - SessionStart zero-touch implementation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import shutil
import subprocess
from .capabilities import ProviderCapabilities, ZeroTouchLevel


class ClaudeIntegration:
    """Claude Code SessionStart lifecycle integration.
    
    Strategy: SESSION_START_ZERO_TOUCH via native hooks configuration.
    
    Hook installation adds a session-start hook that invokes:
      voyager hook startup --provider claude --cwd "$WORKSPACE"
    
    The hook stdout is injected into Claude's context automatically.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities: Optional[ProviderCapabilities] = None
        self.settings_file = self.home / ".claude/settings.json"
        self.local_settings = self.home / ".claude/settings.local.json"
        self.voyager_hook_script = self.home / ".claude/voyager_session_start.sh"
    
    def _install_voyager_hook_script(self) -> Path:
        """Create the Voyager SessionStart hook wrapper script."""
        # Determine Claude executable path
        claude_exe = shutil.which("claude-code") or shutil.which("claude")
        
        script_content = f'''#!/bin/bash
# Voyager SessionStart hook for Claude Code
# This script runs at session start and injects continuation context

set -o pipefail

# Prevent recursion
if [ -n "${{VOYAGER_CLAUDE_HOOK_RUNNING}}" ]; then
    exit 0
fi

export VOYAGER_CLAUDE_HOOK_RUNNING=1
export VOYAGER_HOOK_CWD="${{PWD:-$HOME}}"

# Run Voyager hook and capture output
CONTEXT=$(voyager hook startup \\
  --provider claude \\
  --cwd "$VOYAGER_HOOK_CWD" \\
  2>/dev/null || echo "[Voyager Continuity] No active WorkThread found.")

# Check if we got meaningful context
if [[ -n "$CONTEXT" && "$CONTEXT" != *"No active WorkThread"* ]]; then
    # Output context to stdout - Claude will inject this
    printf "\\n\\n=== Voyager Continuation ===\\n%s\\n\\n=== End Continuation ===\\n\\n" "$CONTEXT"
fi

exit 0
'''
        
        self.voyager_hook_script.parent.mkdir(parents=True, exist_ok=True)
        self.voyager_hook_script.write_text(script_content, encoding="utf-8")
        self.voyager_hook_script.chmod(0o755)
        
        return self.voyager_hook_script
    
    def install(self) -> Dict[str, Any]:
        """Install Claude SessionStart hook integration.
        
        Adds Voyager hook to settings.json (additive merge).
        Preserves existing hooks; only adds/modifies Voyager entry.
        """
        result = {
            "provider": "claude",
            "status": "installed",
            "strategy": "SESSION_START_ZERO_TOUCH",
        }
        
        try:
            # Step 1: Create hook script
            hook_script = self._install_voyager_hook_script()
            result["hook_script"] = str(hook_script)
            
            # Step 2: Load existing settings or create new
            settings = {}
            if self.settings_file.exists():
                try:
                    settings = json.loads(self.settings_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    # Malformed config - backup and refuse
                    backup = self.home / f".claude/settings.json.backup.{int(__import__('time').time())}"
                    try:
                        import shutil as sh
                        sh.copy2(self.settings_file, backup)
                        return {
                            "provider": "claude",
                            "status": "error",
                            "message": f"Malformed settings.json backed up to {backup}",
                            "strategy": "FAILED_MALFORMED_CONFIG",
                        }
                    except OSError:
                        pass
            
            # Step 3: Add/merge hooks section
            if "hooks" not in settings:
                settings["hooks"] = {}
            
            # Step 4: Remove old Voyager entry if present (idempotent update)
            if "SessionStart" in settings["hooks"]:
                session_hooks = settings["hooks"]["SessionStart"]
                if isinstance(session_hooks, list):
                    settings["hooks"]["SessionStart"] = [
                        h for h in session_hooks 
                        if not (isinstance(h, dict) and h.get("command", "").find("voyager") >= 0)
                    ]
            
            # Step 5: Add Voyager SessionStart hook
            voyager_entry = {
                "name": "voyager-session-start",
                "description": "Automatically attach to WorkThread using Voyager",
                "event": "sessionStart",
                "command": str(hook_script),
                "priority": 100,  # High priority to run early
                "timeout_ms": 30000,  # 30s timeout
                "stdout_injection": True,  # Enable context injection
            }
            
            # Ensure SessionStart list exists
            if "SessionStart" not in settings["hooks"]:
                settings["hooks"]["SessionStart"] = []
            
            if not isinstance(settings["hooks"]["SessionStart"], list):
                settings["hooks"]["SessionStart"] = [settings["hooks"]["SessionStart"]]
            
            settings["hooks"]["SessionStart"].append(voyager_entry)
            
            # Step 6: Write back to settings file
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings_file.write_text(
                json.dumps(settings, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            
            result["settings_file"] = str(self.settings_file)
            result["hooks_added"] = 1
            result["warnings"] = [
                "SessionStart hook installed successfully",
                "Restart Claude Code for changes to take effect",
                "Test by starting Claude without mentioning 'Voyager'",
            ]
            
            return result
            
        except Exception as e:
            return {
                "provider": "claude",
                "status": "error",
                "message": str(e),
                "strategy": "INSTALLATION_FAILED",
            }
    
    def remove(self) -> Dict[str, Any]:
        """Remove Claude hook artifacts safely."""
        result = {"provider": "claude", "status": "removed"}
        
        try:
            # Remove hook script
            if self.voyager_hook_script.exists():
                self.voyager_hook_script.unlink()
            
            # Remove from settings
            if self.settings_file.exists():
                try:
                    settings = json.loads(self.settings_file.read_text(encoding="utf-8"))
                    if "hooks" in settings and "SessionStart" in settings["hooks"]:
                        hooks_list = settings["hooks"]["SessionStart"]
                        if isinstance(hooks_list, list):
                            original_len = len(hooks_list)
                            settings["hooks"]["SessionStart"] = [
                                h for h in hooks_list
                                if not (isinstance(h, dict) and h.get("name") == "voyager-session-start")
                            ]
                            if len(settings["hooks"]["SessionStart"]) < original_len:
                                # Only write if we removed something
                                self.settings_file.write_text(
                                    json.dumps(settings, indent=2, ensure_ascii=False),
                                    encoding="utf-8"
                                )
                except (json.JSONDecodeError, OSError):
                    pass
            
            return result
            
        except OSError as e:
            result["status"] = "error"
            result["message"] = str(e)
            return result
    
    def capabilities(self) -> ProviderCapabilities:
        """Return capability profile."""
        from .capabilities import detect_capabilities
        if self.capabilities is None:
            self.capabilities = detect_capabilities("claude", self.home)
        return self.capabilities
    
    def verify(self) -> Dict[str, Any]:
        """Verify SessionStart hook is correctly installed."""
        import sys
        
        checks = {
            "settings_file_exists": self.settings_file.exists(),
            "hook_script_exists": self.voyager_hook_script.exists(),
        }
        
        # Check executable only on POSIX systems; Windows doesn't use execute bit
        if sys.platform != "win32" and self.voyager_hook_script.exists():
            checks["hook_script_executable"] = self.voyager_hook_script.stat().st_mode & 0o111 != 0
        else:
            # On Windows, just check that script file exists and has content
            checks["hook_script_executable"] = (
                self.voyager_hook_script.exists() 
                and self.voyager_hook_script.read_text(encoding="utf-8").strip()
            )
        
        # Check if Voyager entry exists in settings
        checks["voyager_entry_present"] = False
        if checks["settings_file_exists"]:
            try:
                settings = json.loads(self.settings_file.read_text(encoding="utf-8"))
                if "hooks" in settings and "SessionStart" in settings["hooks"]:
                    hooks = settings["hooks"]["SessionStart"]
                    if isinstance(hooks, list):
                        checks["voyager_entry_present"] = any(
                            h.get("name") == "voyager-session-start" 
                            for h in hooks
                        )
            except (json.JSONDecodeError, OSError):
                pass
        
        all_ok = all(checks.values())
        return {
            "verified": all_ok,
            "checks": checks,
            "strategy": "SESSION_START_ZERO_TOUCH" if all_ok else "INSTALLED_INCOMPLETE",
            "next_steps": [
                "Restart Claude Code",
                "Start a session WITHOUT typing 'Voyager'",
                "Observe if context appears automatically",
            ] if all_ok else ["Fix installation issues first"],
        }
