"""Voyager MCP server — exposes the unified agent-session index as MCP tools.

Run:  python -m voyager.mcp_server        (stdio transport)
      or the `voyager-mcp` console script (installed with `pip install -e ".[mcp]"`).

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

_MISSING_MCP = (
    "voyager: the MCP server needs the optional 'mcp' package.\n"
    "  pip install -e \".[mcp]\"     (from a checkout)\n"
    "  pip install \"voyager[mcp]\"   (from PyPI)\n"
    "Core CLI commands (scan/list/show/search/export/handoff) work without it."
)

class _UnavailableServer:
    """Stand-in server used when the optional `mcp` extra is not installed.

    Keeps the module importable (the tool functions below still exist, so
    tests and introspection work) and refuses to start with the install hint
    instead of a bare ModuleNotFoundError — both for
    `python -m voyager.mcp_server` and for the `voyager-mcp` console script.
    """

    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        def decorate(fn):
            return fn
        return decorate

    def run(self):
        raise SystemExit(_MISSING_MCP)


try:
    # mcp >= 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - mcp 1.x fallback
    try:
        from mcp.server.fastmcp import FastMCP as _Server
        MCP_AVAILABLE = True
    except ImportError:
        MCP_AVAILABLE = False
        _Server = _UnavailableServer      # type: ignore[assignment]

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
def voyager_handoff(session_id: str, target: str = "claude",
                    goal: str = "") -> str:
    """Generate a Context Package (goal, instructions, files, commands,
    errors, where work stopped) from a session, for handing the task to a
    DIFFERENT agent. Returns the package file path — the target agent
    should read that file and continue the task."""
    from pathlib import Path
    from .continuity import get_bundles_dir
    from .handoff import build_context_package, default_package_name
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
    out = get_bundles_dir() / default_package_name(row)
    out.write_text(build_context_package(store, row), encoding="utf-8")
    store.close()
    return (f"Context package written to {out.resolve()}. "
            f"Target agent ({target}) should read this file and continue "
            f"the task described inside.")


@mcp.tool()
def voyager_merge(session_ids: list[str], target: str = "claude", goal: str = "") -> str:
    """Synthesize multiple sessions (from any agents) into a single Continuation
    Bundle, resolving chronological conflicts, deduplicating files/commands/errors,
    and capturing live git state. Returns the bundle file path — the target agent
    should read that file and continue work."""
    from pathlib import Path
    from .continuity import build_continuation_bundle, default_bundle_name, get_bundles_dir
    store = Store()
    rows = []
    for sid in session_ids:
        row, ambiguous = store.session(sid)
        if row is None and ambiguous:
            store.close()
            return f"ambiguous id '{sid}', candidates: " + ", ".join(r['native_id'] for r in ambiguous[:5])
        if row is None:
            store.close()
            return f"session not found: {sid}"
        rows.append(row)
    if not rows:
        store.close()
        return "error: no valid sessions provided"
    out_dir = get_bundles_dir()
    out = out_dir / default_bundle_name(rows)
    bundle = build_continuation_bundle(store, rows, goal=goal or None)
    out.write_text(bundle, encoding="utf-8")
    store.close()
    return (f"Continuation bundle written to {out.resolve()} ({len(bundle)} chars). "
            f"Target agent ({target}) should read this file and continue the task.")


@mcp.tool()
def voyager_thread_list(status: str = "active") -> str:
    """List WorkThreads (task-centric groups of sessions that span agents).
    Use when the user refers to a task/thread rather than a single session."""
    store = Store()
    rows = store.thread_list(status)
    store.close()
    if not rows:
        return f"no {status} threads"
    out = []
    for t in rows:
        out.append("{0}  [{1}]  members:{2}  repo: {3}\n    {4}".format(
            t["id"], t["status"], t["members"], t["repo_root"] or "?",
            (t["title"] or "")[:150]))
    return "\n".join(out)


@mcp.tool()
def voyager_thread_show(thread_id: str) -> str:
    """One WorkThread in detail: status, repo, goal, writer lease state and
    its member sessions (provider, message counts, titles). Accepts a
    prefix of the thread id."""
    from .store import lease_state
    store = Store()
    t = store.thread_get(thread_id)
    if not t:
        store.close()
        return f"thread not found: {thread_id}"
    members = store.thread_members(t["id"])
    lease = store.thread_lease_get(t["id"])
    st = lease_state(lease)
    if st["held"]:
        lease_line = ("held by {0} pid={1}".format(
            lease["holder"], lease["pid"]))
        if st["expired"]:
            lease_line += " (EXPIRED: " + st["why"] + ")"
    else:
        lease_line = "free"
    out = ["Thread {0}  [{1}]".format(t["id"], t["status"]),
           "repo: " + (t["repo_root"] or "?"),
           "title: " + (t["title"] or "?")]
    if t["goal"]:
        out.append("goal: " + t["goal"])
    out.append("lease: " + lease_line)
    out.append("members:")
    for m in members:
        out.append("  {0:<8} {1}  ({2} msgs)  {3}".format(
            m["provider"], m["native_id"], m["message_count"],
            (m["title"] or "")[:80]))
    store.close()
    return "\n".join(out)


@mcp.tool()
def voyager_thread_attach(thread_id: str, session_ids: list[str]) -> str:
    """Attach sessions (any agents) to a WorkThread. Duplicates are
    ignored; unknown ids are reported. Returns per-id results."""
    store = Store()
    t = store.thread_get(thread_id)
    if not t:
        store.close()
        return f"thread not found: {thread_id}"
    results = []
    for ref in session_ids:
        row, ambiguous = store.session(ref)
        if row is None and ambiguous:
            results.append(f"  ! {ref}: ambiguous ({len(ambiguous)} matches)")
            continue
        if row is None:
            results.append(f"  ! {ref}: not found")
            continue
        ok = store.thread_attach(t["id"], row["id"])
        results.append(("  + attached " if ok else "  = already in ") +
                       f"{row['id']}")
    store.close()
    return "\n".join(results)


@mcp.tool()
def voyager_thread_close(thread_id: str) -> str:
    """Mark a WorkThread closed. Sessions are never deleted."""
    store = Store()
    t = store.thread_get(thread_id)
    if not t:
        store.close()
        return f"thread not found: {thread_id}"
    store.thread_set_status(t["id"], "closed")
    store.close()
    return f"thread {t['id']} closed (sessions untouched)"


@mcp.tool()
def voyager_current(cwd: str = "", provider: str = "",
                    native_session_id: str = "") -> str:
    """Which WorkThread covers the current repo? What's the lease, holder,
    pending attach and recommended action? Call this at session start."""
    from .auto import discover_continuity
    store = Store()
    disc = discover_continuity(store, cwd=cwd or None, provider=provider or None,
                               native_session_id=native_session_id or None)
    store.close()
    if not disc["continuity_available"]:
        return "no active WorkThread for this repo"
    return json.dumps(disc, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def voyager_context(cwd: str = "", provider: str = "",
                    native_session_id: str = "", goal: str = "",
                    budget: str = "") -> str:
    """Compile a ready-to-use continuation context (goal, state, decisions,
    failures, next steps) for the active WorkThread. Returns the bundle
    text — read it and continue the work."""
    from .auto import get_continuation_context
    store = Store()
    res = get_continuation_context(store=store, cwd=cwd or None,
                                   provider=provider or None,
                                   native_session_id=native_session_id or None,
                                   goal=goal or None, budget=budget or None)
    if not res["continuity_available"]:
        return "no active WorkThread — nothing to continue"
    return res["context"]


@mcp.tool()
def voyager_continue(cwd: str = "", goal: str = "", budget: str = "") -> str:
    """Discover the active WorkThread for the current repo, compile a
    continuation bundle, and return it. Same as voyager_context but with
    automatic session discovery — no manual session ids needed."""
    return voyager_context(cwd=cwd, provider="", native_session_id="",
                           goal=goal, budget=budget)


@mcp.tool()
def voyager_switch(target: str, cwd: str = "", goal: str = "",
                   budget: str = "", steal: bool = False) -> str:
    """Switch the active WorkThread to another agent. Compiles the
    continuation bundle (or native resume if same provider). Returns
    instructions for the target agent."""
    from .auto import discover_continuity
    from .continuity import handoff_thread
    store = Store()
    disc = discover_continuity(store, cwd=cwd or None)
    if not disc["continuity_available"]:
        store.close()
        return "no active WorkThread — nothing to switch"
    tid = disc["active_thread"]["id"]
    t = store.thread_get(tid)
    
    res = handoff_thread(
        store=store,
        thread=t,
        target=target,
        goal=goal or None,
        budget=budget or None,
        launch=False,  # MCP returns text, doesn't launch
        mode="bundle",
        steal=steal,
        no_launch=True,
    )
    store.close()
    
    if res["action"] == "refused":
        return "refused: {0}".format(res.get("error", "unknown"))
    if res["warnings"]:
        warnings = "\n".join(["warning: {0}".format(w) for w in res["warnings"]])
    else:
        warnings = ""
    
    if res["action"] == "native-resume":
        return ("same provider ({0}) — native resume ready:\n\n{1}".format(
            target, "\n".join(res["argv"]))) + ("\n\n" + warnings if warnings else "")
    elif res["action"] == "transcript":
        return ("transplant written; session id {0}.\n\n{1}".format(
            res.get("native_session_id", "unknown"), warnings))
    elif res["action"] == "bundle":
        return ("Continuation bundle ready: {0}\n\n{1}{2} Target agent ({3}) should "
                "read the bundle and continue.".format(
                    res["bundle_path"],
                    warnings,
                    "\nestimated_tokens is available in the bundle," if "context" in locals() else "",
                    target))
    else:
        return "unexpected action: {0}".format(res["action"])


def main() -> None:
    if not MCP_AVAILABLE:
        raise SystemExit(_MISSING_MCP)
    mcp.run()


if __name__ == "__main__":
    main()
