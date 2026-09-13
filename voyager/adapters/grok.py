"""xAI Grok CLI adapter.

Source: ~/.grok/sessions/<urlencoded-cwd>/<session-uuid>/
  chat_history.jsonl  — OpenAI-style lines {type: user|assistant|reasoning|
                        tool_result|system, content, tool_calls, tool_call_id}
  summary.json        — info{id,cwd}, session_summary, created/updated,
                        current_model_id, git_root_dir/remotes/head_commit
Note: reasoning content is server-encrypted (kept as raw, no text).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Adapter, finish_session, register
from ..model import new_event, new_session, text_of

HOME = Path.home()
SESSIONS_DIR = HOME / ".grok" / "sessions"


def parse_ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class GrokAdapter(Adapter):
    provider = "grok"
    can_resume = True
    can_fork = True

    def discover(self) -> List[Path]:
        if not SESSIONS_DIR.is_dir():
            return []
        return sorted(SESSIONS_DIR.rglob("chat_history.jsonl"))

    def parse(self, source: Path) -> Optional[dict]:
        sdir = source.parent
        summary: Dict[str, Any] = {}
        try:
            sm_path = sdir / "summary.json"
            if sm_path.is_file():
                summary = json.load(open(sm_path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}

        info = summary.get("info") or {}
        native = info.get("id") or sdir.name
        sid = f"grok:{native}"
        session = new_session(
            provider=self.provider,
            native_session_id=native,
            cwd=info.get("cwd"),
            title=summary.get("session_summary") or native,
            started_at=parse_ts(summary.get("created_at")),
            updated_at=parse_ts(summary.get("updated_at")),
            model=summary.get("current_model_id"),
            git_branch=summary.get("head_branch"),
            git_commit=summary.get("head_commit"),
        )
        remotes = summary.get("git_remotes") or []
        if remotes:
            session["git_remote"] = remotes[0]
        session["raw_metadata"] = {
            k: summary.get(k) for k in ("agent_name", "sandbox_profile",
                                        "reasoning_effort", "num_messages",
                                        "chat_format_version")
            if summary.get(k) is not None
        }

        events: List[dict] = []
        first_user: Optional[str] = None
        try:
            fh = open(source, encoding="utf-8", errors="replace")
        except OSError:
            return None
        with fh:
            for seq, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = row.get("type")

                def ev(**kw):
                    e = new_event(sid=sid, seq=seq, raw_event=row, **kw)
                    events.append(e)
                    return e

                if typ == "user":
                    text = text_of(row.get("content"))
                    if text and first_user is None and "<system-reminder>" not in text[:60]:
                        first_user = text
                    ev(kind="user", role="user", content=text or None)
                elif typ == "assistant":
                    if row.get("content"):
                        ev(kind="assistant", role="assistant",
                           content=row["content"], model=row.get("model_id"))
                    for tc in row.get("tool_calls") or []:
                        args = tc.get("arguments")
                        cmd = None
                        try:
                            parsed = json.loads(args) if isinstance(args, str) else args
                        except json.JSONDecodeError:
                            parsed = None
                        if isinstance(parsed, dict):
                            for k in ("command", "cmd", "target_file", "path"):
                                if isinstance(parsed.get(k), str):
                                    cmd = parsed[k]
                                    break
                        ev(kind="tool_call", role="assistant",
                           tool_name=tc.get("name"), tool_call_id=tc.get("id"),
                           tool_input=args if isinstance(args, str) else
                           json.dumps(args, ensure_ascii=False) if args is not None else None,
                           command=cmd, model=row.get("model_id"))
                elif typ == "tool_result":
                    ev(kind="tool_result", role="tool",
                       tool_call_id=row.get("tool_call_id"),
                       tool_output=text_of(row.get("content")) or None)
                elif typ == "reasoning":
                    # content is server-encrypted; keep the placeholder event
                    ev(kind="reasoning", role="assistant", content=None)
                elif typ == "system":
                    ev(kind="meta", role="system", content=None)

        if not events:
            return None
        if not session.get("title") or session["title"] == native:
            session["title"] = (first_user and " ".join(first_user.split())[:120]) \
                or summary.get("session_summary") or native
        session["id"] = sid
        for e in events:
            e["sid"] = sid
        session["message_count"] = sum(1 for e in events
                                       if e["kind"] in ("user", "assistant"))
        session["tool_count"] = sum(1 for e in events if e["kind"] == "tool_call")
        session["can_resume"] = True
        session["can_fork"] = True
        session["resume_cmd"] = f"grok -r {native}"
        git = {"repo_root": summary.get("git_root_dir"),
               "remote": session.get("git_remote"),
               "commit": summary.get("head_commit")}
        return {"session": finish_session(session, git), "events": events,
                "extra_sources": [sdir / "summary.json", sdir / "events.jsonl"]}


register(GrokAdapter())
