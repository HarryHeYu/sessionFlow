"""Cursor IDE integration - sessionStart zero-touch implementation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


class CursorIntegration:
    """Cursor IDE sessionStart lifecycle integration.
    
    Strategy: SESSION_START_ZERO_TOUCH via official hooks system.
    
    Cursor's sessionStart hook can return additional_context which is
    automatically injected into the agent context.
    """
    
    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities: Optional[Any] = None
        # Cursor uses settings.json at ~/.cursor/ or project-level .cursor/
        self.global_settings = self.home / ".cursor/settings.json"
        self.project_settings = Path.cwd() / ".cursor/settings.json" if Path.cwd().exists() else None
    
    def install(self) -> Dict[str, Any]:
        """Install Cursor sessionStart hook.
        
        Adds hook configuration that invokes Voyager on session start.
        The hook returns JSON with additional_context field.
        """
        result = {
            "provider": "cursor",
            "status": "installed",
            "strategy": "SESSION_START_ZERO_TOUCH",
        }
        
        try:
            # Step 1: Determine target settings file (global preferred)
            target_file = self.global_settings
            if not target_file.exists():
                # Fall back to project-level
                if self.project_settings and self.project_settings.parent.exists():
                    target_file = self.project_settings
            
            # Step 2: Load existing config or create new
            config = {}
            if target_file.exists():
                try:
                    config = json.loads(target_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    backup = target_file.with_suffix(".json.backup")
                    try:
                        import shutil as sh
                        sh.copy2(target_file, backup)
                        return {
                            "provider": "cursor",
                            "status": "error",
                            "message": f"Malformed settings backed up to {backup}",
                            "strategy": "FAILED_MALFORMED_CONFIG",
                        }
                    except OSError:
                        pass
            
            # Step 3: Add hooks section if needed
            if "hooks" not in config:
                config["hooks"] = {}
            
            hooks = config["hooks"]
            
            # Step 4: Remove old Voyager entry if present
            if "sessionStart" in hooks:
                session_hooks = hooks["sessionStart"]
                if isinstance(session_hooks, list):
                    hooks["sessionStart"] = [
                        h for h in session_hooks
                        if not (isinstance(h, dict) and h.get("name") == "voyager-session-start")
                    ]
            
            # Step 5: Add Voyager sessionStart hook
            voyager_entry = {
                "name": "voyager-session-start",
                "description": "Automatically attach to WorkThread using Voyager MCP",
                "event": "sessionStart",
                "priority": 100,
                "command": "voyager hook startup --provider cursor --cwd '$WORKSPACE_DIRECTORY'",
                "returns": {
                    "type": "json",
                    "fields": ["additional_context"],
                },
                "timeout_ms": 15000,
                "on_error": "ignore",  # Don't block session if hook fails
            }
            
            # Ensure sessionStart list exists
            if "sessionStart" not in hooks:
                hooks["sessionStart"] = []
            
            if not isinstance(hooks["sessionStart"], list):
                hooks["sessionStart"] = [hooks["sessionStart"]]
            
            hooks["sessionStart"].append(voyager_entry)
            
            # Step 6: Write back
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            
            result["settings_file"] = str(target_file)
            result["hooks_added"] = 1
            result["hook_event"] = "sessionStart"
            result["response_format"] = {"additional_context": "<markdown continuation>"}
            result["warnings"] = [
                "SessionStart hook installed successfully",
                "Restart Cursor for changes to take effect",
                "Test by opening a new file without mentioning 'Voyager'",
            ]
            
            return result
            
        except Exception as e:
            return {
                "provider": "cursor",
                "status": "error",
                "message": str(e),
                "strategy": "INSTALLATION_FAILED",
            }
    
    def remove(self) -> Dict[str, Any]:
        """Remove Cursor hook artifacts."""
        result = {"provider": "cursor", "status": "removed"}
        
        files_to_check = [self.global_settings]
        if self.project_settings:
            files_to_check.append(self.project_settings)
        
        try:
            for target_file in files_to_check:
                if target_file.exists():
                    try:
                        config = json.loads(target_file.read_text(encoding="utf-8"))
                        if "hooks" in config and "sessionStart" in config["hooks"]:
                            hooks = config["hooks"]["sessionStart"]
                            if isinstance(hooks, list):
                                original_len = len(hooks)
                                config["hooks"]["sessionStart"] = [
                                    h for h in hooks
                                    if not (isinstance(h, dict) and h.get("name") == "voyager-session-start")
                                ]
                                if len(config["hooks"]["sessionStart"]) < original_len:
                                    target_file.write_text(
                                        json.dumps(config, indent=2, ensure_ascii=False),
                                        encoding="utf-8"
                                    )
                    except (json.JSONDecodeError, OSError):
                        pass
            
            return result
            
        except OSError as e:
            result["status"] = "error"
            result["message"] = str(e)
            return result
    
    def verify(self) -> Dict[str, Any]:
        """Verify sessionStart hook is correctly installed."""
        checks = {
            "global_settings_exists": self.global_settings.exists(),
            "project_settings_exists": bool(self.project_settings) and self.project_settings.exists() if self.project_settings else False,
            "voyager_entry_present": False,
        }
        
        # Check global first, then project
        for target_file in [self.global_settings, self.project_settings]:
            if target_file and target_file.exists():
                try:
                    config = json.loads(target_file.read_text(encoding="utf-8"))
                    if "hooks" in config and "sessionStart" in config["hooks"]:
                        hooks = config["hooks"]["sessionStart"]
                        if isinstance(hooks, list):
                            if any(h.get("name") == "voyager-session-start" for h in hooks):
                                checks["voyager_entry_present"] = True
                                checks["settings_file_used"] = str(target_file)
                                break
                except (json.JSONDecodeError, OSError):
                    pass
        
        all_ok = checks["voyager_entry_present"]
        return {
            "verified": all_ok,
            "checks": checks,
            "strategy": "SESSION_START_ZERO_TOUCH" if all_ok else "INSTALLED_INCOMPLETE",
            "next_steps": [
                "Restart Cursor IDE",
                "Open a new editor tab WITHOUT typing 'Voyager'",
                "Check if continuation appears in agent context",
            ] if all_ok else ["Fix installation issues first"],
        }
