"""Anthropic Claude Code adapter.

Source: ~/.claude/projects/<munged-cwd>/<sessionId>.jsonl
Chain: uuid/parentUuid; per-line cwd/gitBranch/sessionId/version.
Bonus: ~/.claude/file-history/<sessionId>/<hash>@vN full file versions +
file-history-snapshot events -> `voyager diff` support.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Adapter, finish_session, git_info, register
from ..model import new_event, new_session, text_of

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
FILE_HISTORY_DIR = HOME / ".claude" / "file-history"


def parse_ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _blocks(content: Any):
    """Yield (block_type, block) pairs from a message content value."""
    if content is None:
        return
    if isinstance(content, str):
        yield "text", {"type": "text", "text": content}
        return
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type"):
                yield b["type"], b


class ClaudeAdapter(Adapter):
    provider = "claude"
    can_resume = True
    can_fork = True

    def discover(self) -> List[Path]:
        if not PROJECTS_DIR.is_dir():
            return []
        return sorted(PROJECTS_DIR.glob("*/*.jsonl"))

    def parse(self, source: Path) -> Optional[dict]:
        native_id = source.stem
        sid = f"claude:{native_id}"
        session = new_session(provider=self.provider, native_session_id=native_id)
        events: List[dict] = []
        files: List[dict] = []
        title: Optional[str] = None
        model: Optional[str] = None
        usage_total: Dict[str, int] = {}

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
                ts = parse_ts(row.get("timestamp"))

                if session.get("cwd") is None and row.get("cwd"):
                    session["cwd"] = row.get("cwd")
                if row.get("gitBranch"):
                    session["git_branch"] = row.get("gitBranch")

                def ev(**kw):
                    e = new_event(sid=sid, ts=ts, seq=seq, raw_event=row, **kw)
                    events.append(e)
                    return e

                if typ in ("user", "assistant"):
                    msg = row.get("message") or {}
                    role = msg.get("role") or typ
                    if msg.get("model"):
                        model = msg["model"]
                    u = msg.get("usage")
                    if u:
                        for k in ("input_tokens", "output_tokens",
                                  "cache_creation_input_tokens",
                                  "cache_read_input_tokens"):
                            if u.get(k):
                                usage_total[k] = usage_total.get(k, 0) + u[k]
                    seen_result = False
                    for btype, block in _blocks(msg.get("content")):
                        if btype == "text":
                            text = block.get("text") or ""
                            if typ == "user" and title is None and text.strip():
                                title = " ".join(text.split())[:120]
                            ev(kind=role if role in ("user", "assistant") else "meta",
                               role=role, content=text or None)
                        elif btype == "thinking":
                            ev(kind="reasoning", role="assistant",
                               content=block.get("thinking"), model=model)
                        elif btype == "tool_use":
                            inp = block.get("input")
                            cmd = None
                            if isinstance(inp, dict) and inp.get("command"):
                                cmd = inp["command"]
                            fp = inp.get("file_path") if isinstance(inp, dict) else None
                            ev(kind="tool_call", role="assistant",
                               tool_name=block.get("name"),
                               tool_call_id=block.get("id"),
                               tool_input=json.dumps(inp, ensure_ascii=False)
                               if inp is not None else None,
                               command=cmd, file_path=fp, model=model)
                        elif btype == "tool_result" and not seen_result:
                            seen_result = True
                            tur = row.get("toolUseResult")
                            stdout = stderr = None
                            rc = None
                            if isinstance(tur, dict):
                                stdout = tur.get("stdout")
                                stderr = tur.get("stderr")
                                rc = tur.get("return_code") or tur.get("exit_code")
                            ev(kind="tool_result", role="tool",
                               tool_call_id=block.get("tool_use_id"),
                               tool_output=text_of(block.get("content")) or None,
                               stdout=stdout, stderr=stderr, exit_code=rc)
                elif typ == "system":
                    subtype = row.get("subtype")
                    if subtype and "error" in str(subtype).lower():
                        ev(kind="error", role="system",
                           content=row.get("content") or subtype)
                elif typ == "file-history-snapshot":
                    snap = (row.get("snapshot") or {}).get("trackedFileBackups") or {}
                    paths = list(snap.keys())
                    ev(kind="snapshot", role="system", files=paths,
                       file_path=paths[0] if len(paths) == 1 else None)
                    for p, b in snap.items():
                        files.append({
                            "path": p,
                            "backup": b if isinstance(b, str) else json.dumps(b, ensure_ascii=False),
                        })
                elif typ == "queue-operation":
                    continue
                # unknown types: preserved via raw only if we emit an event;
                # skipped silently to keep the timeline clean.

        if not events:
            return None

        session["id"] = sid
        for e in events:
            e["sid"] = sid
        session["title"] = title or native_id
        session["started_at"] = min(
            (e["ts"] for e in events if e.get("ts")), default=None
        )
        session["updated_at"] = max(
            (e["ts"] for e in events if e.get("ts")), default=None
        )
        session["model"] = model
        session["message_count"] = sum(
            1 for e in events if e["kind"] in ("user", "assistant")
        )
        session["tool_count"] = sum(
            1 for e in events if e["kind"] == "tool_call"
        )
        session["can_resume"] = True
        session["can_fork"] = True
        session["resume_cmd"] = f"claude --resume {native_id}"
        session["metadata"] = {"usage_totals": usage_total or None}
        session["_files"] = files
        git = git_info(session.get("cwd"))
        return {
            "session": finish_session(session, git),
            "events": events,
            "extra_sources": [],
        }

    # -- file history / diff support ---------------------------------------

    def session_files(self, native_id: str) -> List[dict]:
        d = FILE_HISTORY_DIR / native_id
        if not d.is_dir():
            return []
        out = []
        for f in sorted(d.iterdir()):
            if "@" in f.name:
                base, ver = f.name.rsplit("@", 1)
                out.append({"base": base, "version": ver, "path": f})
        return out


register(ClaudeAdapter())
