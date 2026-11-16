"""WorkBuddy adapter — read-only JSONL session indexing.

Source: ``~/.workbuddy-ai/projects/<project>/<sessionId>.jsonl``.  Tests and
operators can point at another tree with ``VOYAGER_WORKBUDDY_PROJECTS``; when
that variable is set it is authoritative, so a test can never accidentally
walk into the real profile.

What was verified from a local WorkBuddy desktop install without reading any
conversation text:

* ``workbuddy.db`` is an application metadata database (``sessions``,
  ``workspaces``, ``session_usage``, automations).  It has no message table.
* Conversation turns live in per-session JSONL files under
  ``~/.workbuddy-ai/projects/<project>/``.  The file stem is the
  ``sessionId`` carried by every record.
* Records are JSON objects with ``type`` and millisecond ``timestamp``.  The
  observed types are ``message`` (role user/assistant), ``reasoning``,
  ``function_call``, ``function_call_result``, ``session-meta``, ``ai-title``,
  ``resend-fork-notice`` and ``file-history-snapshot``.
* Message text is a list of ``{type: input_text|output_text, text: str}``
  blocks.  Tool arguments are a JSON string; tool output is an object or list.

What this adapter deliberately does not do:

* No native resume, hook, context injection or transcript writing.  Reading a
  session is not the same as handing it back to WorkBuddy.
* No writes to WorkBuddy storage.  Files are opened read-only; companion files
  are only used as freshness sources.
* No guessing.  A malformed line is skipped, an unrecognised file is named as
  unreadable/unknown, and a missing result is left NULL rather than invented.

Verification level: UNIT_VERIFIED on synthetic fixtures.  The local install
was used only for structural reconnaissance (paths, DB tables, JSON keys and
value types), never as evidence that live continuation works.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .base import Adapter, finish_session, git_info, register
from ..model import new_event, new_session, text_of

_ENV_OVERRIDE = "VOYAGER_WORKBUDDY_PROJECTS"
_DEFAULT_RELATIVE = Path(".workbuddy-ai") / "projects"

PROBE_ABSENT = "absent"
PROBE_OK = "ok"
PROBE_UNREADABLE = "unreadable"
PROBE_UNKNOWN_SCHEMA = "unknown-schema"

_PROBE_LINES = 32


def _resolve_home(home: Optional[Path] = None) -> Path:
    if home is not None:
        return Path(home)
    for name in ("HOME", "USERPROFILE"):
        value = os.environ.get(name)
        if value:
            return Path(value)
    return Path.home()


def projects_dir(home: Optional[Path] = None) -> Path:
    """Directory containing per-project JSONL session files.

    The explicit override wins completely.  That is important for isolation:
    a test that sets it must not read the real ``~/.workbuddy-ai`` tree even
    when that tree exists.
    """
    value = os.environ.get(_ENV_OVERRIDE)
    if value:
        return Path(value).expanduser()
    return _resolve_home(home) / _DEFAULT_RELATIVE


def candidate_files(projects: Optional[Path] = None) -> List[Path]:
    """Ordered JSONL candidates. Never creates a directory or follows links."""
    root = Path(projects) if projects is not None else projects_dir()
    try:
        return sorted(p for p in root.glob("*/*.jsonl") if p.is_file())
    except OSError:
        return []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ms(value: Any) -> Optional[float]:
    """WorkBuddy timestamps are epoch milliseconds; accept seconds too."""
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n / 1000.0 if n >= 100_000_000_000 else n


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _block_text(content: Any) -> str:
    """Plain text from WorkBuddy's content-block list, without metadata noise."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return text_of(content).strip()
    parts: List[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        value = block.get("text")
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (dict, list)):
            parts.append(text_of(value))
    return "\n".join(p for p in parts if p).strip()


def _provider_data(record: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict(record.get("providerData"))


def _model(record: Dict[str, Any]) -> Optional[str]:
    pd = _provider_data(record)
    for key in ("requestModelName", "model", "requestModelId"):
        value = pd.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _command_and_path(arguments: Any) -> Tuple[Optional[str], Optional[str]]:
    data = _as_dict(_json(arguments))
    command = None
    for key in ("command", "cmd", "script"):
        if isinstance(data.get(key), str):
            command = data[key]
            break
    path = None
    for key in ("file_path", "filePath", "path", "filename"):
        if isinstance(data.get(key), str):
            path = data[key]
            break
    return command, path


def _tool_input(arguments: Any) -> Optional[str]:
    if arguments is None:
        return None
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def _result_text(record: Dict[str, Any]) -> Optional[str]:
    output = record.get("output")
    if isinstance(output, dict) and "content" in output:
        text = _block_text(output.get("content"))
        if text:
            return text
    text = _block_text(output)
    if text:
        return text
    tool = _as_dict(_provider_data(record).get("toolResult"))
    return _block_text(tool.get("content")) or None


def _read_records(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("type"), str):
                yield record


def probe(path: Path) -> Tuple[str, Optional[str]]:
    """(verdict, native session id). Never raises."""
    if not path.is_file():
        return PROBE_ABSENT, None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(_PROBE_LINES):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                sid = record.get("sessionId")
                if isinstance(sid, str) and sid and isinstance(record.get("type"), str):
                    return PROBE_OK, sid
    except OSError:
        return PROBE_UNREADABLE, None
    return PROBE_UNKNOWN_SCHEMA, None


def _companions(path: Path) -> List[Path]:
    out: List[Path] = []
    for suffix in (".meta.json", ".file-rollback.ndjson"):
        companion = path.with_name(path.stem + suffix)
        if companion.is_file():
            out.append(companion)
    return out


class WorkbuddyAdapter(Adapter):
    provider = "workbuddy"
    can_resume = False
    can_fork = False

    def discover(self) -> List[Path]:
        return [p for p in candidate_files() if probe(p)[0] == PROBE_OK]

    def unusable_sources(self) -> List[Path]:
        """Files still on disk but not indexable this round (O2: retain)."""
        return [p for p in candidate_files()
                if probe(p)[0] in (PROBE_UNREADABLE, PROBE_UNKNOWN_SCHEMA)]

    def parse(self, source: Path) -> Optional[dict]:
        verdict, native = probe(source)
        if verdict != PROBE_OK or not native:
            return None

        # Fixed before any event is built: a later line may have a different
        # id (a resumed/forked transcript), but the source file is one session
        # artifact and every event must carry its stable sid.
        sid: Optional[str] = "workbuddy:%s" % native
        events: List[dict] = []
        timestamps: List[float] = []
        cwd: Optional[str] = None
        title: Optional[str] = None
        model: Optional[str] = None
        host_kind: Any = None
        first_user: Optional[str] = None
        seq = 0

        def add(kind: str, **kw) -> None:
            nonlocal seq
            seq += 1
            kw.setdefault("ts", None)
            events.append(new_event(sid=sid, seq=seq, kind=kind, **kw))

        for record in _read_records(source):
            if sid is None:
                native = record.get("sessionId")
                if isinstance(native, str) and native:
                    sid = "workbuddy:%s" % native
            ts = _ms(record.get("timestamp"))
            if ts is not None:
                timestamps.append(ts)
            if cwd is None and isinstance(record.get("cwd"), str) and record["cwd"]:
                cwd = record["cwd"]
            if model is None:
                model = _model(record)

            rtype = record.get("type")
            if rtype == "session-meta":
                host_kind = _as_dict(record.get("meta")).get("codebuddy.ai/hostKind")
                continue
            if rtype == "ai-title":
                candidate = record.get("aiTitle")
                if isinstance(candidate, str) and candidate.strip():
                    title = candidate.strip()
                continue
            if rtype == "message":
                role = record.get("role")
                text = _block_text(record.get("content"))
                pd = _provider_data(record)
                if role == "user":
                    if pd.get("isMeta") or "clientMeta" in pd:
                        if text:
                            add("meta", role="system", content=text, ts=ts,
                                origin="provider_system",
                                raw_event={"type": rtype, "role": role, "meta": True})
                    elif text:
                        if first_user is None:
                            first_user = text
                        add("user", role="user", content=text, ts=ts,
                            origin="human",
                            raw_event={"type": rtype, "role": role})
                elif role == "assistant" and text:
                    add("assistant", role="assistant", content=text, ts=ts,
                        model=model, origin="provider_system",
                        raw_event={"type": rtype, "role": role})
                continue
            if rtype == "reasoning":
                text = _block_text(record.get("rawContent")) or _block_text(
                    record.get("content"))
                if text:
                    add("reasoning", role="assistant", content=text, ts=ts,
                        model=model, origin="provider_system",
                        raw_event={"type": rtype})
                continue
            if rtype == "function_call":
                name = record.get("name")
                call_id = record.get("callId")
                arguments = record.get("arguments")
                command, file_path = _command_and_path(arguments)
                add("tool_call", role="assistant", ts=ts, model=model,
                    tool_name=str(name) if name else None,
                    tool_call_id=str(call_id) if call_id else None,
                    tool_input=_tool_input(arguments), command=command,
                    file_path=file_path, origin="provider_system",
                    raw_event={"type": rtype, "name": name, "callId": call_id})
                continue
            if rtype == "function_call_result":
                name = record.get("name")
                call_id = record.get("callId")
                add("tool_result", role="tool", ts=ts,
                    tool_name=str(name) if name else None,
                    tool_call_id=str(call_id) if call_id else None,
                    tool_output=_result_text(record), origin="provider_system",
                    raw_event={"type": rtype, "name": name, "callId": call_id,
                               "status": record.get("status")})
                continue
            # session-meta / ai-title / snapshots / fork notices carry no
            # conversational turn. They stay in the source, not in the index.

        if sid is None or (not events and not title):
            return None

        label = title or (first_user and " ".join(first_user.split())[:120]) or sid
        metadata: Dict[str, Any] = {"schema": "codebuddy-jsonl-v1"}
        if host_kind is not None:
            metadata["host_kind"] = host_kind
        session = new_session(
            id=sid,
            provider=self.provider,
            native_session_id=sid.split(":", 1)[1],
            title=label,
            started_at=min(timestamps) if timestamps else None,
            updated_at=max(timestamps) if timestamps else None,
            cwd=cwd,
            model=model,
            message_count=sum(1 for e in events
                              if e["kind"] in ("user", "assistant")),
            tool_count=sum(1 for e in events if e["kind"] == "tool_call"),
            can_resume=False,
            can_fork=False,
            resume_cmd=None,
            metadata=metadata,
            raw_metadata={"project": source.parent.name},
        )
        session = finish_session(session, git_info(cwd))
        return {"session": session, "events": events, "source_path": source,
                "extra_sources": _companions(source)}


register(WorkbuddyAdapter())
