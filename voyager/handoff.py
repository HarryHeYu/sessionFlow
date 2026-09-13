"""Cross-agent handoff: export a session as a Context Package and seed a
new session in another agent with it.

The package is a self-contained Markdown file. Target agents are launched
with a short prompt that points at the file ("read it and continue") — this
avoids command-line length limits and shell-quoting pitfalls, and works for
every agentic CLI that can read a file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .store import Store

# targets that accept an initial prompt and can read a file themselves
PROMPT_TARGETS = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
}
HANDOFF_INSTRUCTION = (
    "The file below is a handoff context package exported from a previous "
    "AI agent session (a different tool). Read it fully, then pick up the "
    "task: honor the goal, decisions and constraints, keep working in the "
    "same repository, and do not redo work that is already done."
)

_USER_MAX = 1200       # per user message
_ASST_MAX = 1500       # per assistant message (only the last few are kept)
_CMD_MAX = 300
_MAX_COMMANDS = 25
_MAX_ERRORS = 10
_MAX_FILES = 60


def _fmt_ts(ts) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def build_context_package(store: Store, srow) -> str:
    events = store.events(srow["id"])
    meta = json.loads(srow["metadata_json"] or "{}")

    L: List[str] = []
    L.append("# Agent Handoff — Context Package")
    L.append("")
    L.append(f"- Source: **{srow['provider']}** session `{srow['native_id']}`")
    L.append(f"- Title: {srow['title'] or '?'}")
    L.append(f"- Time: {_fmt_ts(srow['started_at'])} → {_fmt_ts(srow['updated_at'])}")
    if srow["repo_root"]:
        L.append(f"- Repository: `{srow['repo_root']}`"
                 + (f" (branch `{srow['git_branch']}`" if srow["git_branch"] else " (")
                 + (f", commit `{srow['git_commit'][:12]}`" if srow["git_commit"] else "")
                 + (")" if (srow["git_branch"] or srow["git_commit"]) else ""))
    if srow["git_remote"]:
        L.append(f"- Remote: {srow['git_remote']}")
    L.append(f"- CWD: `{srow['cwd'] or '?'}`")
    if srow["model"]:
        L.append(f"- Model: {srow['model']}")
    if meta.get("usage_totals"):
        L.append(f"- Usage: `{json.dumps(meta['usage_totals'], ensure_ascii=False)}`")
    L.append("")
    L.append("> Note: repository state may have moved on since this session. "
             "Re-check `git status` / the files below before editing.")
    L.append("")

    # ---- goal: the earliest substantive user messages -------------------
    user_msgs = [e for e in events if e["kind"] == "user" and e["content"]]
    L.append("## User goal / instructions")
    L.append("")
    if user_msgs:
        L.append("**Original request:**")
        L.append("")
        L.append(user_msgs[0]["content"][: _USER_MAX * 2])
        L.append("")
        if len(user_msgs) > 1:
            L.append("**Follow-up instructions (in order):**")
            L.append("")
            for e in user_msgs[1:]:
                L.append(f"- [{_fmt_ts(e['ts'])}] {e['content'][:_USER_MAX]}")
            L.append("")
    else:
        L.append("(no user messages captured)")
        L.append("")

    # ---- last assistant state ------------------------------------------
    asst = [e for e in events if e["kind"] == "assistant" and e["content"]]
    if asst:
        L.append("## Where the work stopped (last assistant message)")
        L.append("")
        L.append(asst[-1]["content"][:_ASST_MAX])
        L.append("")

    # ---- files ----------------------------------------------------------
    files: List[str] = []
    for e in events:
        if e["file_path"]:
            files.append(e["file_path"])
        if e["files_json"]:
            try:
                files.extend(json.loads(e["files_json"]))
            except json.JSONDecodeError:
                pass
        if e["kind"] == "file" and e["file_path"]:
            pass
    seen = set()
    files = [f for f in files if not (f in seen or seen.add(f))][:_MAX_FILES]
    if files:
        L.append("## Files this session touched")
        L.append("")
        for f in files:
            L.append(f"- `{f}`")
        L.append("")

    # ---- commands -------------------------------------------------------
    cmds = [(e["ts"], e["command"], e["exit_code"]) for e in events
            if e["kind"] == "tool_call" and e["command"]]
    if cmds:
        L.append("## Commands executed")
        L.append("")
        for ts, cmd, rc in cmds[-_MAX_COMMANDS:]:
            rc_s = f"  # exit {rc}" if rc is not None else ""
            one = " ".join(str(cmd).split())[:_CMD_MAX]
            L.append(f"- `{one}`{rc_s}")
        L.append("")

    # ---- errors ---------------------------------------------------------
    errs = [e for e in events if e["kind"] == "error" and e["content"]]
    bad = [(e["ts"], e["command"]) for e in events
           if e["kind"] == "tool_call" and (e["exit_code"] or 0) not in (0, None)
           and e["command"]]
    if errs or bad:
        L.append("## Errors encountered")
        L.append("")
        for e in errs[:_MAX_ERRORS]:
            L.append(f"- [{_fmt_ts(e['ts'])}] {(e['content'] or '')[:300]}")
        for ts, cmd in bad[-_MAX_ERRORS:]:
            L.append(f"- [{_fmt_ts(ts)}] non-zero exit: `{' '.join(str(cmd).split())[:200]}`")
        L.append("")

    # ---- recent timeline ------------------------------------------------
    L.append("## Recent timeline (condensed)")
    L.append("")
    shown = 0
    for e in reversed(events):
        if shown >= 30:
            break
        if e["kind"] == "user" and e["content"]:
            L.append(f"- **User** [{_fmt_ts(e['ts'])}]: {e['content'][:300]}")
            shown += 1
        elif e["kind"] == "assistant" and e["content"]:
            L.append(f"- **Assistant** [{_fmt_ts(e['ts'])}]: {e['content'][:200]}")
            shown += 1
        elif e["kind"] == "tool_call" and e["command"]:
            L.append(f"- _tool_ `{e['tool_name']}`: {' '.join(e['command'].split())[:150]}")
            shown += 1
    if not shown:
        L.append("(no timeline events)")
    L.append("")

    return "\n".join(L)


def handoff_command(target: str, package_path: Path) -> Optional[List[str]]:
    """Build the argv that seeds a new session in the target agent."""
    exe = PROMPT_TARGETS.get(target)
    if not exe:
        return None
    return [
        exe,
        f"{HANDOFF_INSTRUCTION}\n\nHandoff file: {package_path.resolve()}",
    ]


def default_package_name(srow) -> str:
    native = (srow["native_id"] or "session")[:24]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in native)
    return f"handoff-{srow['provider']}-{safe}.md"
