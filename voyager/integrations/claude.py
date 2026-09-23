"""Claude Code integration - native SessionStart hook.

Claude Code reads hooks from `~/.claude/settings.json` (and the project-local
`.claude/settings.json` / `.claude/settings.local.json`).  The schema is:

    {
      "hooks": {
        "SessionStart": [
          {
            "matcher": "startup",
            "hooks": [
              {"type": "command", "command": "<shell command>", "timeout": 120}
            ]
          }
        ]
      }
    }

`matcher` selects the session-start source (`startup` / `resume` / `clear` /
`compact`); an empty string matches every source.  `timeout` is in **seconds**.
The command's stdout is parsed as JSON and `hookSpecificOutput.additionalContext`
is injected into the model's first turn.

This module writes that structure.  It deliberately does **not** use the older
flat shape (`{"name": ..., "event": "sessionStart", "command": ...}`) that an
earlier revision of this file emitted: Claude Code ignores unknown keys, so that
config was silently inert.

The installed command is an absolute interpreter plus an absolute path to
`claude_session_start.py` — the same entrypoint the verifier exercises.  An
absolute path means the hook does not depend on the user-site `.pth` resolving,
and the entrypoint bootstraps its own `sys.path`, so it works from any cwd.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .capabilities import ProviderCapabilities, ZeroTouchLevel

# Substring that identifies our own hook command.  Used for idempotent
# replace-on-install and for selective removal, so it must be specific enough
# not to match an unrelated user hook.
ENTRYPOINT_BASENAME = "claude_session_start.py"
LEGACY_WRAPPER_BASENAME = "voyager_session_start.sh"
LEGACY_ENTRY_NAME = "voyager-session-start"

#: Session-start source to match.  `startup` fires when a new session begins,
#: which is the case this integration targets.
DEFAULT_MATCHER = "startup"

#: Claude Code hook timeouts are seconds.  120 s covers a cold compile on a
#: large index (a warm one is sub-second).
DEFAULT_TIMEOUT_S = 120


class ClaudeIntegration:
    """Claude Code native SessionStart hook integration."""

    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self.capabilities: Optional[ProviderCapabilities] = None
        self.settings_file = self.home / ".claude/settings.json"
        self.local_settings = self.home / ".claude/settings.local.json"
        #: Entrypoint the hook command points at.
        self.entrypoint = Path(__file__).with_name(ENTRYPOINT_BASENAME)
        #: Written by revisions <= 2026-09-20; still removed for cleanliness.
        self.legacy_wrapper = self.home / ".claude" / LEGACY_WRAPPER_BASENAME

    # -- command construction -------------------------------------------------

    def hook_command(self, interpreter: Optional[str] = None) -> str:
        """Absolute interpreter + absolute entrypoint, quoted for the shell.

        Claude Code runs the command through the system shell, so both paths
        are quoted: a Windows interpreter path routinely contains spaces
        (`C:\\Program Files\\...`) or is run from a user profile directory.
        """
        interp = interpreter or sys.executable or "python"
        return f'"{interp}" "{self.entrypoint}"'

    # -- settings helpers -----------------------------------------------------

    @staticmethod
    def _is_voyager_hook(entry: Any) -> bool:
        """True if a `SessionStart` list entry belongs to Voyager.

        Recognises both the current nested shape and the legacy flat shape,
        so `install()` upgrades an old config in place and `remove()` cleans
        it up.
        """
        if not isinstance(entry, dict):
            return False
        # Legacy flat shape written by earlier revisions.
        if entry.get("name") == LEGACY_ENTRY_NAME:
            return True
        flat_cmd = entry.get("command")
        if isinstance(flat_cmd, str) and (
            ENTRYPOINT_BASENAME in flat_cmd or LEGACY_WRAPPER_BASENAME in flat_cmd
        ):
            return True
        # Current nested shape.
        inner = entry.get("hooks")
        if isinstance(inner, list):
            for h in inner:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command")
                if isinstance(cmd, str) and (
                    ENTRYPOINT_BASENAME in cmd or LEGACY_WRAPPER_BASENAME in cmd
                ):
                    return True
        return False

    def _load_settings(self) -> Dict[str, Any]:
        if not self.settings_file.exists():
            return {}
        return json.loads(self.settings_file.read_text(encoding="utf-8"))

    def _backup_settings(self) -> Optional[Path]:
        """Copy the settings file aside before rewriting it."""
        if not self.settings_file.exists():
            return None
        backup = self.settings_file.with_name(
            f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            shutil.copy2(self.settings_file, backup)
            return backup
        except OSError:
            return None

    def _write_settings(self, settings: Dict[str, Any]) -> Optional[Path]:
        backup = self._backup_settings()
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return backup

    def _session_start_list(self, settings: Dict[str, Any]) -> List[Any]:
        """Return the `hooks.SessionStart` list, normalising odd shapes.

        A user may have written a single object instead of a list; Claude Code
        tolerates that, and so do we, by wrapping it.
        """
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            hooks = {}
            settings["hooks"] = hooks
        existing = hooks.get("SessionStart")
        if existing is None:
            return []
        if isinstance(existing, list):
            return existing
        return [existing]

    # -- lifecycle ------------------------------------------------------------

    def install(self, interpreter: Optional[str] = None) -> Dict[str, Any]:
        """Install (or upgrade) the native SessionStart hook.

        Additive and idempotent: user hooks are preserved, a previous Voyager
        entry (either shape) is replaced rather than duplicated, and the
        settings file is backed up before it is rewritten.
        """
        try:
            settings = self._load_settings()
        except (json.JSONDecodeError, OSError) as e:
            backup = self._backup_settings()
            return {
                "provider": "claude",
                "status": "error",
                "message": (
                    f"malformed {self.settings_file} ({e}); "
                    + (f"backed up to {backup}" if backup else "no backup written")
                ),
                "strategy": "FAILED_MALFORMED_CONFIG",
            }

        if not self.entrypoint.exists():
            return {
                "provider": "claude",
                "status": "error",
                "message": f"hook entrypoint not found: {self.entrypoint}",
                "strategy": "FAILED_MISSING_ENTRYPOINT",
            }

        command = self.hook_command(interpreter)
        entries = self._session_start_list(settings)
        kept = [e for e in entries if not self._is_voyager_hook(e)]
        removed = len(entries) - len(kept)

        kept.append({
            "matcher": DEFAULT_MATCHER,
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": DEFAULT_TIMEOUT_S,
            }],
        })
        settings["hooks"]["SessionStart"] = kept

        backup = self._write_settings(settings)

        # The pre-2026-09-21 wrapper is now unreferenced; drop it so it cannot
        # be mistaken for the live hook.
        legacy_removed = False
        if self.legacy_wrapper.exists():
            try:
                self.legacy_wrapper.unlink()
                legacy_removed = True
            except OSError:
                pass

        result: Dict[str, Any] = {
            "provider": "claude",
            "status": "installed",
            "strategy": "NATIVE_SESSIONSTART_HOOK",
            "settings_file": str(self.settings_file),
            "entrypoint": str(self.entrypoint),
            "command": command,
            "matcher": DEFAULT_MATCHER,
            "hooks_replaced": removed,
            "backup": str(backup) if backup else None,
            "legacy_wrapper_removed": legacy_removed,
            "warnings": [
                "Restart Claude Code for the new hook to be loaded.",
                "Verify with: claude --debug hooks --init-only",
            ],
        }
        return result

    def remove(self) -> Dict[str, Any]:
        """Remove Voyager's hook, leaving every other hook untouched."""
        result: Dict[str, Any] = {
            "provider": "claude",
            "status": "removed",
            "removed": 0,
        }
        try:
            if self.settings_file.exists():
                try:
                    settings = self._load_settings()
                except (json.JSONDecodeError, OSError) as e:
                    result["status"] = "error"
                    result["message"] = f"cannot parse {self.settings_file}: {e}"
                    return result

                hooks = settings.get("hooks")
                if isinstance(hooks, dict) and "SessionStart" in hooks:
                    entries = self._session_start_list(settings)
                    kept = [e for e in entries if not self._is_voyager_hook(e)]
                    result["removed"] = len(entries) - len(kept)
                    if result["removed"]:
                        if kept:
                            hooks["SessionStart"] = kept
                        else:
                            hooks.pop("SessionStart", None)
                        if not hooks:
                            settings.pop("hooks", None)
                        result["backup"] = str(self._write_settings(settings) or "")

            if self.legacy_wrapper.exists():
                self.legacy_wrapper.unlink()
                result["legacy_wrapper_removed"] = True

            return result
        except OSError as e:
            result["status"] = "error"
            result["message"] = str(e)
            return result

    def verify(self) -> Dict[str, Any]:
        """Verify the hook is registered in the shape Claude Code reads."""
        checks: Dict[str, Any] = {
            "settings_file_exists": self.settings_file.exists(),
            "entrypoint_exists": self.entrypoint.exists(),
            "voyager_entry_present": False,
        }
        registered_command = None

        if checks["settings_file_exists"]:
            try:
                settings = self._load_settings()
                hooks = settings.get("hooks")
                if isinstance(hooks, dict):
                    for entry in self._session_start_list(settings):
                        if not self._is_voyager_hook(entry):
                            continue
                        # Must be the nested shape — a legacy flat entry is
                        # present but inert, so it does not count as verified.
                        inner = entry.get("hooks")
                        if not isinstance(inner, list):
                            continue
                        for h in inner:
                            if not isinstance(h, dict):
                                continue
                            cmd = h.get("command")
                            if isinstance(cmd, str) and ENTRYPOINT_BASENAME in cmd:
                                checks["voyager_entry_present"] = True
                                registered_command = cmd
            except (json.JSONDecodeError, OSError):
                pass

        checks["command_uses_entrypoint"] = registered_command is not None
        checks["legacy_wrapper_absent"] = not self.legacy_wrapper.exists()

        all_ok = all(checks.values())
        return {
            "verified": all_ok,
            "checks": checks,
            "command": registered_command,
            "strategy": "NATIVE_SESSIONSTART_HOOK" if all_ok else "INSTALLED_INCOMPLETE",
            "next_steps": [
                "Restart Claude Code",
                "Run: claude --debug hooks --init-only",
                "Expect: Found 1 hook matchers in settings",
            ] if all_ok else ["Fix the failed checks, then re-run verify()"],
        }

    def capabilities(self) -> ProviderCapabilities:
        from .capabilities import detect_capabilities
        if self.capabilities is None:
            self.capabilities = detect_capabilities("claude", self.home)
        return self.capabilities
