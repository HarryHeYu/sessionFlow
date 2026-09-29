"""Codex integration: native SessionStart hook + tiered-v1 context (G3-B).

Codex 0.15x+ ships Claude-compatible lifecycle hooks.  A user-level
``~/.codex/hooks.json`` declares handlers per event; ``SessionStart`` fires
before the first turn; handler stdout ``hookSpecificOutput.additionalContext``
is materialized into the model context.  All of this was live-verified on
Windows TUI in G3-A (dispatch, trust, delivery, identity, model recitation).

Strategy: NATIVE_SESSIONSTART_HOOK with tiered-v1 context.

The pre-G3-B FIRST_TURN_ZERO_TOUCH strategy (AGENTS.md instructing the model
to call voyager_startup on the first turn) is retired: the hook already
delivers the context before the first turn, so first-turn MCP calls are
redundant startup-assisted traffic.  ``~/.codex/AGENTS.md`` keeps only
retrieval guidance, managed between markers so upgrades replace it cleanly.

Additive and reversible, mirroring the Claude integration: user hooks are
preserved, only Voyager-owned entries are replaced/removed, and every file is
backed up before it is rewritten.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .capabilities import ProviderCapabilities

ENTRYPOINT_BASENAME = "codex_session_start.py"
RELAY_BASENAME = "codex_hidden_relay.ps1"
DEFAULT_TIMEOUT_S = 30
MANAGED_BEGIN = "<!-- voyager:managed-block begin -->"
MANAGED_END = "<!-- voyager:managed-block end -->"
LEGACY_HEADING = "## 跨 Agent 会话记忆（voyager）"

MANAGED_BLOCK = MANAGED_BEGIN + "\n" + """## 跨 Agent 会话记忆（voyager）

本机的 voyager 已通过 SessionStart hook 自动注入当前 WorkThread 的任务上下文
（目标、最近真实对话与工作、仓库状态），无需主动查询即可继续既有任务。

**仅当需要更早历史时**才检索：
- `voyager search "关键词"` —— 全文检索（中英文均可）
- `voyager show <id>` —— 某个会话的完整时间线
- `voyager brief` —— 最近 48 小时全部 agent 摘要
- 若工具列表中有 voyager MCP 工具（voyager_search / voyager_show /
  voyager_brief），优先用它们
""" + MANAGED_END


class CodexIntegration:
    """Codex native SessionStart hook integration."""

    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()
        self._capabilities: Optional[ProviderCapabilities] = None
        self.hooks_file = self.home / ".codex/hooks.json"
        self.agents_md = self.home / ".codex/AGENTS.md"
        self.entrypoint = Path(__file__).with_name(ENTRYPOINT_BASENAME)
        self.relay = Path(__file__).with_name(RELAY_BASENAME)

    # -- command construction -------------------------------------------------

    def hook_command(self, interpreter: Optional[str] = None) -> str:
        """The hidden-relay hook command (unquoted, forward slashes).

        Two live-proven constraints shape this command:

        1. Codex parses hook commands with PowerShell semantics — a quoted
           path followed by another quoted path is a statement error
           ("Hook failed / exited with code 1", reproduced in G3-B).  The
           unquoted forward-slash form is the shape that live-fires.
        2. The dispatch parent is windowless, so a console python.exe child
           pops a visible console window on every session start, and
           pythonw.exe is not viable either: PowerShell does not collect
           stdout of GUI-subsystem children (verified — the handler ran,
           logged, and Codex received nothing).  The relay script therefore
           runs the real handler hidden via Start-Process
           -WindowStyle Hidden -Wait with file redirection and relays the
           captured stdout back — synchronous, silent, byte-preserving
           (standalone-verified against the real payload).
        """
        relay = self.relay.as_posix()
        # No `-NonInteractive`.  Measured directly (both flag sets, real payload):
        # the relay receives stdin and emits byte-identical protocol JSON either
        # way, so the flag buys nothing on this path -- and the canonical command
        # should stay the shape that was live-verified.  A relay that did block
        # on a prompt is still bounded by the hook's own `timeout`.
        return (f"powershell -NoLogo -NoProfile "
                f"-ExecutionPolicy Bypass -File {relay}")

    def _hook_target_ok(self) -> bool:
        return self.entrypoint.exists() and self.relay.exists()

    # -- hooks.json helpers ---------------------------------------------------

    @staticmethod
    def _is_voyager_hook(entry: Any) -> bool:
        """True if a ``SessionStart`` list entry belongs to Voyager.

        Recognises both the direct handler command and the hidden-relay
        command, so upgrades replace either shape in place.
        """
        if not isinstance(entry, dict):
            return False
        inner = entry.get("hooks")
        candidates: List[Any] = [inner] if isinstance(inner, list) else [entry]
        for group in candidates:
            handlers = group if isinstance(group, list) else [entry]
            for h in handlers:
                if not isinstance(h, dict):
                    continue
                cmd = str(h.get("command", ""))
                if ENTRYPOINT_BASENAME in cmd or RELAY_BASENAME in cmd:
                    return True
        return False

    def _load_hooks(self) -> Dict[str, Any]:
        if not self.hooks_file.exists():
            return {}
        return json.loads(self.hooks_file.read_text(encoding="utf-8"))

    def _backup(self, path: Path) -> Optional[Path]:
        if not path.exists():
            return None
        backup = path.with_name(
            path.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            shutil.copy2(path, backup)
            return backup
        except OSError:
            return None

    def _write_hooks(self, settings: Dict[str, Any]) -> Optional[Path]:
        backup = self._backup(self.hooks_file)
        self.hooks_file.parent.mkdir(parents=True, exist_ok=True)
        self.hooks_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        return backup

    def _session_start_list(self, settings: Dict[str, Any]) -> List[Any]:
        """Return the ``hooks.SessionStart`` list, normalising odd shapes."""
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

    # -- AGENTS.md managed block ----------------------------------------------

    def _upsert_agents_block(self) -> bool:
        """Replace or append the managed retrieval-guidance block.

        Retires the pre-G3-B startup-assisted wording (first-turn
        voyager_startup calls) wherever the legacy block is found, and is
        idempotent: re-installing replaces the marked block in place.
        """
        text = (self.agents_md.read_text(encoding="utf-8")
                if self.agents_md.exists() else "")
        original = text
        if MANAGED_BEGIN in text and MANAGED_END in text:
            start = text.index(MANAGED_BEGIN)
            end = text.index(MANAGED_END) + len(MANAGED_END)
            text = text[:start] + MANAGED_BLOCK + text[end:]
        elif LEGACY_HEADING in text:
            start = text.index(LEGACY_HEADING)
            nxt = text.find("\n## ", start + len(LEGACY_HEADING))
            end = nxt if nxt != -1 else len(text)
            text = text[:start] + MANAGED_BLOCK + text[end:]
        else:
            text = (text.rstrip("\n") + "\n\n" + MANAGED_BLOCK + "\n"
                    if text.strip() else MANAGED_BLOCK + "\n")
        if text != original:
            self.agents_md.parent.mkdir(parents=True, exist_ok=True)
            self.agents_md.write_text(text, encoding="utf-8")
            return True
        return False

    def _strip_agents_block(self) -> bool:
        if not self.agents_md.exists():
            return False
        text = self.agents_md.read_text(encoding="utf-8")
        if MANAGED_BEGIN not in text or MANAGED_END not in text:
            return False
        start = text.index(MANAGED_BEGIN)
        end = text.index(MANAGED_END) + len(MANAGED_END)
        cleaned = (text[:start] + text[end:]).replace("\n\n\n", "\n\n")
        self.agents_md.write_text(cleaned, encoding="utf-8")
        return True

    # -- lifecycle ------------------------------------------------------------

    def install(self, interpreter: Optional[str] = None) -> Dict[str, Any]:
        """Install (or upgrade) the native SessionStart hook.

        Additive and idempotent: user hooks are preserved, a previous Voyager
        entry is replaced rather than duplicated, and hooks.json is backed up
        before it is rewritten.
        """
        try:
            settings = self._load_hooks()
        except (json.JSONDecodeError, OSError) as e:
            backup = self._backup(self.hooks_file)
            return {
                "provider": "codex",
                "status": "error",
                "message": (
                    f"malformed {self.hooks_file} ({e}); "
                    + (f"backed up to {backup}" if backup else "no backup written")
                ),
                "backup": str(backup) if backup else None,
                "strategy": "FAILED_MALFORMED_CONFIG",
            }

        if not self._hook_target_ok():
            return {
                "provider": "codex",
                "status": "error",
                "message": (f"hook entrypoint not found: {self.entrypoint} "
                            f"or relay: {self.relay}"),
                "strategy": "FAILED_MISSING_ENTRYPOINT",
            }

        command = self.hook_command(interpreter)
        entries = self._session_start_list(settings)
        kept = [e for e in entries if not self._is_voyager_hook(e)]
        replaced = len(entries) - len(kept)

        kept.append({"hooks": [{
            "type": "command",
            "command": command,
            "commandWindows": command,
            "timeout": DEFAULT_TIMEOUT_S,
            "statusMessage": "Voyager continuity",
        }]})
        settings["hooks"]["SessionStart"] = kept

        backup = self._write_hooks(settings)
        agents_updated = self._upsert_agents_block()

        return {
            "provider": "codex",
            "status": "installed",
            "strategy": "NATIVE_SESSIONSTART_HOOK",
            "hooks_file": str(self.hooks_file),
            "entrypoint": str(self.entrypoint),
            "command": command,
            "hooks_replaced": replaced,
            "backup": str(backup) if backup else None,
            "agents_md_updated": agents_updated,
            "warnings": [
                "Restart Codex; on the next start the startup hooks review "
                "must trust the Voyager hook once.",
            ],
        }

    def remove(self) -> Dict[str, Any]:
        """Remove Voyager's hook + managed AGENTS.md block, nothing else."""
        result: Dict[str, Any] = {
            "provider": "codex", "status": "removed", "removed": 0,
        }
        try:
            if self.hooks_file.exists():
                try:
                    settings = self._load_hooks()
                except (json.JSONDecodeError, OSError) as e:
                    result["status"] = "error"
                    result["message"] = f"cannot parse {self.hooks_file}: {e}"
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
                        result["backup"] = str(self._write_hooks(settings) or "")
            result["agents_md_block_removed"] = self._strip_agents_block()
            return result
        except OSError as e:
            result["status"] = "error"
            result["message"] = str(e)
            return result

    def verify(self) -> Dict[str, Any]:
        try:
            settings = self._load_hooks() if self.hooks_file.exists() else {}
            entries = self._session_start_list(settings)
            installed = any(self._is_voyager_hook(e) for e in entries)
        except (json.JSONDecodeError, OSError):
            installed = False
        checks = {
            "hook_entrypoint_exists": self.entrypoint.exists(),
            "relay_exists": self.relay.exists(),
            "hooks_json_has_voyager": installed,
            "agents_md_managed_block": (
                self.agents_md.exists()
                and MANAGED_BEGIN in self.agents_md.read_text(encoding="utf-8")
            ),
        }
        return {
            "provider": "codex",
            "verified": all(checks.values()),
            "checks": checks,
            "strategy": ("NATIVE_SESSIONSTART_HOOK" if all(checks.values())
                         else "STARTUP_ASSISTED"),
        }

    def capabilities(self) -> ProviderCapabilities:
        """Return capability profile."""
        from .capabilities import detect_capabilities
        if self._capabilities is None:
            self._capabilities = detect_capabilities("codex", self.home)
        return self._capabilities
