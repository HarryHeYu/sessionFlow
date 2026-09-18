"""Continuity Engine: compile and synthesize multiple agent sessions into a
coherent Continuation Bundle.

Principles (ROADMAP.md):
1. Work continuation, not session migration.
2. Goal-directed extraction, not history dump.
3. Pointer over prose.
4. Synthesis, not concat (older assistant conclusions are marked superseded).
5. Provenance on every claim (session ids, timestamps).
6. Deterministic compiler in core (no LLM, zero dependencies).
7. One core, many fronts (CLI, MCP, Skill).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .store import Store
from .ranker import extract_candidate_facts, rank_candidates

PROMPT_TARGETS = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
}

CONTINUATION_INSTRUCTION = (
    "The file below is a continuation context bundle compiled from previous "
    "AI agent sessions. Read it fully, then pick up the task: honor the "
    "goal, current verified state, decisions and constraints, keep working in "
    "the same repository, and do not redo work that is already done."
)

_USER_MAX = 1200
_ASST_MAX = 1500
_CMD_MAX = 300
_MAX_COMMANDS = 30
_MAX_ERRORS = 15
_MAX_FILES = 80


def get_bundles_dir() -> Path:
    """Return ~/.voyager/bundles/ (created if missing)."""
    p = Path.home() / ".voyager" / "bundles"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def _fmt_ts(ts) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def get_git_snapshot(repo_root: Optional[str] = None) -> Dict[str, Any]:
    """Capture live git snapshot (branch, HEAD commit, short status) if in a git repo."""
    cwd = repo_root or os.getcwd()
    snapshot: Dict[str, Any] = {
        "is_git": False,
        "branch": "",
        "commit": "",
        "dirty_count": 0,
        "dirty_files": [],
    }
    try:
        res_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res_branch.returncode != 0:
            return snapshot
        snapshot["is_git"] = True
        snapshot["branch"] = res_branch.stdout.strip()

        res_commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res_commit.returncode == 0:
            snapshot["commit"] = res_commit.stdout.strip()

        res_st = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res_st.returncode == 0:
            lines = [ln for ln in res_st.stdout.splitlines() if ln.strip()]
            snapshot["dirty_count"] = len(lines)
            snapshot["dirty_files"] = lines[:15]
    except (subprocess.SubprocessError, OSError):
        pass

    return snapshot


def build_continuation_bundle(
    store: Store,
    session_rows: List[Any],
    goal: Optional[str] = None,
    live_git: bool = True,
) -> str:
    """Synthesize N session rows into a structured Continuation Bundle (Markdown)."""
    if not session_rows:
        return "# Continuation Bundle\n\n(no sessions provided)\n"

    # Sort sessions chronologically: earliest first, newest last
    sorted_rows = sorted(
        session_rows,
        key=lambda r: (r["updated_at"] or 0, r["started_at"] or 0),
    )
    latest_row = sorted_rows[-1]

    # Gather events per session
    sess_events: Dict[str, List[Any]] = {}
    for r in sorted_rows:
        sess_events[r["id"]] = store.events(r["id"])

    L: List[str] = []
    L.append("# Continuation Bundle")
    L.append("")

    # ---- 1. Goal --------------------------------------------------------
    L.append("## Goal")
    L.append("")
    if goal:
        L.append(f"**Primary user goal:** {goal}")
        L.append("")
    else:
        # Collect user messages across sessions in chronological order
        all_user_msgs: List[Tuple[Any, Any]] = []
        for r in sorted_rows:
            for ev in sess_events[r["id"]]:
                if ev["kind"] == "user" and ev["content"]:
                    all_user_msgs.append((r, ev))

        if all_user_msgs:
            first_row, first_ev = all_user_msgs[0]
            L.append(
                f"**Initial request** ([{first_row['provider']}:{first_row['native_id'][:16]}] "
                f"at {_fmt_ts(first_ev['ts'])}):"
            )
            L.append("")
            L.append(first_ev["content"][: _USER_MAX * 2])
            L.append("")
            if len(all_user_msgs) > 1:
                L.append("**Follow-up user instructions across sessions:**")
                L.append("")
                for r, ev in all_user_msgs[1:]:
                    L.append(
                        f"- [{_fmt_ts(ev['ts'])}] `[{r['provider']}:{r['native_id'][:16]}]`: "
                        f"{ev['content'][:_USER_MAX]}"
                    )
                L.append("")
        else:
            L.append("(no user instructions captured)")
            L.append("")

    # ---- 1b. Goal-ranked evidence (Phase 3 / #4) ------------------------
    # Only when a goal is given: same extract+rank pipeline as handoff.
    # Without a goal this section does not exist (pre-Phase-3 semantics).
    top_facts: List[Tuple[Any, float]] = []
    if goal and goal.strip():
        facts = extract_candidate_facts(store, sorted_rows)
        ranked = rank_candidates(facts, goal=goal)
        top_facts = ranked[:12]
        L.append("## Goal-ranked evidence")
        L.append("")
        L.append('goal: "{0}" — top {1} of {2} ranked facts '
                 "(deterministic; each line is provenance-bound):".format(
                     goal, len(top_facts), len(facts)))
        L.append("")
        for f, score in top_facts:
            head = " ".join((f.text or f.command or "").split())[:220]
            L.append("- [{0}] ({1}, score {2}) {3}".format(
                f.provenance, f.kind, score, head))
        L.append("")

    # ---- 2. Current verified state (newest session wins) ----------------
    L.append("## Current verified state")
    L.append("")
    latest_asst = [
        ev for ev in sess_events[latest_row["id"]]
        if ev["kind"] == "assistant" and ev["content"]
    ]
    if latest_asst:
        L.append(
            f"**Where work stopped (active session: `{latest_row['provider']}` "
            f"`{latest_row['native_id'][:24]}`):**"
        )
        L.append("")
        L.append(latest_asst[-1]["content"][:_ASST_MAX])
        L.append("")
    else:
        L.append("(no assistant conclusion captured in the active session)")
        L.append("")

    # ---- 3. Prior assistant conclusions (may be superseded) -------------
    # Chronological overlay rule: older proposals / conclusions are marked
    # superseded so they cannot be confused with open/active options.
    older_conclusions: List[str] = []
    for r in sorted_rows[:-1]:
        asst = [
            ev for ev in sess_events[r["id"]]
            if ev["kind"] == "assistant" and ev["content"]
        ]
        if asst:
            ts_str = _fmt_ts(r["updated_at"] or asst[-1]["ts"])
            snippet = asst[-1]["content"].strip().replace("\n", " ")[:250]
            older_conclusions.append(
                f"- `[{r['provider']}:{r['native_id'][:16]}]` ({ts_str}): {snippet}"
            )

    if older_conclusions:
        L.append("## Prior assistant conclusions (may be superseded)")
        L.append("")
        L.append(
            "> Note: These conclusions are from earlier sessions. If they conflict "
            "with the active session above, the active session takes precedence."
        )
        L.append("")
        for item in older_conclusions:
            L.append(item)
        L.append("")

    # ---- 4. Live git snapshot -------------------------------------------
    if live_git:
        repo_hint = latest_row["repo_root"] or latest_row["cwd"]
        git_info = get_git_snapshot(repo_hint)
        if git_info["is_git"]:
            L.append("## Current repository state (live snapshot)")
            L.append("")
            L.append(f"- Git Branch: `{git_info['branch']}`")
            if git_info["commit"]:
                L.append(f"- HEAD Commit: `{git_info['commit']}`")
            if git_info["dirty_count"] == 0:
                L.append("- Working Tree: clean")
            else:
                L.append(f"- Working Tree: {git_info['dirty_count']} uncommitted change(s)")
                for df in git_info["dirty_files"]:
                    L.append(f"  `{df}`")
            L.append("")

    # ---- 5. Files touched across sessions -------------------------------
    all_files: List[str] = []
    for r in sorted_rows:
        for ev in sess_events[r["id"]]:
            if ev["file_path"]:
                all_files.append(ev["file_path"])
            if ev["files_json"]:
                try:
                    all_files.extend(json.loads(ev["files_json"]))
                except json.JSONDecodeError:
                    pass

    seen_files = set()
    dedup_files = [f for f in all_files if not (f in seen_files or seen_files.add(f))][:_MAX_FILES]
    # Phase 3: with a goal, shrink the file list to what the ranked facts
    # touched (never to empty — the unfiltered list is the fallback).
    if goal and top_facts:
        goal_files = {p for f, _ in top_facts for p in f.paths}
        if goal_files:
            narrowed = [f for f in dedup_files if f in goal_files]
            if narrowed:
                dedup_files = narrowed
    if dedup_files:
        L.append("## Files touched across sessions")
        L.append("")
        for f in dedup_files:
            L.append(f"- `{f}`")
        L.append("")

    # ---- 6. Commands executed -------------------------------------------
    all_cmds: List[Tuple[Any, str, Any]] = []
    for r in sorted_rows:
        for ev in sess_events[r["id"]]:
            if ev["kind"] == "tool_call" and ev["command"]:
                all_cmds.append((ev["ts"], ev["command"], ev["exit_code"]))

    if all_cmds:
        L.append("## Commands executed")
        L.append("")
        seen_cmds = set()
        dedup_cmds = []
        for ts, cmd, rc in reversed(all_cmds):
            norm = " ".join(str(cmd).split())
            if norm not in seen_cmds:
                seen_cmds.add(norm)
                dedup_cmds.append((ts, norm, rc))
        dedup_cmds.reverse()

        # Phase 3: with a goal, shrink to the commands the ranked facts ran
        # (never to empty — the unfiltered list is the fallback).
        if goal and top_facts:
            goal_cmds = {" ".join((f.command or "").split())
                         for f, _ in top_facts if f.command}
            if goal_cmds:
                narrowed = [c for c in dedup_cmds if c[1] in goal_cmds]
                if narrowed:
                    dedup_cmds = narrowed

        for ts, cmd_str, rc in dedup_cmds[-_MAX_COMMANDS:]:
            rc_s = f"  # exit {rc}" if rc is not None else ""
            L.append(f"- `{cmd_str[:_CMD_MAX]}`{rc_s}")
        L.append("")

    # ---- 7. Errors encountered ------------------------------------------
    all_errs: List[str] = []
    for r in sorted_rows:
        for ev in sess_events[r["id"]]:
            if ev["kind"] == "error" and ev["content"]:
                all_errs.append(f"[{_fmt_ts(ev['ts'])}] {ev['content'][:250]}")
            elif (
                ev["kind"] == "tool_call"
                and (ev["exit_code"] or 0) not in (0, None)
                and ev["command"]
            ):
                cmd_short = " ".join(str(ev["command"]).split())[:200]
                all_errs.append(f"[{_fmt_ts(ev['ts'])}] exit {ev['exit_code']}: `{cmd_short}`")

    if all_errs:
        L.append("## Errors encountered")
        L.append("")
        seen_errs = set()
        dedup_errs = [e for e in all_errs if not (e in seen_errs or seen_errs.add(e))][:_MAX_ERRORS]
        for item in dedup_errs:
            L.append(f"- {item}")
        L.append("")

    # ---- 8. Evidence & Provenance ---------------------------------------
    L.append("## Evidence & Provenance")
    L.append("")
    L.append(f"Compiled from {len(sorted_rows)} session(s):")
    L.append("")
    for r in sorted_rows:
        prov = r["provider"]
        nid = r["native_id"]
        t_span = f"{_fmt_ts(r['started_at'])} → {_fmt_ts(r['updated_at'])}"
        title = r["title"] or "(no title)"
        counts = f"{r['message_count']} msgs / {r['tool_count']} tools"
        repo = f" repo=`{r['repo_root'] or r['cwd'] or '?'}`"
        L.append(f"- **{prov}** `{nid}`: {title} ({counts}, {t_span}{repo})")
    L.append("")

    return "\n".join(L)


def default_bundle_name(session_rows: List[Any]) -> str:
    """Generate a clean, filesystem-safe filename for a bundle."""
    if not session_rows:
        return "continuation-bundle.md"
    if len(session_rows) == 1:
        srow = session_rows[0]
        native = (srow["native_id"] or "session")[:24]
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in native)
        return f"bundle-{srow['provider']}-{safe}.md"

    # Multi-session bundle
    import time
    ts = int(time.time())
    provs = "-".join(sorted(set(r["provider"] for r in session_rows)))
    return f"bundle-merged-{len(session_rows)}sessions-{provs}-{ts}.md"


def bundle_command(target: str, bundle_path: Path) -> Optional[List[str]]:
    """Build the argv that seeds a new session in the target agent with this bundle."""
    exe = PROMPT_TARGETS.get(target)
    if not exe:
        return None
    return [
        exe,
        f"{CONTINUATION_INSTRUCTION}\n\nContinuation bundle: {bundle_path.resolve()}",
    ]
