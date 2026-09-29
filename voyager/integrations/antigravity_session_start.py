"""Antigravity hook handler (native hooks, PreInvocation).

Antigravity (https://antigravity.google/docs/hooks) has **no session-start
event**: its events are ``PreToolUse``, ``PostToolUse``, ``PreInvocation``,
``PostInvocation`` and ``Stop``.  ``PreInvocation`` fires before each model call
and carries ``invocationNum``, which is 0 for the first call — that is the
session-start equivalent, and this handler injects exactly once.

  config   ``~/.gemini/config/hooks.json`` (global) or ``.agents/hooks.json``
           (workspace).  The shape maps a hook *name* to its events, and
           ``PreInvocation`` takes handlers directly (no matcher):

             {"voyager-continuity": {"PreInvocation": [
                {"type": "command", "command": "<shell string>", "timeout": 30}]}}

  stdin    ``{"conversationId": ..., "workspacePaths": [...],
              "transcriptPath": ..., "modelName": ..., "invocationNum": 0}``
  stdout   ``{"injectSteps": [{"ephemeralMessage": "..."}]}`` — context reaches
           the conversation only through ``injectSteps``; ``PreInvocation`` is
           the earliest point at which it can.

Antigravity documents no exit-code semantics and no size limit, so this handler
always exits 0 and applies the shared cap.

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
from typing import Any, Dict, List, Optional

try:
    from voyager.continuity import CONTEXT_FORMAT_TIERED
    from voyager.integrations.hook_payload import cap_tiered_with_note, payload_len
    from voyager.startup import startup_continuity
except ImportError:  # running as a standalone script from the hooks config
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from voyager.continuity import CONTEXT_FORMAT_TIERED
    from voyager.integrations.hook_payload import cap_tiered_with_note, payload_len
    from voyager.startup import startup_continuity

PROVIDER = "antigravity"

#: No documented size limit; the shared cap keeps the window buildable without a
#: later cut.
PROVIDER_L1_BUDGET = 7600

LOG_MAX_BYTES = 1_000_000
LOG_KEEP_BYTES = 200_000


def _log_dir() -> Path:
    override = os.environ.get("VOYAGER_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".voyager" / "logs"


def _log_event(event: Dict[str, Any]) -> None:
    try:
        log = _log_dir() / "antigravity-hooks.jsonl"
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
    """Antigravity's PreInvocation payload -> the fields the shared core needs.

    ``workspacePaths`` is the documented source of the repository; there is no
    ``cwd``.  ``invocationNum`` decides whether this is the first model call, and
    only that call may inject.
    """
    try:
        event = json.loads(stdin_raw) if stdin_raw.strip() else {}
        if not isinstance(event, dict):
            event = {}
    except Exception:
        event = {}

    roots: List[str] = []
    raw_roots = event.get("workspacePaths")
    if isinstance(raw_roots, list):
        roots = [r for r in raw_roots if isinstance(r, str) and r.strip()]
    # `invocationNum` decides whether this is the first model call.  JSON gives no
    # guarantee about its numeric shape, so accept int, float and a numeric string
    # -- a `2.0` or a `"2"` that normalised to None would be read as "first" and
    # re-inject the whole window on every single turn.  A value that is present but
    # unusable maps to -1 (definitely not the first call); only an absent field
    # means a provider version that does not send one, which is treated as first.
    num = event.get("invocationNum")
    if isinstance(num, bool):
        invocation_num: Optional[int] = -1
    elif isinstance(num, int):
        invocation_num = num
    elif isinstance(num, float) and num.is_integer():
        invocation_num = int(num)
    elif isinstance(num, str) and num.strip().lstrip("+-").isdigit():
        invocation_num = int(num.strip())
    elif num is None:
        invocation_num = None
    else:
        invocation_num = -1
    return {
        "cwd": cwd or (roots[0] if roots else None) or event.get("cwd") or os.getcwd(),
        "native_session_id": (event.get("conversationId")
                              or event.get("conversation_id") or None),
        "invocation_num": invocation_num,
    }


def handle_antigravity_pre_invocation(
    cwd: Optional[str] = None,
    store: Optional[Any] = None,
    stdin_raw: Optional[str] = None,
) -> Dict[str, Any]:
    """One PreInvocation -> tiered-v1 text for the first model call only."""
    if stdin_raw is None:
        stdin_raw = _read_stdin()
    payload = normalize_event(stdin_raw, cwd=cwd)

    invocation_num = payload["invocation_num"]
    if invocation_num is not None and invocation_num != 0:
        # A later model call: injecting again would repeat the whole window every
        # turn.  An absent field means a provider version that does not send it,
        # which is treated as the first call.
        return {"status": "skipped", "reason": "not_first_invocation"}

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
    """Write ``injectSteps``; always exit 0.

    Antigravity only accepts conversation steps through ``injectSteps``, so the
    context rides as an ``ephemeralMessage`` (a system message, not a user turn).
    """
    try:
        if result.get("status") == "context_ready" and result.get("context"):
            context, spilled = cap_tiered_with_note(result["context"], PROVIDER)
            if spilled:
                _log_event({"ts": time.time(), "event": "payload_capped",
                            "original_chars": payload_len(result["context"]),
                            "delivered_chars": payload_len(context),
                            "spilled": str(spilled)})
            payload = {"injectSteps": [{"ephemeralMessage": context}]}
            sys.stdout.write(json.dumps(payload, ensure_ascii=True))
            sys.stdout.flush()
    except Exception as e:
        _log_event({"ts": time.time(), "event": "emit_error", "error": str(e)})
    return 0


def main() -> int:
    try:
        result = handle_antigravity_pre_invocation()
    except Exception as e:
        _log_event({"ts": time.time(), "event": "handler_crash", "error": str(e)})
        return 0
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
