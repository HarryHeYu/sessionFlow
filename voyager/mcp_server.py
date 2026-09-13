"""Voyager MCP server — exposes the unified agent-session index as MCP tools.

Run:  python -m voyager.mcp_server        (stdio transport)
      or the `voyager-mcp` console script.

Register with an agent, e.g.:
  Codex  (~/.codex/config.toml):
    [mcp_servers.voyager]
    command = "python"
    args = ["-m", "voyager.mcp_server"]
  Claude Code:
    claude mcp add --scope user voyager -- python -m voyager.mcp_server
"""

from __future__ import annotations

import json

try:
    # mcp >= 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x fallback
    from mcp.server.fastmcp import FastMCP as _Server

from .store import Store

mcp = _Server("voyager", instructions=(
    "Unified local index of the user's AI coding agent sessions across 8 "
    "platforms (Codex, Claude Code, ZCode, DSH, Grok, Cursor, Kiro, "
    "Antigravity). Use these tools whenever the user refers to past work, "
    "other agents, previous sessions, or needs context from earlier tasks."
))


def _row(r) -> dict:
    d = {k: r[k] for k in r.keys()}
    d["can_resume"] = bool(d.get("can_resume"))
    if d.get("metadata_json"):
        try:
            d["metadata"] = json.loads(d["metadata_json"])
        except json.JSONDecodeError:
            pass
    d.pop("metadata_json", None)
    d.pop("raw_metadata_json", None)
    return d


@mcp.tool()
def voyager_brief(hours: float = 48, limit: int = 10) -> str:
    """What have ALL my agents been doing recently? Compact digest of the
    last `hours` hours across every platform, newest first. Call this when
    the user asks about recent activity or past work in general."""
    import time
    store = Store()
    cutoff = time.time() - hours * 3600
    rows = [r for r in store.sessions() if (r["updated_at"] or 0) >= cutoff]
    rows.sort(key=lambda x: x["updated_at"] or 0, reverse=True)
    out = [f"{len(rows)} sessions active in the last {hours}h:"]
    for r in rows[:limit]:
        last_user = ""
        for e in reversed(store.events(r["id"])):
            if e["kind"] == "user" and e["content"]:
                last_user = " ".join(e["content"].split())[:160]
                break
        out.append(f"[{r['provider']}] {r['native_id']}  "
                   f"repo={r['repo_root'] or r['cwd']}  "
                   f"{r['message_count']} msgs / {r['tool_count']} tools\n"
                   f"  last user: {last_user or (r['title'] or '')[:160]}")
    store.close()
    return "\n".join(out)


@mcp.tool()
def voyager_search(query: str, limit: int = 10) -> str:
    """Full-text search across ALL agents' session content (messages, tool
    calls, commands, file paths). Chinese and English both work. Use when
    hunting for where something was said/done."""
    store = Store()
    try:
        rows = store.search(query, limit=limit)
    except Exception as e:
        store.close()
        return f"search error: {e}"
    out = []
    for r in rows:
        out.append(f"[{r['provider']}] {r['native_id']}  {r['title'] or ''}\n"
                   f"  …{(r['snippet'] or '').strip()[:200]}…")
    store.close()
    return "\n\n".join(out) or "no matches"


@mcp.tool()
def voyager_list(platform: str = "", repo: str = "", limit: int = 15) -> str:
    """List indexed sessions, newest first. Optionally filter by platform
    (codex/claude/zcode/dsh/grok/cursor/kiro/antigravity) or a repo/cwd
    substring."""
    store = Store()
    rows = store.sessions(platform or None)
    if repo:
        rows = [r for r in rows if repo.lower() in
                (r["repo_root"] or r["cwd"] or "").lower().replace("\\", "/")]
    out = []
    for r in rows[:limit]:
        out.append(f"[{r['provider']}] {r['native_id']}  "
                   f"({r['message_count']} msgs / {r['tool_count']} tools)\n"
                   f"  {(r['title'] or '')[:150]}")
    n = len(rows)
    store.close()
    return "\n".join(out) + f"\n\n({n} session(s) total)"


@mcp.tool()
def voyager_show(session_id: str, max_events: int = 60) -> str:
    """Full timeline of one session: user/assistant messages, reasoning,
    tool calls with commands and exit codes. Accepts a prefix of the id.
    Use after voyager_search/voyager_list to dive into a session."""
    store = Store()
    row, ambiguous = store.session(session_id)
    if row is None and ambiguous:
        store.close()
        return ("ambiguous id, candidates:\n" +
                "\n".join(f"  [{r['provider']}] {r['native_id']}  "
                          f"{(r['title'] or '')[:60]}"
                          for r in ambiguous[:10]))
    if row is None:
        store.close()
        return f"session not found: {session_id}"
    evs = store.events(row["id"])
    out = [f"[{row['provider']}] {row['native_id']}  "
           f"repo={row['repo_root'] or row['cwd']}  model={row['model']}"]
    for e in evs[:max_events]:
        kind = e["kind"]
        if kind in ("user", "assistant"):
            out.append(f"USER/ASSISTANT {kind}: {(e['content'] or '')[:600]}")
        elif kind == "tool_call":
            out.append(f"TOOL {e['tool_name']}: "
                       f"{(e['command'] or e['tool_input'] or '')[:300]}")
        elif kind == "tool_result":
            rc = f" exit={e['exit_code']}" if e["exit_code"] is not None else ""
            out.append(f"RESULT{rc}: {(e['tool_output'] or e['stdout'] or '')[:300]}")
        elif kind == "error":
            out.append(f"ERROR: {(e['content'] or '')[:300]}")
    if len(evs) > max_events:
        out.append(f"... ({len(evs) - max_events} more events; "
                   f"increase max_events to see more)")
    store.close()
    return "\n\n".join(out)


@mcp.tool()
def voyager_handoff(session_id: str, target: str = "claude") -> str:
    """Generate a Context Package (goal, instructions, files, commands,
    errors, where work stopped) from a session, for handing the task to a
    DIFFERENT agent. Returns the package file path — the target agent
    should read that file and continue the task."""
    from .handoff import build_context_package, default_package_name
    from pathlib import Path
    store = Store()
    row, ambiguous = store.session(session_id)
    if row is None and ambiguous:
        store.close()
        return ("ambiguous id, candidates:\n" +
                "\n".join(f"  [{r['provider']}] {r['native_id']}"
                          for r in ambiguous[:10]))
    if row is None:
        store.close()
        return f"session not found: {session_id}"
    out = Path(default_package_name(row))
    out.write_text(build_context_package(store, row), encoding="utf-8")
    store.close()
    return (f"Context package written to {out.resolve()}. "
            f"Target agent ({target}) should read this file and continue "
            f"the task described inside.")


if __name__ == "__main__":
    mcp.run()
