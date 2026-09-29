"""Cursor sessionStart hook handler (native hooks).

Cursor's contract differs from Claude/Codex/ZCode in one important place, so it
is worth stating precisely (https://cursor.com/docs/hooks):

  config  ``~/.cursor/hooks.json`` (user) or ``.cursor/hooks.json`` (project),
          ``{"version": 1, "hooks": {"sessionStart": [{"command": "<shell
          string>", "timeout": 30}]}}`` — ``command`` is a string, not argv.
          User hooks run from ``~/.cursor/``, so the command must be absolute.
  stdin   ``{"session_id": ..., "is_background_agent": ..., "composer_mode":
            ..., "workspace_roots": [...], "transcript_path": ..., ...}``
          There is **no ``cwd``** on sessionStart — the working directory comes
          from ``workspace_roots`` or the ``CURSOR_PROJECT_DIR`` environment
          variable the provider always sets.
  stdout  ``{"additional_context": "..."}`` — **top level**, NOT
          ``hookSpecificOutput``; Cursor does not use that envelope at all.

Thin by design: WorkThread resolution, tiered-v1 context, cache, pending attach
and ambiguity rules live in :func:`voyager.startup.startup_continuity`.  Only
payload normalisation and the response envelope live here.

Fail-open contract: never break a Cursor session.  Every failure path exits 0;
diagnostics go to ``~/.voyager/logs/cursor-hooks.jsonl``.
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

PROVIDER = "cursor"
#: The providers cap the injected string at 10,000 characters; leave room for
#: the preamble (L0 + Runtime State + retrieval hint) and the truncation note.
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
        log = _log_dir() / "cursor-hooks.jsonl"
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


def normalize_event(stdin_raw: str,
                    env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Cursor's sessionStart payload -> the fields the shared core needs.

    sessionStart carries no ``cwd``, so the repository comes from
    ``workspace_roots`` and then from the provider's own environment variables
    (``CURSOR_PROJECT_DIR``, or Claude's ``CLAUDE_PROJECT_DIR`` alias).  A
    missing session id stays ``None`` so the core records a pending attach
    instead of inventing an identity.
    """
    env = env if env is not None else dict(os.environ)
    try:
        event = json.loads(stdin_raw) if stdin_raw.strip() else {}
        if not isinstance(event, dict):
            event = {}
    except Exception:
        event = {}

    roots: List[str] = []
    raw_roots = event.get("workspace_roots")
    if isinstance(raw_roots, list):
        roots = [r for r in raw_roots if isinstance(r, str) and r.strip()]
    # No `cwd` on sessionStart: the repository comes from workspace_roots, then
    # from the provider's own environment.  The process fallback is ~/.cursor/,
    # where user hooks run -- it resolves to no thread rather than the wrong one,
    # which is the failure mode that matters.
    cwd = (roots[0] if roots
           else env.get("CURSOR_PROJECT_DIR")
           or env.get("CLAUDE_PROJECT_DIR")
           or os.getcwd())
    return {
        "cwd": cwd,
        # Cursor documents session_id as the identity; conversation_id is the same
        # value and is carried alongside, so it is accepted as a fallback rather
        # than silently losing the session identity.
        "native_session_id": (event.get("session_id")
                              or event.get("sessionId")
                              or event.get("conversation_id")
                              or event.get("conversationId") or None),
        "workspace_roots": roots,
        "composer_mode": event.get("composer_mode"),
        "is_background_agent": event.get("is_background_agent"),
    }


def handle_cursor_session_start(
    cwd: Optional[str] = None,
    store: Optional[Any] = None,
    stdin_raw: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """One sessionStart event -> tiered-v1 context in Cursor's envelope.

    Returns the internal result dict; :func:`emit` turns it into protocol
    stdout.  ``store`` exists for tests; production resolves the real index.
    """
    if stdin_raw is None:
        stdin_raw = _read_stdin()
    payload = normalize_event(stdin_raw, env=env)
    cwd = cwd or payload["cwd"]
    session_id = payload["native_session_id"]

    try:
        # Anything the core prints would splice into our protocol stdout, so the
        # call is buffered; the envelope is written afterwards, on purpose.
        with contextlib.redirect_stdout(io.StringIO()):
            result = startup_continuity(
                provider=PROVIDER,
                cwd=cwd,
                native_session_id=session_id,
                auto_attach=True,
                store=store,
                context_format=CONTEXT_FORMAT_TIERED,
                # Build the L1 inside this provider's cap instead of letting the
                # provider cut the document afterwards: a cut drops whole sessions.
                l1_hard_max=PROVIDER_L1_BUDGET,
        )
    except Exception as e:
        _log_event({"ts": time.time(), "event": "startup_continuity_error",
                    "error": str(e), "cwd": cwd, "session_id": session_id})
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
            _log_event({"ts": time.time(), "event": "ambiguous", "cwd": cwd,
                        "session_id": session_id, "attach_status": attach_status})
            return {"status": "error", "attach_status": attach_status,
                    "message": ("voyager could not resolve a WorkThread for %s (%s). "
                                "Pick one explicitly with `voyager continue --thread <id>`."
                                % (cwd, attach_status)),
                    "continuity_info": continuity_info}
        _log_event({"ts": time.time(), "event": "no_context",
                    "cwd": cwd, "session_id": session_id,
                    "attach_status": attach_status})
        return {"status": "no_thread", "attach_status": attach_status,
                "continuity_info": continuity_info}

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
                "composer_mode": payload.get("composer_mode"),
                "attach_status": out["attach_status"]})
    return out


def emit(result: Dict[str, Any]) -> int:
    """Write Cursor's envelope; always exit 0 (fail-open).

    Cursor reads a top-level ``additional_context``; ``hookSpecificOutput`` is
    not part of its contract.  Its docs document no size limit for the field, so
    the shared cap is applied defensively: a coherent capped document plus a
    spilled full copy beats an unknown provider-side cut.
    """
    try:
        if result.get("status") == "context_ready" and result.get("context"):
            context, spilled = cap_tiered_with_note(result["context"], "cursor")
            if spilled:
                _log_event({"ts": time.time(), "event": "payload_capped",
                            "original_chars": payload_len(result["context"]),
                            "delivered_chars": payload_len(context),
                            "spilled": str(spilled)})
            payload = {"additional_context": context}
            # ensure_ascii: a GBK console must never corrupt the context, and
            # escaped JSON decodes back to the exact same string.
            sys.stdout.write(json.dumps(payload, ensure_ascii=True))
            sys.stdout.flush()
    except Exception as e:
        _log_event({"ts": time.time(), "event": "emit_error", "error": str(e)})
    return 0


def main() -> int:
    try:
        result = handle_cursor_session_start()
    except Exception as e:
        _log_event({"ts": time.time(), "event": "handler_crash", "error": str(e)})
        return 0
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
