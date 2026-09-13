"""OpenAI Codex adapter (CLI / VS Code extension / Desktop share one store).

Source: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>_<window_id>.jsonl
Each line: {"timestamp": iso, "ordinal": int, "type": ..., "payload": {...}}
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Adapter, finish_session, git_info, register
from ..model import new_event, new_session, text_of

HOME = Path.home()
SESSIONS_DIR = HOME / ".codex" / "sessions"


def parse_ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_ENV_NOISE = re.compile(
    r"<(environment_context|user_instructions|ENVIRONMENT_CONTEXT|user_instructions)>"
    r".*?</\1>", re.S,
)


def _clean_title(text: str) -> str:
    """Strip codex's injected <environment_context>/<user_instructions> blocks.

    Titles only need the head of the message; cap the regex input to keep
    the .*? scan linear on huge messages.
    """
    head = (text or "")[:8000]
    cleaned = _ENV_NOISE.sub(" ", head)
    cleaned = " ".join(cleaned.split())
    return cleaned[:120]


def _extract_output(output: Any) -> tuple:
    """Codex tool outputs come in two shapes; normalize both."""
    if isinstance(output, list):
        # custom_tool_call_output: [{exit_code, wall_time_seconds, output}]
        rc = None
        texts = []
        for item in output:
            if isinstance(item, dict):
                if item.get("exit_code") is not None:
                    rc = item["exit_code"]
                texts.append(str(item.get("output") or ""))
        return rc, "\n".join(texts)
    if isinstance(output, str):
        # function_call_output: "Exit code: N\nWall time: ...\nOutput:\n..."
        rc = None
        body = output
        for line in output.splitlines()[:3]:
            if line.lower().startswith("exit code:"):
                try:
                    rc = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        marker = "Output:"
        idx = output.find(marker)
        if idx >= 0:
            body = output[idx + len(marker):].lstrip("\n")
        return rc, body
    return None, text_of(output)


def _command_from_args(name: str, arguments: Any) -> Optional[str]:
    if not isinstance(arguments, dict):
        return None
    for key in ("command", "cmd", "script", "cmdline"):
        v = arguments.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
    return None


class CodexAdapter(Adapter):
    provider = "codex"
    can_resume = True
    can_fork = True

    def discover(self) -> List[Path]:
        if not SESSIONS_DIR.is_dir():
            return []
        return sorted(SESSIONS_DIR.rglob("rollout-*.jsonl"))

    def _session_id_of(self, path: Path) -> str:
        # rollout-<ts>-<session_id>_<window_id>.jsonl
        stem = path.stem  # rollout-2026-09-12T14-54-06-<sid>_<wid>
        stem = stem[len("rollout-"):]
        if "_" in stem:
            sid = stem.rsplit("_", 1)[0]
        else:
            sid = stem
        # sid itself is <timestamp>-<uuid>; take the trailing uuid part
        tail = sid.split("-")
        if len(tail) >= 5:
            return "-".join(tail[-5:])
        return sid

    def scan(self, source_changed) -> List[dict]:
        """Group continuation rollouts by session id and merge each group."""
        groups: Dict[str, List[Path]] = {}
        for f in self.discover():
            groups.setdefault(self._session_id_of(f), []).append(f)
        out: List[dict] = []
        for native, files in sorted(groups.items()):
            files.sort(key=lambda p: p.name)  # rollouts are named by start time
            sid = f"codex:{native}"
            merged: Optional[dict] = None
            for f in files:
                r = self.parse(f)
                if not r or r.get("__error__"):
                    continue
                if merged is None:
                    merged = r
                    merged["session"]["id"] = sid
                    merged["session"]["native_session_id"] = native
                else:
                    # continuation rollout: keep base meta, extend the timeline
                    prev = merged
                    offset = (prev["session"].get("updated_at") or 0)
                    for e in r["events"]:
                        e["sid"] = sid
                        if e.get("ts") and prev["session"].get("started_at") \
                                and e["ts"] < prev["session"]["started_at"]:
                            e["seq"] += 10_000_000  # keep ordering sane
                    prev["events"].extend(r["events"])
                    if (r["session"].get("updated_at") or 0) > (prev["session"].get("updated_at") or 0):
                        prev["session"]["updated_at"] = r["session"]["updated_at"]
                    prev["session"]["raw_metadata"].setdefault(
                        "continuations", []
                    ).append(f.name)
                    merged["extra_sources"].append(f)
            if merged:
                s = merged["session"]
                for e in merged["events"]:
                    e["sid"] = sid
                s["message_count"] = sum(1 for e in merged["events"]
                                         if e["kind"] in ("user", "assistant"))
                s["tool_count"] = sum(1 for e in merged["events"]
                                      if e["kind"] == "tool_call")
                s["updated_at"] = max((e["ts"] for e in merged["events"] if e.get("ts")),
                                      default=s.get("started_at"))
                out.append(merged)
        return out

    def parse(self, source: Path) -> Optional[dict]:
        session: Dict[str, Any] = None
        events: List[dict] = []
        title: Optional[str] = None
        model: Optional[str] = None
        usage_total: Dict[str, int] = {}
        first_user: Optional[str] = None
        first_clean: Optional[str] = None

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
                payload = row.get("payload") or {}
                ts = parse_ts(row.get("timestamp"))

                if typ == "session_meta":
                    p = payload
                    session = new_session(
                        provider=self.provider,
                        native_session_id=p.get("session_id") or p.get("id"),
                        started_at=ts,
                        updated_at=ts,
                        cwd=p.get("cwd"),
                        model=p.get("model_provider"),
                        raw_metadata={
                            k: p.get(k)
                            for k in ("originator", "source", "cli_version",
                                      "model_provider", "history_base",
                                      "git", "timestamp")
                            if p.get(k) is not None
                        },
                    )
                    g = p.get("git") or {}
                    session["git_commit"] = g.get("commit_hash")
                    session["git_branch"] = g.get("branch")
                    session["git_remote"] = g.get("repository_url")
                    continue

                if session is None:
                    # tolerate truncated files that lost their meta line
                    session = new_session(provider=self.provider,
                                          native_session_id=source.stem)

                def ev(**kw):
                    e = new_event(sid=session["id"], ts=ts, seq=seq,
                                  raw_event=row, **kw)
                    events.append(e)
                    return e

                ptyp = payload.get("type")
                if typ == "response_item" and ptyp == "message":
                    role = payload.get("role") or "assistant"
                    text = text_of(payload.get("content"))
                    if role == "user" and text:
                        if first_user is None:
                            first_user = text
                        if first_clean is None:
                            first_clean = _clean_title(text) or None
                    if role in ("user", "assistant"):
                        ev(kind=role, role=role, content=text)
                elif typ == "response_item" and ptyp == "reasoning":
                    summary = text_of(payload.get("summary"))
                    ev(kind="reasoning", role="assistant",
                       content=summary or None,
                       model=payload.get("model"))
                elif typ in ("response_item",) and ptyp in (
                    "function_call", "custom_tool_call",
                ):
                    name = payload.get("name") or "unknown"
                    args = payload.get("arguments") or payload.get("input")
                    parsed = None
                    if isinstance(args, str):
                        try:
                            parsed = json.loads(args)
                        except json.JSONDecodeError:
                            parsed = None
                    cmd = _command_from_args(name, parsed) if parsed else (
                        args if isinstance(args, str) and name == "exec" else None
                    )
                    ev(kind="tool_call", role="assistant", tool_name=name,
                       tool_call_id=payload.get("call_id"),
                       tool_input=json.dumps(parsed, ensure_ascii=False)
                       if parsed is not None else (args if isinstance(args, str) else None),
                       command=cmd)
                elif typ in ("response_item",) and ptyp in (
                    "function_call_output", "custom_tool_call_output",
                ):
                    rc, body = _extract_output(payload.get("output"))
                    ev(kind="tool_result", role="tool",
                       tool_call_id=payload.get("call_id"),
                       tool_output=body or None, exit_code=rc)
                elif typ == "response_item" and ptyp == "web_search_call":
                    action = payload.get("action") or {}
                    ev(kind="tool_call", role="assistant",
                       tool_name="web_search", tool_input=json.dumps(action, ensure_ascii=False))
                elif typ == "turn_context":
                    if payload.get("model"):
                        model = payload["model"]
                    if payload.get("cwd") and not session.get("cwd"):
                        session["cwd"] = payload["cwd"]
                elif typ == "token_usage_record":
                    u = (payload.get("usage") or {})
                    for k in ("input", "cached_input", "cache_write_input",
                              "output", "reasoning_output", "total"):
                        if u.get(k):
                            usage_total[k] = usage_total.get(k, 0) + u[k]
                elif typ == "compacted":
                    ev(kind="meta", role="system",
                       content=payload.get("message") or "conversation compacted")

        if session is None:
            return None

        native = session.get("native_session_id") or source.stem
        session["id"] = f"codex:{native}"
        for e in events:
            e["sid"] = session["id"]
        session["title"] = first_clean or native
        session["model"] = model or session.get("model")
        session["updated_at"] = max(
            (e["ts"] for e in events if e.get("ts")), default=session.get("started_at")
        )
        session["message_count"] = sum(
            1 for e in events if e["kind"] in ("user", "assistant")
        )
        session["tool_count"] = sum(
            1 for e in events if e["kind"] == "tool_call"
        )
        session["can_resume"] = True
        session["can_fork"] = True
        session["resume_cmd"] = f"codex resume {native}"
        session["metadata"] = {
            "usage_totals": usage_total or None,
            "originator": session["raw_metadata"].get("originator"),
        }
        git = git_info(session.get("cwd"))
        return {
            "session": finish_session(session, git),
            "events": events,
            "extra_sources": [],
        }


register(CodexAdapter())
