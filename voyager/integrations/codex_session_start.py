"""Codex SessionStart hook handler (native hooks, codex 0.15x+).

Codex fires ``SessionStart`` with the Claude-compatible stdin payload::

    {"session_id": ..., "cwd": ..., "hook_event_name": "SessionStart",
     "model": ..., "permission_mode": ..., "source": "startup",
     "transcript_path": ...}

and accepts the Claude-compatible stdout protocol::

    {"hookSpecificOutput": {"hookEventName": "SessionStart",
                            "additionalContext": "..."}}

Both were live-verified on codex Windows TUI in G3-A: dispatch, trust,
delivery into the native rollout as a developer message before the first
turn, and model recitation without tool calls.

This file is deliberately thin.  WorkThread resolution, tiered context
generation, cache, pending attach and ambiguity rules all live in
:func:`voyager.startup.startup_continuity` — one shared core for every
provider.  Only payload normalisation and the response envelope live here.

Fail-open contract: this handler must never break a Codex session.  Every
failure path exits 0; diagnostics go to ``~/.voyager/logs/codex-hooks.jsonl``.
"""

from __future__ import annotations

import json
import os
import re
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
        log = _log_dir() / "codex-hooks.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
            with open(log, "rb") as f:
                f.seek(-LOG_KEEP_BYTES, os.SEEK_END)
                tail = f.read()
            nl = tail.find(b"\n")
            log.write_bytes(tail[nl + 1:] if nl != -1 else b"")
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=True) + "\n")
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
    """Codex stdin payload -> ``{cwd, native_session_id}`` (both optional).

    A malformed payload must never abort the session: unknown shapes degrade
    to "no native id, cwd from the process", which is exactly the pre-hook
    behaviour minus the model's own MCP round-trip.
    """
    event: Dict[str, Any] = {}
    if stdin_raw.strip():
        try:
            parsed = json.loads(stdin_raw)
            if isinstance(parsed, dict):
                event = parsed
        except Exception:
            event = {}
    return {
        "cwd": event.get("cwd") or os.getcwd(),
        "native_session_id": event.get("session_id") or None,
    }


def canonical_cwd_from_rollout(session_id: Optional[str]) -> Optional[str]:
    """The session's cwd as Codex itself recorded it, or None.

    The hook payload can carry a non-ASCII ``cwd`` mangled by the console
    encoding, and guessing an encoding back is exactly the kind of heuristic
    that silently produces a wrong repository.  Codex already knows the answer:
    its rollout for the session starts with a ``session_meta`` record whose
    ``cwd`` is authoritative, and the rollout's filename contains the session id,
    so the lookup is a glob.  Nothing here inspects text for "mojibake" — the
    structured source either resolves or it does not.

    ``CODEX_HOME`` is honoured because that is Codex's own override.
    """
    if not session_id:
        return None
    # The id comes from stdin, and it is interpolated into a glob pattern: a `*`
    # or `?` in it would match another session's rollout and return the wrong
    # repository.  Only the documented id alphabet is accepted.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    try:
        root = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        for path in (root / "sessions").glob("**/rollout-*%s.jsonl" % session_id):
            with open(path, encoding="utf-8", errors="replace") as fh:
                for _ in range(20):          # session_meta is the first record
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if record.get("type") != "session_meta":
                        continue
                    cwd = (record.get("payload") or {}).get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        return cwd
                    return None
    except Exception:
        return None
    return None


def handle_codex_session_start(
    cwd: Optional[str] = None,
    store: Optional[Any] = None,
    stdin_raw: Optional[str] = None,
) -> Dict[str, Any]:
    """One SessionStart event -> tiered-v1 context in the Codex envelope.

    Returns the internal result dict; :func:`emit` turns it into protocol
    stdout.  ``store`` exists for tests; production resolves the real index.
    """
    if stdin_raw is None:
        stdin_raw = _read_stdin()
    payload = normalize_event(stdin_raw)
    session_id = payload["native_session_id"]
    # Order matters: an explicit cwd (tests, callers) wins, then Codex's own
    # rollout record, and only then the hook payload -- whose cwd can arrive
    # mangled when the path is non-ASCII.
    cwd = cwd or canonical_cwd_from_rollout(session_id) or payload["cwd"]

    try:
        result = startup_continuity(
            provider="codex",
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
    """Write the protocol envelope; always exit 0 (fail-open).

    The payload is capped here rather than left to the provider.  Codex caps the
    injected string at 10,000 characters and, when a payload exceeds it, keeps
    the head and the tail while eliding the middle -- for tiered-v1 that removes
    the newest L1 turns, which is exactly what the model needs.  Staying under
    the cap keeps the document coherent, and the full bundle is spilled to
    ~/.voyager/context/ so nothing is actually lost.
    """
    try:
        if result.get("status") == "context_ready" and result.get("context"):
            context, spilled = cap_tiered_with_note(result["context"], "codex")
            if spilled:
                _log_event({"ts": time.time(), "event": "payload_capped",
                            "original_chars": payload_len(result["context"]),
                            "delivered_chars": payload_len(context),
                            "spilled": str(spilled)})
            payload = {"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
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
        result = handle_codex_session_start()
    except Exception as e:
        _log_event({"ts": time.time(), "event": "handler_crash", "error": str(e)})
        return 0
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
