"""Grok ``SessionStart`` hook handler — cross-platform entrypoint.

Called from a Grok native hook (``~/.grok/hooks/voyager.json``). Grok runs the
hook ``command`` through PowerShell on Windows, which mangles an inline shell
command containing quotes, so the hook points at a small ``.cmd`` shim that in
turn invokes this file by absolute path — the same "works even when voyager is
not importable" contract ``claude_session_start.py`` uses.

Invocation:
  "<ABSOLUTE_PYTHON>" "<ABS_PATH>/voyager/integrations/grok_session_start.py"

Stdin: Grok's hook envelope as JSON (``sessionId``, ``cwd``, ``workspaceRoot``,
       ``hookEventName``, ...). Grok also exports ``GROK_SESSION_ID`` and
       ``GROK_WORKSPACE_ROOT``, which is what this handler prefers.

Output: none. ``SessionStart`` is a passive event — Grok ignores the stdout of a
        non-blocking hook, and there is no channel to inject context from a
        session-start hook. The value of running at all is the side effect: the
        pending attach that lets the next scan bind this native session to its
        WorkThread.

Exit codes: always 0. Hooks fail open by design, and a continuity recorder must
never block the agent session it is observing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from voyager.integrations.grok_native import session_start
except ImportError:
    # Running as a standalone module, parent is the voyager package dir.
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from voyager.integrations.grok_native import session_start


def _read_payload() -> Dict[str, Any]:
    """Read Grok's hook envelope from stdin.

    A console (or a closed stream) is not a hook pipe. Reading it would block
    until the user typed EOF, and on Windows that read holds the console the
    Grok TUI is using. Grok writes one JSON line and does not always close the
    pipe, so this reads a single line rather than until EOF.
    """
    try:
        stdin_stream = sys.stdin
        if stdin_stream is None or stdin_stream.closed or stdin_stream.isatty():
            return {}
        raw = stdin_stream.readline()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _log(record: Dict[str, Any]) -> None:
    """Append a bounded trace, so "hook never ran" is distinguishable from
    "hook ran and did nothing". Failures here are ignored on purpose."""
    override = os.environ.get("VOYAGER_GROK_HOOK_LOG")
    path = Path(override) if override else (
        Path.home() / ".voyager" / "logs" / "grok-session-start.jsonl"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.write_text("", encoding="utf-8")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def main() -> int:
    payload = _read_payload()

    # Grok's env vars are authoritative; the envelope is the fallback for a
    # client that does not export them.
    session_id = os.environ.get("GROK_SESSION_ID") or payload.get("sessionId")
    cwd = (
        os.environ.get("GROK_WORKSPACE_ROOT")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or payload.get("workspaceRoot")
        or payload.get("cwd")
    )

    result = session_start(session_id=session_id, cwd=cwd)
    result["hook_event"] = payload.get("hookEventName") or os.environ.get(
        "GROK_HOOK_EVENT"
    )
    # Arm after the attach is recorded. The watcher is detached and fail-open;
    # it must not delay or fail this hook. It exists so a Grok 1.0.70 quit,
    # which logs pager.exited and then sticks in ExitProcess, cannot hold the
    # terminal. ``~/.voyager/bin`` is not on PATH here, so the launcher shim
    # never sees this process — the hook is the only sessionFlow code that runs.
    try:
        from voyager.integrations.grok_exit_watch import arm_from_here
        armed = arm_from_here()
        if armed:
            result["exit_watch_pid"] = armed
    except Exception:
        pass
    _log(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
