"""Markdown / JSON export of a normalized session."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List

from .store import Store


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _md_escape(s) -> str:
    return (s or "").replace("\r\n", "\n")


def export_markdown(store: Store, srow, events) -> str:
    meta = json.loads(srow["metadata_json"] or "{}")
    lines: List[str] = []
    lines.append(f"# {srow['title'] or srow['id']}")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- **Provider**: {srow['provider']}")
    lines.append(f"- **Session ID**: `{srow['native_id']}`")
    lines.append(f"- **Voyager ID**: `{srow['id']}`")
    lines.append(f"- **Started**: {_fmt_ts(srow['started_at'])}")
    lines.append(f"- **Updated**: {_fmt_ts(srow['updated_at'])}")
    lines.append(f"- **Model**: {srow['model'] or '?'}")
    lines.append(f"- **CWD**: `{srow['cwd'] or '?'}`")
    if srow["repo_root"]:
        lines.append(f"- **Repo**: `{srow['repo_root']}`"
                     + (f" (branch: {srow['git_branch']}" if srow["git_branch"] else " (")
                     + (f", commit: {srow['git_commit'][:12]}" if srow["git_commit"] else "")
                     + (")" if (srow['git_branch'] or srow['git_commit']) else ""))
    if srow["git_remote"]:
        lines.append(f"- **Remote**: {srow['git_remote']}")
    lines.append(f"- **Messages**: {srow['message_count']} · **Tool calls**: {srow['tool_count']}")
    if meta.get("usage_totals"):
        lines.append(f"- **Usage**: `{json.dumps(meta['usage_totals'], ensure_ascii=False)}`")
    if srow["resume_cmd"]:
        lines.append(f"- **Resume**: `{srow['resume_cmd']}`")
    lines.append("")

    lines.append("## Conversation")
    lines.append("")
    open_file_blocks = []   # files modified (from old/new or snapshots)
    for ev in events:
        t = _fmt_ts(ev["ts"])
        kind = ev["kind"]
        if kind == "user":
            lines.append(f"### 🧑 User — {t}")
            lines.append("")
            lines.append(_md_escape(ev["content"]))
            lines.append("")
        elif kind == "assistant":
            lines.append(f"### 🤖 Assistant — {t}")
            lines.append("")
            lines.append(_md_escape(ev["content"]))
            lines.append("")
        elif kind == "reasoning":
            lines.append("<details><summary>💭 Reasoning</summary>")
            lines.append("")
            lines.append(_md_escape(ev["content"]))
            lines.append("")
            lines.append("</details>")
            lines.append("")
        elif kind == "tool_call":
            head = f"`{ev['tool_name']}`"
            if ev["command"]:
                lines.append(f"### 🔧 {head} — {t}")
                lines.append("")
                lines.append("```")
                lines.append(_md_escape(ev["command"]))
                lines.append("```")
                lines.append("")
            elif ev["tool_input"]:
                lines.append(f"### 🔧 {head} — {t}")
                lines.append("")
                lines.append("```json")
                lines.append(_md_escape(ev["tool_input"]))
                lines.append("```")
                lines.append("")
        elif kind == "tool_result":
            body = ev["tool_output"] or ev["stdout"] or ""
            rc = f" (exit {ev['exit_code']})" if ev["exit_code"] is not None else ""
            lines.append(f"<details><summary>📤 Result{rc}</summary>")
            lines.append("")
            lines.append("```")
            lines.append(_md_escape(body[:20000]))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")
        elif kind == "snapshot":
            try:
                snap_files = json.loads(ev["files_json"] or "[]")
            except json.JSONDecodeError:
                snap_files = []
            for p in snap_files:
                open_file_blocks.append(p)
        elif kind == "file" and ev["file_path"]:
            open_file_blocks.append(ev["file_path"])
        elif kind == "error":
            lines.append(f"### ❌ Error — {t}")
            lines.append("")
            lines.append(_md_escape(ev["content"]))
            lines.append("")

    if open_file_blocks:
        lines.append("## Files Touched")
        lines.append("")
        for p in dict.fromkeys(open_file_blocks):
            lines.append(f"- `{p}`")
        lines.append("")

    return "\n".join(lines)


def export_json(store: Store, srow, events) -> str:
    out = {
        "session": {k: srow[k] for k in srow.keys()},
        "events": [],
    }
    out["session"]["metadata"] = json.loads(srow["metadata_json"] or "{}")
    out["session"]["raw_metadata"] = json.loads(srow["raw_metadata_json"] or "{}")
    for ev in events:
        d = {k: ev[k] for k in ev.keys()}
        d["files"] = json.loads(ev["files_json"] or "[]")
        d["usage"] = json.loads(ev["usage_json"] or "null")
        try:
            d["raw_event"] = json.loads(ev["raw_json"]) if ev["raw_json"] else None
        except json.JSONDecodeError:
            d["raw_event"] = ev["raw_json"]
        d.pop("files_json"); d.pop("usage_json"); d.pop("raw_json")
        out["events"].append(d)
    return json.dumps(out, ensure_ascii=False, indent=2)


def write_export(store: Store, srow, path: str, fmt: str) -> str:
    events = store.events(srow["id"])
    if fmt == "json":
        content = export_json(store, srow, events)
    else:
        content = export_markdown(store, srow, events)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
