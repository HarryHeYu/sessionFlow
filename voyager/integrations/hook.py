"""Voyager hook CLI — deterministic startup continuation handler.

This module provides `voyager hook startup` command that can be invoked
from provider-native hooks (if available) or via instruction-following.

Usage pattern:
    voyager hook startup \
      --provider claude \
      --cwd "$PWD" \
      --session-id "$SESSION_ID"

Output format suitable for injection into provider context:
    [Voyager Continuation]
    
    Goal:
    ...
    
    Current state:
    ...
    
    Thread:
    thr_xxx

Exit codes:
    0 - Success, context provided
    1 - No active WorkThread found
    2 - Ambiguous thread resolution
    3 - Error during compilation
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..store import Store, default_db_path


def startup_handler(
    provider: str,
    cwd: str,
    session_id: Optional[str] = None,
    db: Optional[Path] = None,
    goal: Optional[str] = None,
    compact: bool = False,
) -> Dict[str, Any]:
    """Handle startup continuity for a given provider/session.
    
    Returns result dict with fields:
        status: "success" | "no_thread" | "ambiguous" | "error"
        context: Markdown continuation bundle (if success)
        thread: Thread info (if any)
        warnings: List of caution notes
    """
    db_path = db or default_db_path()
    store = Store(db_path)
    
    # Incremental scan before compile (Phase 1b requirement)
    try:
        from ..auto import discover_continuity
        disc = discover_continuity(store, cwd=cwd, repo=None)
    except Exception as e:
        return {
            "status": "error",
            "context": f"[Voyager Continuity ERROR]\n\nFailed to discover continuity: {e}",
            "thread": None,
            "warnings": ["Index scan failed"],
        }
    
    # Check for active WorkThread
    if not disc.get("continuity_available"):
        return {
            "status": "no_thread",
            "context": "[Voyager Continuity]\n\nNo active WorkThread found for this repository.\n\nCreate one first:\n  voyager thread create --repo " + cwd,
            "thread": None,
            "warnings": [],
        }
    
    # Check for ambiguity
    pend = disc.get("pending_attach") or []
    if any(p.get("status") == "ambiguous" for p in pend):
        ambiguous_threads = [p for p in pend if p.get("status") == "ambiguous"]
        thread_ids = [t.get("thread_id", "unknown") for t in ambiguous_threads]
        return {
            "status": "ambiguous",
            "context": "[Voyager Continuity WARNING]\n\nAmbiguous WorkThread resolution.\n\nMultiple active threads found in same repo:\n  - " + "\n  - ".join(thread_ids) + "\n\nResolve by specifying exact thread ID:\n  voyager continue --thread <id>",
            "thread": None,
            "warnings": ["Multiple active WorkThreads detected"],
        }
    
    # Get active thread
    active_thread = disc.get("active_thread")
    if not active_thread:
        return {
            "status": "no_thread",
            "context": "[Voyager Continuity]\n\nNo active WorkThread found.\n\nCreate one with:\n  voyager thread create --repo " + cwd,
            "thread": None,
            "warnings": [],
        }
    
    thread_id = active_thread.get("id")
    repo_root = disc.get("repo_root", cwd)
    
    # Compile continuation context
    try:
        from ..continuity import compile_continuation_bundle
        
        # Extract member sessions from thread
        member_sessions = store.thread_member_sessions(thread_id)
        
        # Compile bundle
        bundle = compile_continuation_bundle(
            sessions=member_sessions,
            goal=goal,
            budget="compact" if compact else "balanced",
            output_format="markdown",
        )
        
        # Format output
        context = format_voyager_continuation(bundle, active_thread, disc)
        
        return {
            "status": "success",
            "context": context,
            "thread": {
                "id": thread_id,
                "title": active_thread.get("title"),
                "goal": active_thread.get("goal"),
                "members": len(member_sessions),
                "repo": repo_root,
            },
            "warnings": disc.get("warnings", []),
        }
        
    except Exception as e:
        return {
            "status": "error",
            "context": f"[Voyager Continuity ERROR]\n\nFailed to compile context: {e}",
            "thread": active_thread,
            "warnings": ["Context compilation failed"],
        }


def format_voyager_continuation(
    bundle: Dict[str, Any],
    thread: Dict[str, Any],
    discovery: Dict[str, Any]
) -> str:
    """Format continuation bundle into provider-injectable text."""
    lines = [
        "[Voyager Continuation]",
        "",
        "## Thread",
        f"- ID: {thread.get('id', 'unknown')}",
        f"- Title: {thread.get('title', 'Untitled')}",
        f"- Repo: {discovery.get('repo_root', 'Unknown')}",
        "",
    ]
    
    # Add goal if present
    goal = thread.get("goal") or bundle.get("goal")
    if goal:
        lines.extend([
            "## Goal",
            goal,
            "",
        ])
    
    # Add current state from bundle
    current_state = bundle.get("current_state", {})
    if current_state:
        lines.extend([
            "## Current State",
        ])
        if isinstance(current_state, dict):
            for key, value in current_state.items():
                lines.append(f"- {key}: {value}")
        else:
            lines.append(str(current_state))
        lines.append("")
    
    # Add relevant files
    files = bundle.get("relevant_files", [])
    if files:
        lines.extend([
            "## Relevant Files",
        ])
        for f in files[:20]:  # Limit to 20 files
            lines.append(f"- {f}")
        lines.append("")
    
    # Add next steps
    next_steps = bundle.get("next_steps", [])
    if next_steps:
        lines.extend([
            "## Next Steps",
        ])
        for step in next_steps[:10]:
            lines.append(f"- {step}")
        lines.append("")
    
    # Add decisions if any
    decisions = bundle.get("decisions", [])
    if decisions:
        lines.extend([
            "## Important Decisions",
        ])
        for dec in decisions[:5]:
            lines.append(f"- {dec}")
        lines.append("")
    
    # Provenance note
    lines.extend([
        "---",
        f"Voyager WorkThread: {thread.get('id')}",
        f"Member sessions: {len(bundle.get('sessions', []))}",
        f"Compiled from: {discovery.get('repo_root')}",
    ])
    
    return "\n".join(lines)


def cmd_hook_startup(args) -> int:
    """CLI entry point for hook startup command."""
    result = startup_handler(
        provider=args.provider,
        cwd=args.cwd,
        session_id=getattr(args, "session_id", None),
        db=Path(args.db) if getattr(args, "db", None) else None,
        goal=getattr(args, "goal", None),
        compact=getattr(args, "compact", False),
    )
    
    # Output context to stdout (for injection)
    print(result["context"])
    
    # Exit code based on status
    status_map = {
        "success": 0,
        "no_thread": 1,
        "ambiguous": 2,
        "error": 3,
    }
    
    return status_map.get(result["status"], 3)


def register_hook_parser(subparsers, common_parents):
    """Register hook startup subcommand."""
    sp = subparsers.add_parser(
        "hook",
        help="Voyager lifecycle hooks for native provider integration"
    )
    
    hook_sub = sp.add_subparsers(dest="hook_cmd", required=True)
    
    # startup subcommand
    startup_sp = hook_sub.add_parser(
        "startup",
        help="Handle startup continuity for a provider session"
    )
    startup_sp.add_argument("--provider", required=True,
                           help="target provider (claude|codex|grok|...)")
    startup_sp.add_argument("--cwd", required=True,
                           help="current working directory")
    startup_sp.add_argument("--session-id",
                           help="native session ID (if available at startup)")
    startup_sp.add_argument("--goal",
                           help="primary goal for context ranking")
    startup_sp.add_argument("--compact", action="store_true",
                           help="use compact context budget")
    startup_sp.set_defaults(func=cmd_hook_startup)
    
    return sp
