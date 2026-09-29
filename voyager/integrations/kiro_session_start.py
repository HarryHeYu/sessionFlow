"""Kiro hook handler (native Agent Hooks).

Kiro's contract (https://kiro.dev/docs/hooks/) differs from the Claude-compatible
providers in one structural way: a **command action's stdout is added to the
agent's context**, so this handler writes the tiered-v1 document as plain text —
there is no JSON envelope to fill.

  config   ``.kiro/hooks/<id>.json`` (project root), auto-activated:
           ``{"version": "v1", "hooks": [{"name": ..., "trigger": "SessionStart",
              "action": {"type": "command", "command": "<shell string>"}}]}``
           ``SessionStart`` is IDE-only; the CLI's equivalent is ``AgentSpawn``.
  stdin    "session context as JSON" — the documented trigger list does not pin
           the field names, so this handler reads what it recognises and does not
           invent the rest.  Kiro runs command hooks **in the project root**, so
           the process cwd is already the repository.
  stdout   added to the agent's context verbatim.  A non-zero exit sends stderr
           to the agent instead (and blocks for PreToolUse / PromptSubmit), so
           every failure path here still exits 0.

Thin by design: WorkThread resolution, tiered context, cache, pending attach and
ambiguity rules live in :func:`voyager.startup.startup_continuity`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from voyager.continuity import CONTEXT_FORMAT_TIERED
    from voyager.integrations.hook_payload import cap_tiered_with_note, payload_len
    from voyager.startup import startup_continuity
except ImportError:  # running as a standalone script from the hooks config
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from voyager.continuity import CONTEXT_FORMAT_TIERED
    from voyager.integrations.hook_payload import cap_tiered_with_note, payload_len
    from voyager.startup import startup_continuity

PROVIDER = "kiro"

#: Kiro documents no size limit for what a hook may add to the context. The
#: shared cap is applied defensively, and it is also what keeps the window built
#: small enough that nothing has to be cut.
PROVIDER_L1_BUDGET = 7600

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
        log = _log_dir() / "kiro-hooks.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
            log.write_bytes(log.read_bytes()[-LOG_KEEP_BYTES:])
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


def normalize_event(stdin_raw: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Kiro's session context -> the fields the shared core needs.

    The documented triggers do not pin Kiro's stdin field names, so this reads
    only the keys that appear across Kiro's other surfaces and never guesses a
    value.  The repository is the process cwd, because Kiro runs command hooks
    in the project root.
    """
    try:
        event = json.loads(stdin_raw) if stdin_raw.strip() else {}
        if not isinstance(event, dict):
            event = {}
    except Exception:
        event = {}

    roots = event.get("workspacePaths") or event.get("workspace_roots")
    first_root = None
    if isinstance(roots, list) and roots:
        first_root = next((r for r in roots if isinstance(r, str) and r.strip()), None)
    return {
        # Kiro runs command hooks in the project root, so the process cwd is the
        # authoritative repository; a payload path is only a fallback.
        "cwd": cwd or os.getcwd() or first_root or event.get("cwd"),
        "native_session_id": (event.get("session_id") or event.get("sessionId")
                              or event.get("conversation_id")
                              or event.get("conversationId") or None),
        "trigger": event.get("trigger") or event.get("hook_event_name") or None,
    }


def handle_kiro_session_start(
    cwd: Optional[str] = None,
    store: Optional[Any] = None,
    stdin_raw: Optional[str] = None,
) -> Dict[str, Any]:
    """One Kiro session start -> tiered-v1 text for the agent's context."""
    if stdin_raw is None:
        stdin_raw = _read_stdin()
    payload = normalize_event(stdin_raw, cwd=cwd)
    session_id = payload["native_session_id"]

    try:
        # Anything the core prints would splice into our protocol stdout, so the
        # call is buffered; the envelope is written afterwards, on purpose.
        with contextlib.redirect_stdout(io.StringIO()):
            result = startup_continuity(
                provider=PROVIDER,
                cwd=payload["cwd"],
                native_session_id=session_id,
                auto_attach=True,
                store=store,
                context_format=CONTEXT_FORMAT_TIERED,
                l1_hard_max=PROVIDER_L1_BUDGET,
        )
    except Exception as e:
        _log_event({"ts": time.time(), "event": "startup_continuity_error",
                    "error": str(e), "cwd": payload["cwd"], "session_id": session_id})
        return {"status": "error", "message": str(e)}

    if not result.continuity_available or not result.context:
        attach_status = getattr(result, "attach_status", None) or ""
        continuity_info = {
            "context_source": getattr(result, "context_source", None),
            "recommended_action": getattr(result, "recommended_action", None),
        }
        # Ambiguity -- several active WorkThreads in this repo -- is a real,
        # actionable failure.  Reporting it as "no_thread" would hide it and
        # contradict the core's own contract (exact repo + 0 threads -> nothing to
        # continue; + 1 -> continue it; + more than one -> refuse to choose).
        # Nothing is auto-selected and nothing is injected on this path.
        if attach_status.startswith("ERROR_") or "ambiguous" in attach_status.lower():
            _log_event({"ts": time.time(), "event": "ambiguous", "cwd": payload["cwd"],
                        "session_id": session_id, "attach_status": attach_status})
            return {"status": "error", "attach_status": attach_status,
                    "message": ("voyager could not resolve a WorkThread for %s (%s). "
                                "Pick one explicitly with `voyager continue --thread <id>`."
                                % (payload["cwd"], attach_status)),
                    "continuity_info": continuity_info}
        _log_event({"ts": time.time(), "event": "no_context", "cwd": payload["cwd"],
                    "session_id": session_id, "attach_status": attach_status})
        return {"status": "no_thread", "attach_status": attach_status,
                "continuity_info": continuity_info}

    _log_event({"ts": time.time(), "event": "context_ready", "cwd": payload["cwd"],
                "session_id": session_id, "thread_id": result.thread_id,
                "context_chars": len(result.context),
                "attach_status": getattr(result, "attach_status", None)})
    return {"status": "context_ready", "context": result.context,
            "thread": {"id": result.thread_id, "goal": result.goal,
                       "repo": result.repo_root},
            "native_session_id": session_id}


def emit(result: Dict[str, Any]) -> int:
    """Write the context as plain text; always exit 0 (fail-open).

    Kiro adds a command hook's stdout to the agent's context directly, so the
    document is written verbatim rather than wrapped in an envelope.
    """
    try:
        if result.get("status") == "context_ready" and result.get("context"):
            context, spilled = cap_tiered_with_note(result["context"], PROVIDER)
            if spilled:
                _log_event({"ts": time.time(), "event": "payload_capped",
                            "original_chars": payload_len(result["context"]),
                            "delivered_chars": payload_len(context),
                            "spilled": str(spilled)})
            sys.stdout.write(context)
            sys.stdout.flush()
    except Exception as e:
        _log_event({"ts": time.time(), "event": "emit_error", "error": str(e)})
    return 0


def main() -> int:
    try:
        result = handle_kiro_session_start()
    except Exception as e:
        _log_event({"ts": time.time(), "event": "handler_crash", "error": str(e)})
        return 0
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
