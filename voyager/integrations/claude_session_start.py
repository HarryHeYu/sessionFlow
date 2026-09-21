"""Claude SessionStart hook handler - cross-platform entrypoint.

Called from Claude Code native hooks configuration.

Invocation:
  python -m voyager.integrations.claude_session_start
    
Stdin: JSON with session_id, cwd, etc. (from Claude)
Stdout: JSON with continuation context or empty on failure
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from voyager.store import Store
    from voyager.startup import startup_continuity
except ImportError:
    # Running as standalone module, parent is voyager dir
    import os
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    
    from voyager.store import Store
    from voyager.startup import startup_continuity


def handle_claude_session_start(cwd: Optional[str] = None) -> Dict[str, Any]:
    """Handle Claude SessionStart event.
    
    Args:
        cwd: Current working directory from Claude's stdin JSON
        
    Returns:
        {
            "status": "success" | "no_thread" | "error",
            "context": str,      # Continuation bundle markdown (optional if success)
            "thread": {          # WorkThread info (optional)
                "id": str,
                "title": str,
                "members": int,
            },
        }
    """
    try:
        # Read Claude's stdin JSON
        stdin_content = sys.stdin.read()
        
        if stdin_content.strip():
            claude_event = json.loads(stdin_content)
            # Extract common fields from Claude's hook input
            cwd = cwd or claude_event.get("cwd")
            
        if not cwd:
            cwd = str(Path.cwd())
        
        # Use unified startup continuity primitive
        result = startup_continuity(
            provider="claude",
            cwd=cwd,
            auto_attach=False,  # Let user decide when launching via hooks
        )
        
        if not result.continuity_available or result.attach_status == "no_auto_attach":
            return {
                "status": "no_thread",
                "message": result.context_stale or "No active WorkThread found",
            }
        
        # Get continuation context
        if not result.context:
            return {
                "status": "no_thread", 
                "message": "Context not available",
            }
        
        return {
            "status": "success",
            "context": result.context,
            "thread": {
                "id": result.thread_id,
                "title": f"{result.goal or 'Untitled'}"[:100],
                "repo": result.repo_root,
                "members": 0,  # Would need to fetch member sessions separately
                "goal": result.goal,
            },
        }
        
    except Exception as e:
        import traceback
        
        return {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc(),
        }


def main():
    """Standalone entrypoint."""
    result = handle_claude_session_start()
    
    # Output JSON for Claude to consume
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # Exit code indicates status
    sys.exit(0 if result["status"] == "success" else 1)


if __name__ == "__main__":
    main()
