"""Voyager launcher prelaunch hook."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict


def prelaunch(
    provider: str,
    cwd: str,
    db: str = None,
) -> Dict[str, Any]:
    """Run Voyager prelaunch hook before launching target agent.
    
    This is called by wrapper scripts (grok, dsh, kiro-cli) before
    invoking the real executable.
    
    Purpose:
    - Incremental index scan
    - WorkThread discovery
    - Compile minimal context for attachment
    
    Output: Prints JSON status to stdout.
    """
    from .store import Store, default_db_path
    
    db_path = Path(db) if db else default_db_path()
    store = Store(db_path)
    
    result = {
        "provider": provider,
        "cwd": cwd,
        "status": "success",
    }
    
    try:
        # Perform incremental scan
        from .auto import discover_continuity
        
        disc = discover_continuity(store, cwd=cwd, repo=None)
        
        if not disc.get("continuity_available"):
            result["status"] = "no_thread"
            result["message"] = "No active WorkThread found"
        else:
            active_thread = disc.get("active_thread")
            if active_thread:
                thread_id = active_thread.get("id")
                members = store.thread_member_sessions(thread_id)
                
                from .continuity import build_continuation_bundle
                
                bundle = build_continuation_bundle(
                    store=store,
                    session_rows=members[:3],  # Compact: top 3 sessions
                    goal=None,
                    live_git=True,
                )
                
                result["thread"] = thread_id
                result["members_found"] = len(members)
                result["bundle_length"] = len(bundle)
            
            pending = disc.get("pending_attach") or []
            if pending:
                result["pending_attaches"] = len(pending)
        
        return result
        
    except Exception as e:
        return {
            "provider": provider,
            "status": "error",
            "message": str(e),
        }


def main(argv=None):
    """CLI entry point for voyager launcher."""
    import argparse
    
    parser = argparse.ArgumentParser(prog="voyager launcher")
    sub = parser.add_subparsers(dest="cmd", required=True)
    
    sp = sub.add_parser("prelaunch", help="run prelaunch hook")
    sp.add_argument("--provider", required=True)
    sp.add_argument("--cwd", required=True)
    sp.add_argument("--db", help="index db path")
    sp.add_argument("--json", action="store_true", help="output as JSON")
    sp.set_defaults(func=run_prelaunch_cli)
    
    args = parser.parse_args(argv)
    exit_code = args.func(args)
    sys.exit(exit_code)


def run_prelaunch_cli(args) -> int:
    """Run prelaunch via CLI."""
    result = prelaunch(
        provider=args.provider,
        cwd=args.cwd,
        db=args.db,
    )
    
    if getattr(args, "json", False):
        import json
        print(json.dumps(result, indent=2))
    else:
        print(f"Provider: {result['provider']}")
        print(f"Status: {result['status']}")
        if "thread" in result:
            print(f"Thread: {result['thread']}")
            print(f"Members: {result.get('members_found', 'N/A')}")
    
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    main()
