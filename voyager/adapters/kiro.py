"""AWS Kiro IDE adapter.

Source: AppData/Roaming/Kiro/User/globalStorage/kiro.kiroagent/
  workspace-sessions/<base64url(cwd)>/<uuid>.json   (conversation)
  workspace-sessions/<base64url(cwd)>/sessions.json (titles + dateCreated)
Conversation JSON: history[] = {message:{role, content: str|parts[]}}.
Kiro persists no tool calls, diffs or per-session tokens — this adapter
indexes what exists (conversation + title + model + workspace).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Adapter, finish_session, git_info, register
from ..model import new_event, new_session


def kiro_agent_dir() -> Optional[Path]:
    base = os.environ.get("APPDATA")
    if not base:
        return None
    d = Path(base) / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"
    return d if d.is_dir() else None


def _content_text(content: Any) -> tuple:
    """Return (text, file_paths) from a Kiro message content value."""
    if isinstance(content, str):
        return content, []
    texts, files = [], []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text" and part.get("text"):
            texts.append(part["text"])
        elif ptype in ("mention", "file") and part.get("text"):
            files.append(part["text"])
    return "\n".join(texts), files


class KiroAdapter(Adapter):
    provider = "kiro"
    can_resume = False
    can_fork = False

    def discover(self) -> List[Path]:
        base = kiro_agent_dir()
        if not base:
            return []
        ws = base / "workspace-sessions"
        if not ws.is_dir():
            return []
        return [p for p in ws.glob("*/*.json") if p.name != "sessions.json"]

    def parse(self, source: Path) -> Optional[dict]:
        try:
            d = json.load(open(source, encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        native = d.get("sessionId") or source.stem
        sid = f"kiro:{native}"
        cwd = d.get("workspaceDirectory") or d.get("workspacePath")
        model = d.get("selectedModel")
        # dateCreated lives in the sibling sessions.json
        created = None
        try:
            smap = json.load(open(source.parent / "sessions.json", encoding="utf-8"))
            for entry in smap if isinstance(smap, list) else []:
                if entry.get("sessionId") == native:
                    created = float(entry.get("dateCreated") or 0) / 1000.0 or None
                    break
        except (OSError, json.JSONDecodeError, ValueError):
            pass

        events: List[dict] = []
        history = d.get("history") or []
        for seq, entry in enumerate(history):
            msg = entry.get("message") or {}
            role = msg.get("role") or "user"
            text, files = _content_text(msg.get("content"))
            if role not in ("user", "assistant"):
                role = "assistant" if role == "assistant" else "user"
            events.append(new_event(
                sid=sid, seq=seq, kind=role, role=role,
                content=text or None,
                files=files or None,
                model=model if role == "assistant" else None,
                raw_event=entry,
            ))
        if not events:
            return None

        session = new_session(
            id=sid,
            provider=self.provider,
            native_session_id=native,
            title=d.get("title") or native,
            started_at=created,
            updated_at=source.stat().st_mtime,
            cwd=cwd,
            model=model,
            message_count=sum(1 for e in events if e["kind"] in ("user", "assistant")),
            can_resume=False,
            resume_cmd=None,
            metadata={
                "context_usage_pct": d.get("contextUsagePercentage"),
                "autonomy_mode": d.get("autonomyMode"),
                "session_type": d.get("sessionType"),
            },
            raw_metadata={},
        )
        for e in events:
            e["sid"] = sid
        session["tool_count"] = 0
        git = git_info(cwd)
        return {"session": finish_session(session, git), "events": events,
                "extra_sources": [source.parent / "sessions.json"]}


register(KiroAdapter())
