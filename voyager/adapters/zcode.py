"""ZCode adapter — SQLite store (read-only access to the live CLI DB).

Source: ~/.zcode/cli/db/db.sqlite
  session(id, directory, title, parent_id, project_id, time_created/updated(ms), ...)
  message(id, session_id, time_created, data JSON {role, model, path, cost, tokens})
  part(message_id, data JSON {type: text|reasoning|tool|file|step-*|timeline})
  model_usage(...) / tool_usage(...) — rich aggregates
Source of truth stays the provider DB; we copy into the voyager index so
search/repo views work uniformly.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .base import Adapter, finish_session, git_info, register
from ..model import new_event, new_session, text_of

HOME = Path.home()
DB_PATH = HOME / ".zcode" / "cli" / "db" / "db.sqlite"


def _open_ro(path: Path) -> Optional[sqlite3.Connection]:
    if not path.is_file():
        return None
    try:
        uri = "file:///" + quote(str(path).replace("\\", "/")) + "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None


def _json(s: Any) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


class ZCodeAdapter(Adapter):
    provider = "zcode"
    can_resume = False        # desktop-side resume; no public CLI confirmed yet
    can_fork = False

    def discover(self) -> List[Path]:
        return [DB_PATH] if DB_PATH.is_file() else []

    def parse(self, source: Path) -> Optional[dict]:
        # One source file holds many sessions; the base scanner expects one
        # session per artifact, so we override scan() instead of using parse().
        raise NotImplementedError

    def scan(self, source_changed) -> List[dict]:
        con = _open_ro(DB_PATH)
        if not con:
            return []
        try:
            sessions: List[dict] = []
            rows = con.execute(
                """SELECT id, directory, title, parent_id, project_id,
                          time_created, time_updated, summary_additions,
                          summary_deletions, summary_files
                   FROM session ORDER BY time_created"""
            ).fetchall()
            for (sid_native, directory, title, parent_id, project_id,
                 t_created, t_updated, s_add, s_del, s_files) in rows:
                sid = f"zcode:{sid_native}"
                events: List[dict] = []
                usage_total: Dict[str, int] = {}
                model: Optional[str] = None
                cwd = directory

                # tool exit codes: tool_call_id -> exit_code / error
                tu: Dict[str, dict] = {}
                for (call_id, exit_code, err, so, se) in con.execute(
                    """SELECT tool_call_id, exit_code, error_message,
                              stdout_bytes, stderr_bytes
                       FROM tool_usage WHERE session_id=? AND tool_call_id IS NOT NULL""",
                    (sid_native,),
                ):
                    tu[call_id] = {"exit_code": exit_code,
                                   "error_message": err,
                                   "stdout_bytes": so, "stderr_bytes": se}

                msgs = con.execute(
                    """SELECT id, time_created, data FROM message
                       WHERE session_id=? ORDER BY sequence, time_created""",
                    (sid_native,),
                ).fetchall()
                seq = 0
                first_user: Optional[str] = None
                for (mid, m_ts, m_data) in msgs:
                    md = _json(m_data) or {}
                    role = md.get("role")
                    mtime = (m_ts or 0) / 1000.0 or None
                    model_info = md.get("model") or {}
                    if model_info.get("modelID"):
                        model = (f"{model_info.get('providerID')}:{model_info.get('modelID')}"
                                 if model_info.get("providerID") else model_info.get("modelID"))
                    path_info = md.get("path") or {}
                    if path_info.get("cwd"):
                        cwd = path_info["cwd"]

                    parts = con.execute(
                        """SELECT data FROM part WHERE message_id=?
                           ORDER BY sequence, time_created""",
                        (mid,),
                    ).fetchall()
                    for (pdata,) in parts:
                        p = _json(pdata) or {}
                        ptype = p.get("type")
                        ptime = (p.get("time") or {}).get("created")
                        ts = (ptime / 1000.0) if ptime else mtime
                        seq += 1

                        def ev(**kw):
                            e = new_event(sid=sid, ts=ts, seq=seq,
                                          raw_event=p, **kw)
                            events.append(e)
                            return e

                        if ptype == "text" and role in ("user", "assistant"):
                            text = p.get("text") or ""
                            if role == "user" and first_user is None and text.strip():
                                first_user = text
                            ev(kind=role, role=role, content=text or None)
                        elif ptype == "reasoning":
                            ev(kind="reasoning", role="assistant",
                               content=p.get("text"), model=model)
                        elif ptype == "tool":
                            state = p.get("state") or {}
                            inp = state.get("input")
                            fp = inp.get("file_path") if isinstance(inp, dict) else None
                            cmd = None
                            if isinstance(inp, dict):
                                for k in ("command", "cmd", "script"):
                                    if isinstance(inp.get(k), str):
                                        cmd = inp[k]
                                        break
                            tuinfo = tu.get(p.get("callID") or "", {})
                            out = state.get("output")
                            ev(kind="tool_call", role="assistant",
                               tool_name=p.get("tool"),
                               tool_call_id=p.get("callID"),
                               tool_input=json.dumps(inp, ensure_ascii=False)
                               if inp is not None else None,
                               command=cmd, file_path=fp,
                               tool_output=out if state.get("status") == "completed" else None,
                               exit_code=tuinfo.get("exit_code"),
                               model=model)
                        elif ptype == "file":
                            fp = p.get("url") or p.get("filename")
                            ev(kind="file", role="user", file_path=fp,
                               content=p.get("text") or None)

                # aggregate token usage for the session
                for (pid, prov, it, ot, rt, cr, cc) in con.execute(
                    """SELECT model_id, provider_id, SUM(input_tokens),
                              SUM(output_tokens), SUM(reasoning_tokens),
                              SUM(cache_read_input_tokens),
                              SUM(cache_creation_input_tokens)
                       FROM model_usage WHERE session_id=? GROUP BY model_id, provider_id""",
                    (sid_native,),
                ):
                    if it or ot or cr or cc or rt:
                        usage_total.setdefault("by_model", {})[
                            f"{prov}:{pid}" if prov else str(pid)
                        ] = {"input": it, "output": ot, "reasoning": rt,
                             "cache_read": cr, "cache_write": cc}

                if not events and not title:
                    continue

                session = new_session(
                    id=f"zcode:{sid_native}",
                    provider=self.provider,
                    native_session_id=sid_native,
                    title=title or (first_user and " ".join(first_user.split())[:120]) or sid_native,
                    started_at=(t_created or 0) / 1000.0 or None,
                    updated_at=(t_updated or 0) / 1000.0 or None,
                    cwd=cwd,
                    model=model,
                    message_count=sum(1 for e in events if e["kind"] in ("user", "assistant")),
                    tool_count=sum(1 for e in events if e["kind"] == "tool_call"),
                    can_resume=False,
                    can_fork=False,
                    resume_cmd=None,
                    metadata={"usage_totals": usage_total or None,
                              "project_id": project_id,
                              "summary": {"additions": s_add, "deletions": s_del,
                                          "files": s_files}},
                    raw_metadata={"parent_id": parent_id},
                )
                for e in events:
                    e["sid"] = sid
                sessions.append({
                    "session": finish_session(session, git_info(cwd)),
                    "events": events,
                    "extra_sources": [],
                })
            return sessions
        finally:
            con.close()


register(ZCodeAdapter())
