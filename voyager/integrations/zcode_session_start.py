"""ZCode SessionStart hook handler (native hooks, zcode 3.14+).

ZCode's hook contract is documented at https://zcode.z.ai/cn/docs/hooks and is
Claude-compatible in **both** directions:

  stdin  ``{"session_id": ..., "cwd": ..., "hook_event_name": "SessionStart",
            "source": "startup", "transcript_path": ..., ...}``
         (ZCode writes camelCase fields with snake_case aliases alongside)
  stdout ``{"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": "..."}}``

Config source that actually executes is the **user** one,
``~/.zcode/cli/config.json``, with ``hooks.enabled: true`` and events under
``hooks.events.<EventName>``.  Project-level hook config is ignored by the
provider for security (logged as ``config_project_hooks_ignored``), and plugin
hooks (``hooks/hooks.json``) require installing and enabling a plugin through
the UI — so the user config is the zero-touch path.

Thin by design: WorkThread resolution, tiered context generation, the cache,
pending attach and ambiguity rules all live in
:func:`voyager.startup.startup_continuity` — one shared core for every provider.
Only payload normalisation and the response envelope live here.

Fail-open contract: never break a ZCode session.  Every failure path exits 0;
diagnostics go to ``~/.voyager/logs/zcode-hooks.jsonl``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from voyager.continuity import CONTEXT_FORMAT_TIERED
    from voyager.startup import startup_continuity
except ImportError:  # running as a standalone script from the hooks config
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from voyager.continuity import CONTEXT_FORMAT_TIERED
    from voyager.startup import startup_continuity

PROVIDER = "zcode"
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_BYTES = 200_000


def _log_dir() -> Path:
    override = os.environ.get("VOYAGER_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".voyager" / "logs"


def _log_event(event: Dict[str, Any]) -> None:
    """Best-effort structured trace: "the hook ran" must always be visible."""
    try:
        log = _log_dir() / "zcode-hooks.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
            tail = log.read_bytes()[-LOG_KEEP_BYTES:]
            log.write_bytes(tail)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_stdin() -> str:
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            return sys.stdin.read()
    except Exception:
        pass
    return ""


def normalize_event(stdin_raw: str) -> Dict[str, Any]:
    """ZCode's SessionStart payload -> the two fields the core needs.

    ZCode sends ``session_id``/``cwd`` and their camelCase aliases; both are
    accepted, and a missing session id stays ``None`` so the shared core can
    record a pending attach rather than inventing an identity.
    """
    try:
        event = json.loads(stdin_raw) if stdin_raw.strip() else {}
        if not isinstance(event, dict):
            event = {}
    except Exception:
        event = {}
    return {
        "cwd": event.get("cwd") or os.getcwd(),
        "native_session_id": (event.get("session_id")
                              or event.get("sessionId") or None),
        "source": event.get("source") or event.get("hook_event_name") or None,
    }


def handle_zcode_session_start(
    cwd: Optional[str] = None,
    store: Optional[Any] = None,
    stdin_raw: Optional[str] = None,
) -> Dict[str, Any]:
    """One SessionStart event -> tiered-v1 context in the ZCode envelope.

    Returns the internal result dict; :func:`emit` turns it into protocol
    stdout.  ``store`` exists for tests; production resolves the real index.
    """
    if stdin_raw is None:
        stdin_raw = _read_stdin()
    payload = normalize_event(stdin_raw)
    cwd = cwd or payload["cwd"]
    session_id = payload["native_session_id"]

    try:
        result = startup_continuity(
            provider=PROVIDER,
            cwd=cwd,
            native_session_id=session_id,
            auto_attach=True,
            store=store,
            context_format=CONTEXT_FORMAT_TIERED,
        )
    except Exception as e:
        _log_event({"ts": time.time(), "event": "startup_continuity_error",
                    "error": str(e), "cwd": cwd, "session_id": session_id})
        return {"status": "error", "message": str(e)}

    if not result.continuity_available or not result.context:
        out = {
            "status": "no_thread",
            "attach_status": getattr(result, "attach_status", None),
            "continuity_info": {
                "context_source": getattr(result, "context_source", None),
                "recommended_action": getattr(result, "recommended_action", None),
            },
        }
        _log_event({"ts": time.time(), "event": "no_context",
                    "cwd": cwd, "session_id": session_id,
                    "attach_status": out["attach_status"]})
        return out

    out = {
        "status": "context_ready",
        "context": result.context,
        "thread": {"id": result.thread_id, "goal": result.goal,
                   "repo": result.repo_root},
        "attach_status": getattr(result, "attach_status", None),
        "native_session_id": session_id,
        "continuity_info": {
            "context_source": getattr(result, "context_source", None),
            "context_stale_cached": getattr(result, "context_stale", None),
            "recommended_action": getattr(result, "recommended_action", None),
        },
    }
    _log_event({"ts": time.time(), "event": "context_ready",
                "cwd": cwd, "session_id": session_id,
                "thread_id": result.thread_id,
                "context_chars": len(result.context),
                "attach_status": out["attach_status"]})
    return out


def emit(result: Dict[str, Any]) -> int:
    """Write the protocol envelope; always exit 0 (fail-open)."""
    try:
        if result.get("status") == "context_ready" and result.get("context"):
            payload = {"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": result["context"],
            }}
            # ensure_ascii: a GBK console must never corrupt the context, and
            # escaped JSON decodes back to the exact same string.
            sys.stdout.write(json.dumps(payload, ensure_ascii=True))
            sys.stdout.flush()
    except Exception as e:
        _log_event({"ts": time.time(), "event": "emit_error", "error": str(e)})
    return 0


def main() -> int:
    try:
        result = handle_zcode_session_start()
    except Exception as e:
        _log_event({"ts": time.time(), "event": "handler_crash", "error": str(e)})
        return 0
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
