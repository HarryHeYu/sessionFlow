"""Voyager core models.

Adapters produce plain dicts following these shapes; the store persists them
as-is (normalized columns + raw_json). Provider-specific fields never get
dropped: they ride along in `metadata` / `raw_event`.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Session (normalized). All time fields are epoch seconds (float).
# ---------------------------------------------------------------------------
SESSION_FIELDS = [
    "id",                # voyager id: f"{provider}:{native_session_id}"
    "provider",          # codex | claude | zcode | dsh | grok | ...
    "native_session_id",
    "title",
    "started_at",
    "updated_at",
    "cwd",
    "repo_root",
    "git_remote",
    "git_branch",
    "git_commit",
    "model",
    "message_count",
    "tool_count",
    "can_resume",
    "can_fork",
    "resume_cmd",        # None if unsupported
    "metadata",          # dict -> metadata_json
    "raw_metadata",      # dict -> raw_metadata_json (provider-native meta)
]

EVENT_FIELDS = [
    "sid",               # voyager session id
    "ts",                # epoch seconds float; None if provider lacks it
    "seq",               # provider-native ordering (line number / ordinal)
    "kind",              # user|assistant|reasoning|tool_call|tool_result|
                         # usage|error|snapshot|meta|file
    "role",
    "content",           # message text / output body
    "tool_name",
    "tool_call_id",
    "tool_input",
    "tool_output",
    "command",
    "stdout",
    "stderr",
    "exit_code",
    "file_path",
    "old_content",
    "new_content",
    "diff",
    "files",             # list of file paths touched by this event
    "model",
    "usage",             # dict {input,output,cache_read,cache_write,cost,...}
    "raw_event",         # provider-native event (dict); store truncates huge blobs
]

EVENT_KINDS = {
    "user", "assistant", "reasoning", "tool_call", "tool_result",
    "usage", "error", "snapshot", "meta", "file",
}


def new_event(**kw) -> Dict[str, Any]:
    ev = {k: None for k in EVENT_FIELDS}
    for k, v in kw.items():
        if k not in ev:
            raise KeyError(f"unknown event field: {k}")
        ev[k] = v
    if ev["kind"] not in EVENT_KINDS:
        raise ValueError(f"bad event kind: {ev['kind']}")
    return ev


def new_session(**kw) -> Dict[str, Any]:
    s = {k: None for k in SESSION_FIELDS}
    s["message_count"] = 0
    s["tool_count"] = 0
    s["can_resume"] = False
    s["can_fork"] = False
    s["metadata"] = {}
    s["raw_metadata"] = {}
    for k, v in kw.items():
        if k not in s:
            raise KeyError(f"unknown session field: {k}")
        s[k] = v
    return s


def text_of(content: Any) -> str:
    """Best-effort plain text extraction from the zoo of content shapes."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                for key in ("text", "thinking", "summary_text", "content"):
                    v = b.get(key)
                    if isinstance(v, str):
                        parts.append(v)
                        break
                else:
                    parts.append(text_of(b))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        for key in ("text", "content", "message"):
            v = content.get(key)
            if isinstance(v, str):
                return v
        return ""
    return str(content)
