"""Google Antigravity adapter (experimental, heuristic).

Source: ~/.gemini/antigravity/conversations/<uuid>.db (SQLite)
  steps(idx, step_type, step_payload BLOB-protobuf, ...)
The step payloads are protobuf without a public schema. This adapter uses
the field types identified in docs/RECON.md (14=init/prompt, 15=message,
132=tool call, 101=task notification with exit code, 17=error) and extracts
readable UTF-8 runs from the blobs — a heuristic, not a full decode. Session
level metadata (title/paths) is reliable; treat event granularity as
best-effort.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Adapter, finish_session, register
from ..model import new_event, new_session

HOME = Path.home()
CONV_DIR = HOME / ".gemini" / "antigravity" / "conversations"

_PRINTABLE = re.compile(rb"[\x20-\x7e\x80-\xff][\x20-\x7e\x80-\xff]{15,}")
# skip protobuf-noise strings: hex/uuid fragments and short base64-ish runs
_NOISE = re.compile(r"^[0-9a-fA-F-]{20,}$")


def _strings(blob: bytes, min_len: int = 16) -> List[str]:
    out = []
    for m in _PRINTABLE.finditer(blob or b""):
        try:
            s = m.group(0).decode("utf-8", errors="replace")
        except Exception:
            continue
        s = s.strip()
        if len(s) >= min_len and not _NOISE.match(s):
            out.append(s)
    return out


_EXIT_CODE = re.compile(r"exited with code (\d+)", re.I)


class AntigravityAdapter(Adapter):
    provider = "antigravity"
    can_resume = False
    can_fork = False

    def discover(self) -> List[Path]:
        if not CONV_DIR.is_dir():
            return []
        return sorted(CONV_DIR.glob("*.db"))

    def parse(self, source: Path) -> Optional[dict]:
        try:
            con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        native = source.stem
        sid = f"antigravity:{native}"
        events: List[dict] = []
        title_parts: List[str] = []
        git_urls: List[str] = []
        try:
            rows = con.execute(
                "SELECT idx, step_type, step_payload FROM steps ORDER BY idx"
            ).fetchall()
        except sqlite3.Error:
            return None
        try:
            for seq, (idx, stype, payload) in enumerate(rows):
                if not payload:
                    continue

                def ev(**kw):
                    e = new_event(sid=sid, seq=idx, kind=kw.pop("kind"),
                                  raw_event={"step_type": stype,
                                             "payload": payload[:2000]
                                             .decode("utf-8", errors="replace")},
                                  **kw)
                    events.append(e)
                    return e

                if stype == 14 and not title_parts:
                    # init: contains the user's prompt / project context
                    ss = _strings(payload, 20)
                    if ss:
                        title_parts.append(ss[0])
                        ev(kind="user", role="user", content=ss[0][:5000])
                        for u in re.findall(r"https?://[^\s\"']+", payload.decode(
                                "utf-8", errors="replace")):
                            if "git" in u and u not in git_urls:
                                git_urls.append(u)
                elif stype == 15:
                    ss = _strings(payload, 24)
                    if ss:
                        # heuristic: the longest run is the message body
                        body = max(ss, key=len)
                        ev(kind="assistant", role="assistant", content=body[:8000])
                elif stype == 132:
                    ss = _strings(payload, 4)
                    name = next((s for s in ss if re.match(
                        r"^[a-z_]{3,40}$", s)), None)
                    call_id = next((s for s in ss if s.startswith("toolu_")), None)
                    args = next((s for s in ss if s.startswith("{")), None)
                    ev(kind="tool_call", role="assistant",
                       tool_name=name or "tool", tool_call_id=call_id,
                       tool_input=args)
                elif stype == 101:
                    text = payload.decode("utf-8", errors="replace")
                    m = _EXIT_CODE.search(text)
                    body = _strings(payload, 24)
                    ev(kind="tool_result", role="tool",
                       exit_code=int(m.group(1)) if m else None,
                       tool_output=(body[0] if body else
                                    text[:500] if text.strip() else None))
                elif stype == 17:
                    ss = _strings(payload, 20)
                    if ss:
                        ev(kind="error", role="system", content=ss[-1][:2000])
        finally:
            con.close()

        if not events:
            return None
        session = new_session(
            id=sid,
            provider=self.provider,
            native_session_id=native,
            title=(title_parts[0][:120] if title_parts else native),
            started_at=None,
            updated_at=source.stat().st_mtime,
            model="deepseek-v4-flash/claude (per-step)",
            message_count=sum(1 for e in events
                              if e["kind"] in ("user", "assistant")),
            tool_count=sum(1 for e in events if e["kind"] == "tool_call"),
            can_resume=False,
            resume_cmd=None,
            metadata={"experimental": "protobuf heuristic decode"},
            raw_metadata={"git_urls": git_urls or None},
        )
        for e in events:
            e["sid"] = sid
        return {"session": finish_session(session, {"remote": git_urls[0]
                                                    if git_urls else None}),
                "events": events, "extra_sources": []}


register(AntigravityAdapter())
