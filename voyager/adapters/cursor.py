"""Cursor adapter (experimental).

Source: AppData/Roaming/Cursor/User/globalStorage/state.vscdb (SQLite)
  cursorDiskKV key 'composerData:<id>'  -> session meta (name, createdAt,
      modelConfig.modelName, fullConversationHeadersOnly, trackedGitRepos)
  cursorDiskKV key 'bubbleId:<cid>:<bid>' -> one message bubble:
      type 1 = user, type 2 = assistant; toolFormerData carries tool
      name/rawArgs/result; tokenCount may exist.
The DB is opened read-only; the index copies what it needs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Adapter, finish_session, register
from ..model import new_event, new_session

HOME = Path.home()
VSCDB = HOME / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _open_ro(path: Path) -> Optional[sqlite3.Connection]:
    if not path.is_file():
        return None
    try:
        from urllib.parse import quote
        uri = "file:///" + quote(str(path).replace("\\", "/")) + "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


class CursorAdapter(Adapter):
    provider = "cursor"
    can_resume = False
    can_fork = False

    def discover(self) -> List[Path]:
        return [VSCDB] if VSCDB.is_file() else []

    def scan(self, source_changed) -> List[dict]:
        con = _open_ro(VSCDB)
        if not con:
            return []
        out: List[dict] = []
        try:
            composers = con.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ).fetchall()
            for (ckey, cvalue) in composers:
                try:
                    cd = json.loads(cvalue)
                except json.JSONDecodeError:
                    continue
                cid = cd.get("composerId") or ckey.split(":", 1)[1]
                headers = cd.get("fullConversationHeadersOnly") or []
                # skip empty conversations (draft-only composers)
                if not headers:
                    continue
                sid = f"cursor:{cid}"
                created = (cd.get("createdAt") or 0) / 1000.0 or None
                events: List[dict] = []
                for seq, hh in enumerate(headers):
                    bid = (hh or {}).get("bubbleId")
                    if not bid:
                        continue
                    row = con.execute(
                        "SELECT value FROM cursorDiskKV WHERE key=?",
                        (f"bubbleId:{cid}:{bid}",),
                    ).fetchone()
                    if not row:
                        continue
                    try:
                        b = json.loads(row[0])
                    except json.JSONDecodeError:
                        continue
                    btype = b.get("type")
                    try:
                        ts = float(b.get("createdAt") or 0) / 1000.0 or None
                    except (TypeError, ValueError):
                        ts = None
                    if btype == 1:
                        events.append(new_event(
                            sid=sid, ts=ts, seq=seq, kind="user", role="user",
                            content=b.get("text") or None, raw_event=b))
                    elif btype == 2:
                        if b.get("text"):
                            events.append(new_event(
                                sid=sid, ts=ts, seq=seq, kind="assistant",
                                role="assistant", content=b.get("text"),
                                raw_event=b))
                        tf = b.get("toolFormerData")
                        if tf:
                            raw_args = _json(tf.get("rawArgs"))
                            result = _json(tf.get("result"))
                            ev = new_event(
                                sid=sid, ts=ts, seq=seq, kind="tool_call",
                                role="assistant", tool_name=tf.get("name"),
                                tool_call_id=tf.get("toolCallId"),
                                tool_input=json.dumps(raw_args, ensure_ascii=False)
                                if raw_args is not None else tf.get("rawArgs"),
                                tool_output=json.dumps(result, ensure_ascii=False)
                                if result is not None else
                                (tf.get("result") if isinstance(tf.get("result"), str) else None),
                                model=(b.get("tokenCount") or {}).get("model") or None,
                                raw_event=b)
                            events.append(ev)
                if not events:
                    continue
                repos = cd.get("trackedGitRepos") or []
                repo_root = None
                remote = None
                if repos:
                    first = repos[0]
                    if isinstance(first, str):
                        repo_root = first
                    elif isinstance(first, dict):
                        repo_root = first.get("path") or first.get("remoteUrl")
                        remote = first.get("remoteUrl") if first.get("remoteUrl") else None
                model = (cd.get("modelConfig") or {}).get("modelName")
                session = new_session(
                    id=sid,
                    provider=self.provider,
                    native_session_id=cid,
                    title=cd.get("name") or (events[0].get("content") or "")[:100] or cid,
                    started_at=created,
                    updated_at=max((e["ts"] for e in events if e.get("ts")),
                                   default=created),
                    model=model,
                    message_count=sum(1 for e in events
                                      if e["kind"] in ("user", "assistant")),
                    tool_count=sum(1 for e in events if e["kind"] == "tool_call"),
                    can_resume=False,
                    resume_cmd=None,
                    metadata={"is_agentic": cd.get("isAgentic"),
                              "total_lines_added": cd.get("totalLinesAdded"),
                              "total_lines_removed": cd.get("totalLinesRemoved")},
                    raw_metadata={"git_repos": repos or None},
                )
                for e in events:
                    e["sid"] = sid
                out.append({"session": finish_session(session, {
                    "repo_root": repo_root, "remote": remote}), "events": events,
                    "extra_sources": []})
        finally:
            con.close()
        return out


register(CursorAdapter())
