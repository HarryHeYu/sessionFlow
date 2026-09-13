"""DeepSeek Harness (DSH) adapter.

Source: ~/.dsh/sessions/<munged-cwd>/session-<uuid>/session.jsonl.zstd
zstd-compressed JSONL; rows {type, seq, time(ms), data}.
Key types: session, session/title, user/message, assistant/message,
reasoning-chunks/... (明文流, skipped), tool/call, tool/result,
request/context (provider/model/contextWindow), turn/*, step/*.
"""

from __future__ import annotations

import json
import zlib  # noqa: F401  (fallback guard, see below)
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Adapter, finish_session, git_info, register
from ..model import new_event, new_session, text_of

try:
    import zstandard
except ImportError:      # pragma: no cover
    zstandard = None

HOME = Path.home()
SESSIONS_DIR = HOME / ".dsh" / "sessions"


def _read_zstd(path: Path) -> str:
    if zstandard is None:
        raise RuntimeError(
            "DSH adapter needs the 'zstandard' package: pip install zstandard"
        )
    dctx = zstandard.ZstdDecompressor()
    with open(path, "rb") as fh:
        with dctx.stream_reader(fh) as reader:
            return reader.read().decode("utf-8", errors="replace")


def _commands_from_args(name: str, arguments: Any) -> Optional[str]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    if isinstance(arguments, dict):
        for k in ("command", "cmd", "script"):
            v = arguments.get(k)
            if isinstance(v, str):
                return v
            if isinstance(v, list):
                return " ".join(str(x) for x in v)
    return None


class DshAdapter(Adapter):
    provider = "dsh"
    can_resume = True
    can_fork = False

    def discover(self) -> List[Path]:
        if not SESSIONS_DIR.is_dir():
            return []
        return sorted(SESSIONS_DIR.glob("*/session-*/session.jsonl.zstd"))

    def parse(self, source: Path) -> Optional[dict]:
        try:
            text = _read_zstd(source)
        except Exception as e:   # corrupted zstd or missing dep
            return {"__error__": str(e)}

        session: Optional[dict] = None
        events: List[dict] = []
        title: Optional[str] = None
        first_user: Optional[str] = None
        model: Optional[str] = None
        context_window: Optional[int] = None
        native: Optional[str] = None
        cwd: Optional[str] = None

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = row.get("type") or ""
            data = row.get("data") or {}
            ts = (row.get("time") or 0) / 1000.0 or None
            seq = row.get("seq")
            if seq is None:
                seq = len(events) + 1   # keep ORDER BY seq stable

            if typ == "session":
                # fields live at the row top level (older builds nested them
                # in `data` — support both)
                native = row.get("id") or data.get("id")
                cwd = row.get("cwd") or data.get("cwd")
                created = row.get("createdAt") or data.get("createdAt")
                session = new_session(provider=self.provider,
                                      native_session_id=native,
                                      cwd=cwd,
                                      started_at=(created or 0) / 1000.0 or None)
                session["raw_metadata"] = {
                    k: row.get(k) or data.get(k)
                    for k in ("agentPreset", "delegationDepth", "createdAt",
                              "version") if (row.get(k) or data.get(k)) is not None
                }
                continue
            if session is None:
                continue

            def ev(**kw):
                e = new_event(sid=session["id"], ts=ts, seq=seq, raw_event=row, **kw)
                events.append(e)
                return e

            if typ == "session/title":
                t = data.get("title") or text_of(data)
                if t:
                    title = t
            elif typ == "user/message":
                text = text_of(data.get("content"))
                if first_user is None and text.strip():
                    first_user = text
                ev(kind="user", role="user", content=text or None)
            elif typ == "assistant/message":
                msg = data.get("message") or {}
                for block in msg.get("content") or []:
                    btype = block.get("type")
                    if btype == "text":
                        ev(kind="assistant", role="assistant", content=block.get("text"))
                    elif btype == "reasoning":
                        ev(kind="reasoning", role="assistant", content=block.get("reasoning"))
                    elif btype == "tool-call":
                        ev(kind="tool_call", role="assistant",
                           tool_name=block.get("name") or block.get("toolName"),
                           tool_call_id=block.get("callId") or block.get("id"),
                           tool_input=block.get("arguments")
                           if isinstance(block.get("arguments"), str) else
                           json.dumps(block.get("arguments"), ensure_ascii=False)
                           if block.get("arguments") is not None else None)
            elif typ == "tool/call":
                args = data.get("arguments")
                if isinstance(args, str):
                    try:
                        parsed = json.loads(args)
                    except json.JSONDecodeError:
                        parsed = None
                else:
                    parsed = args
                ev(kind="tool_call", role="assistant",
                   tool_name=data.get("name"),
                   tool_call_id=data.get("callId"),
                   tool_input=json.dumps(parsed, ensure_ascii=False)
                   if parsed is not None else (args if isinstance(args, str) else None),
                   command=_commands_from_args(data.get("name") or "", parsed or args))
            elif typ == "tool/result":
                src = (data.get("message") or {}).get("source") or {}
                body_parts = []
                for block in data.get("content") or []:
                    if isinstance(block, dict):
                        inner = block.get("content")
                        body_parts.append(text_of(inner))
                ev(kind="tool_result", role="tool",
                   tool_call_id=src.get("callId"),
                   tool_output="\n".join(p for p in body_parts if p) or None)
            elif typ == "request/context":
                model = data.get("model") or model
                context_window = data.get("contextWindow") or context_window
            elif typ == "error" or "error" in typ:
                ev(kind="error", role="system", content=text_of(data) or str(data)[:500])

        if session is None or native is None:
            return None

        session["id"] = f"dsh:{native}"
        for e in events:
            e["sid"] = session["id"]
        session["title"] = title or (first_user and " ".join(first_user.split())[:120]) or native
        session["updated_at"] = max((e["ts"] for e in events if e.get("ts")),
                                    default=session.get("started_at"))
        session["model"] = model
        session["message_count"] = sum(1 for e in events if e["kind"] in ("user", "assistant"))
        session["tool_count"] = sum(1 for e in events if e["kind"] == "tool_call")
        session["resume_cmd"] = f"dsh --resume {native}"
        session["metadata"] = {"context_window": context_window}
        git = git_info(cwd)
        return {
            "session": finish_session(session, git),
            "events": events,
            "extra_sources": [],
        }


register(DshAdapter())
