"""Claude SessionStart hook handler - cross-platform entrypoint.

Called from Claude Code native hooks configuration.

Invocation (preferred — works without voyager being importable):
  "<ABSOLUTE_PYTHON>" "<ABS_PATH>/voyager/integrations/claude_session_start.py"

Invocation (only if voyager is installed/importable from any cwd):
  python -m voyager.integrations.claude_session_start

Stdin: JSON with session_id, cwd, source, etc. (from Claude Code)
Stdout: Claude Code hook JSON — {"hookSpecificOutput": {"hookEventName":
        "SessionStart", "additionalContext": "..."}} — or nothing at all.

Exit codes (Claude Code semantics for SessionStart):
  0 - success (context injected) or nothing to inject. SessionStart is a
      non-blocking event, so 0 is the correct code for the normal path.
  2 - genuine failure; Claude Code surfaces stderr to the user.
      (For SessionStart, exit 2 does NOT block the session.)
"""

import contextlib
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
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


# Hook traces are the only way to tell "the hook never ran" apart from "the hook
# ran and failed", so this logging must actually work — and must stay bounded.
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_BYTES = 200_000


def _log_dir() -> Path:
    """Directory for hook traces. Override with VOYAGER_LOG_DIR."""
    override = os.environ.get("VOYAGER_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".voyager" / "logs"


def _utc_iso() -> str:
    """ISO-8601 UTC timestamp.

    time.strftime() does NOT support %f — it raises ValueError on every
    platform. That single directive is what silently killed all hook logging
    before: the exception was swallowed by `except Exception: pass`, so "no log
    file" looked exactly like "hook never fired".
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _append_jsonl(log_file: Path, record: dict) -> None:
    """Append one JSONL record, trimming the file when it grows too large."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_file.exists() and log_file.stat().st_size > LOG_MAX_BYTES:
            with open(log_file, "rb") as handle:
                handle.seek(-LOG_KEEP_BYTES, os.SEEK_END)
                tail = handle.read()
            # Drop the partial first line so the file stays valid JSONL. If the
            # retained window contains no newline at all, the whole tail is one
            # unterminated fragment — keeping it would leave the file invalid,
            # so reset instead.
            newline = tail.find(b"\n")
            tail = tail[newline + 1:] if newline != -1 else b""
            log_file.write_bytes(tail)
    except Exception:
        pass  # trimming is best-effort; never block the write below
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _note_log_failure(exc: Exception, log_file: Path) -> None:
    """Record a logging failure so 'no log' cannot pass for 'no event'."""
    try:
        fallback = _log_dir().parent / "hook-log-errors.txt"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        with open(fallback, "a", encoding="utf-8") as handle:
            handle.write(f"{_utc_iso()} could not write {log_file}: {exc!r}\n")
    except Exception:
        pass  # nothing left to do; never interfere with the hook


def _log_debug(provider: str, event: str, **fields):
    """Non-intrusive debug logging for hook traces. Never raises."""
    log_file = _log_dir() / "provider-hooks.jsonl"
    try:
        _append_jsonl(log_file, {
            "ts": time.time(),
            "iso": _utc_iso(),
            "provider": provider,
            "event": event,
            **fields,
        })
    except Exception as exc:
        _note_log_failure(exc, log_file)


def _log_env_debug():
    """Log the execution environment for hook diagnostics. Never raises.

    The record is written on every start, because "did the hook process run at
    all" is the one question this file exists to answer. The bulky and
    sensitive fields (PATH, sys.path, HOME, PYTHONPATH) are only included when
    VOYAGER_HOOK_DEBUG is set — dumping them unconditionally grows this file on
    every session start and copies the user's environment into it for no
    diagnostic benefit.
    """
    log_file = _log_dir() / "claude-hook-env.jsonl"
    try:
        record = {
            "ts": time.time(),
            "iso": _utc_iso(),
            "event": "hook_execution_env",
            "sys_executable": sys.executable,
            "sys_version": sys.version,
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "platform": sys.platform,
        }
        if os.environ.get("VOYAGER_HOOK_DEBUG"):
            record.update({
                "sys_path": sys.path,
                "home": os.environ.get("HOME"),
                "path": os.environ.get("PATH"),
                "pythonpath": os.environ.get("PYTHONPATH"),
            })
        _append_jsonl(log_file, record)
    except Exception as exc:
        _note_log_failure(exc, log_file)


def handle_claude_session_start(cwd: Optional[str] = None) -> Dict[str, Any]:
    """Handle Claude SessionStart event.
    
    Args:
        cwd: Current working directory from Claude's stdin JSON
        
    Returns:
        {
            "status": "context_ready" | "no_thread" | "error",
            "context": str,      # Continuation bundle markdown (optional if success)
            "thread": {          # WorkThread info (optional)
                "id": str,
                "title": str,
                "members": int,
            },
            "attach_status": str,  # already_attached / auto_attached / pending / no_auto_attach
            "continuity_info": {   # Metadata about context source
                "context_source": str,
                "context_stale_cached": bool,
                "recommended_action": str,
            },
        }
    """
    # LOG EXECUTION ENVIRONMENT IMMEDIATELY - diagnostic for Claude GUI hooks
    _log_env_debug()
    
    try:
        # Read Claude's stdin JSON. Two guards matter here:
        #  - sys.stdin is None when the caller closes fd 0 (`0<&-`), and calling
        #    .isatty() on it raises AttributeError.
        #  - a TTY would make .read() block forever if a human runs this by hand.
        stdin_stream = sys.stdin
        if stdin_stream is None or stdin_stream.isatty():
            stdin_content = ""
        else:
            stdin_content = stdin_stream.read()
        
        session_id = None
        if stdin_content.strip():
            claude_event = json.loads(stdin_content)
            # Extract common fields from Claude's hook input
            cwd = cwd or claude_event.get("cwd")
            session_id = claude_event.get("session_id")  # Claude's native session ID
            
            _log_debug("claude", "SessionStart_parsed",
                       session_id=session_id, cwd=cwd)
        else:
            _log_debug("claude", "SessionStart_no_stdin", cwd=cwd)
            
        if not cwd:
            cwd = str(Path.cwd())
        
        # Use unified startup continuity primitive - pass native session_id for auto-attach
        # Budget is tunable because this hook runs on every session start and
        # blocks it. `auto` compiles a ~79 KB bundle in ~13 s; `compact` is
        # noticeably faster. Override with VOYAGER_CLAUDE_HOOK_BUDGET.
        result = startup_continuity(
            provider="claude",
            cwd=cwd,
            native_session_id=session_id,  # Pass Claude's native session ID
            auto_attach=True,  # Try to auto-attach via SessionStart
            budget=os.environ.get("VOYAGER_CLAUDE_HOOK_BUDGET", "auto"),
        )
        
        _log_debug("claude", "startup_continuity_result",
                   continuity_available=result.continuity_available,
                   thread_id=result.thread_id,
                   attach_status=result.attach_status,
                   context_stale=result.context_stale,
                   context_source=result.context_source)
        
        # Check for valid WorkThread with available context
        # Note: context_stale=True just means we compiled fresh; context is still usable
        if not result.continuity_available or not result.context:
            attach_status = result.attach_status or ""
            # Ambiguity (several active WorkThreads in this repo) is a real,
            # actionable failure. Reporting it as "no_thread" hides it from the
            # user and contradicts startup.py's own "ambiguity = explicit error"
            # contract, so surface it instead. Exit 2 is non-blocking here.
            if attach_status.startswith("ERROR_") or "ambiguous" in attach_status.lower():
                return {
                    "status": "error",
                    "message": (
                        f"voyager could not resolve a WorkThread for {cwd} "
                        f"({attach_status}). Pick one explicitly with "
                        f"`voyager continue --thread <id>`."
                    ),
                    "attach_status": attach_status,
                }
            return {
                "status": "no_thread",
                "message": "No active WorkThread found" if not result.continuity_available else "Context not available",
                "attach_status": attach_status,
                "continuity_info": {
                    "context_source": result.context_source,
                    "context_stale_cached": result.context_stale,
                    "recommended_action": result.recommended_action,
                },
            }
            
        # Return structured status - separate from attach decision
        # Context is ready regardless of auto_attach status
        attach_ready = result.attach_status == "already_attached" or result.attach_status == "auto_attached"
        
        _log_debug("claude", "SessionStart_success",
                   thread_id=result.thread_id,
                   context_length=len(result.context),
                   attach_status=result.attach_status,
                   context_source=result.context_source)
        
        response = {
            "status": "context_ready",  # Clear indication context available
            "context": result.context,
            "thread": {
                "id": result.thread_id,
                "title": f"{result.goal or 'Untitled'}"[:100],
                "repo": result.repo_root,
                "goal": result.goal,
                # No member count here: it used to be estimated as
                # len(context.split('\n')) // 50, which is not a member count by
                # any definition and was consumed by nothing. StartupContinuity
                # Result does not carry one; add a real field there if needed.
            },
            "attach_status": result.attach_status,  # Separate field for attach decision
            "continuity_info": {
                "context_source": result.context_source,
                "context_stale_cached": result.context_stale,
                "recommended_action": result.recommended_action,
            },
        }
        
        if not attach_ready:
            response["next_step"] = "session_discovery_pending"
        
        return response
        
    except Exception as e:
        import traceback
        
        _log_debug("claude", "SessionStart_error",
                   error=str(e)[:200])
        
        return {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc(),
        }


# Claude Code caps hook JSON string fields (additionalContext, systemMessage,
# initialUserMessage and plain-text stdout) at 10,000 characters. Stay under it
# and spill the full bundle to a file so nothing is lost.
MAX_ADDITIONAL_CONTEXT_CHARS = 9000


def _payload_len(text: str) -> int:
    """Length the way Claude Code measures it.

    Claude Code is a JavaScript program, so its `String.length` counts UTF-16
    code units: an astral character (an emoji, say) counts as 2. Python's
    `len()` counts code points. Measuring with `len()` means a payload of 9000
    emoji looks safe here while actually occupying 18,000 units — i.e. the cap
    would silently fail to cap. Comparing like with like is the only way this
    limit means anything.
    """
    if text.isascii():  # C-level fast path for the overwhelmingly common case
        return len(text)
    return len(text) + sum(1 for ch in text if ch > "\uffff")


def _truncate_to_budget(text: str, budget: int) -> str:
    """Cut `text` so its `_payload_len` is at most `budget`. Never raises.

    Walks character by character rather than slicing first: an overflow measured
    in UTF-16 units cannot be subtracted from a code-point count, because an
    astral character contributes two units but occupies one code point. Slicing
    by the unit overflow removes up to twice as much as needed — for all-astral
    text it removed everything.
    """
    if budget <= 0:
        return ""
    total = 0
    for index, char in enumerate(text):
        total += 2 if char > "\uffff" else 1
        if total > budget:
            return text[:index]
    return text

# Spilled bundles are diagnostic artefacts, not state — keep the newest few.
SPILL_KEEP = 10

# Never prune a bundle younger than this. Several sessions can start at once,
# and deleting a peer's freshly written file would leave that session's stdout
# advertising a path that no longer exists.
SPILL_GRACE_SECONDS = 300


def _spill_dir() -> Path:
    """Directory for spilled bundles. Override with VOYAGER_CONTEXT_DIR."""
    override = os.environ.get("VOYAGER_CONTEXT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".voyager" / "context"


def _spill_context(context: str):
    """Cap the hook payload; spill the full bundle to disk when it is too big.

    Returns (payload_context, spilled_path_or_None). The returned payload is
    guaranteed to be at most MAX_ADDITIONAL_CONTEXT_CHARS *UTF-16 code units*
    (see `_payload_len`), which is how Claude Code measures it.
    """
    if _payload_len(context) <= MAX_ADDITIONAL_CONTEXT_CHARS:
        return context, None

    spilled_path = None
    try:
        out_dir = _spill_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        # pid guards against two sessions starting within the same second.
        name = f"claude-sessionstart-{int(time.time())}-{os.getpid()}.md"
        spilled_path = out_dir / name
        spilled_path.write_text(context, encoding="utf-8")
        _prune_spills(out_dir)
    except Exception:
        spilled_path = None

    note = (
        "\n\n---\n"
        f"[Voyager] Continuation bundle truncated for the hook payload "
        f"({_payload_len(context)} chars total)."
    )
    if spilled_path:
        note += f"\nFull bundle: {spilled_path}"

    # Measure the note *with* the head: the note embeds the spill path, which
    # can itself be long, so truncating first and appending after would overshoot
    # Claude Code's cap.
    if _payload_len(note) >= MAX_ADDITIONAL_CONTEXT_CHARS:
        note = ""  # pathological path length — never blow the cap for a note
    head_budget = MAX_ADDITIONAL_CONTEXT_CHARS - _payload_len(note)
    return _truncate_to_budget(context, head_budget) + note, spilled_path


def _prune_spills(out_dir: Path) -> None:
    """Keep the newest SPILL_KEEP bundles; never touch recent ones. Never raises."""
    try:
        cutoff = time.time() - SPILL_GRACE_SECONDS
        spills = sorted(
            out_dir.glob("claude-sessionstart-*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in spills[SPILL_KEEP:]:
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _write_stdout_json(payload: dict) -> None:
    """Write the hook payload to stdout as pure ASCII.

    `ensure_ascii=True` is deliberate: on Windows a GBK/cp936 console cannot
    encode the Chinese text in a continuation bundle, and a lossy
    `errors="replace"` would silently turn it into "?" — exactly the corruption
    this hook used to work around with a hand-rolled character filter. Escaping
    is lossless and decodes back to the same string, so the model still sees the
    original text.
    """
    text = json.dumps(payload, ensure_ascii=True)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        try:
            sys.stdout.buffer.write(text.encode("ascii"))
            sys.stdout.buffer.flush()
        except Exception:
            pass  # nothing left to try; never take the session down


def emit_claude_hook_output(result: Dict[str, Any]) -> int:
    """Emit Claude Code's expected SessionStart hook JSON; return the exit code.

    Claude Code only reads `hookSpecificOutput.additionalContext` for
    SessionStart (or plain text on stdout). A bespoke JSON shape such as
    {"status": ..., "context": ...} is parsed as JSON and then ignored, so the
    continuity context would never reach the model.
    """
    status = result.get("status")
    context = result.get("context") or ""

    if status == "context_ready" and context:
        payload_context, spilled_path = _spill_context(context)
        _write_stdout_json({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": payload_context,
            }
        })
        _log_debug(
            "claude", "hook_output_emitted",
            context_chars=len(context),
            emitted_chars=len(payload_context),
            spilled_to=str(spilled_path) if spilled_path else None,
        )
        return 0

    if status == "no_thread":
        # Nothing worth injecting — stay silent so the session starts clean.
        _log_debug("claude", "hook_output_empty", reason="no_thread")
        return 0

    # Genuine failure: exit 2 makes Claude Code show the stderr line to the user.
    # For SessionStart this is non-blocking — the session still starts.
    print(
        result.get("message")
        or f"voyager SessionStart hook returned status={status!r} with no context",
        file=sys.stderr,
    )
    _log_debug("claude", "hook_output_error",
               status=status, error=str(result.get("message"))[:200])
    return 2


def _run_pipeline_quietly():
    """Run the continuity pipeline with stdout captured.

    Anything the pipeline prints to stdout would otherwise be spliced into the
    hook JSON and make it unparseable, which Claude Code reports as a broken
    hook. Capture it, log it, and keep stdout clean for the real payload.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = handle_claude_session_start()
    stray = buffer.getvalue()
    if stray.strip():
        _log_debug("claude", "stdout_suppressed",
                   chars=len(stray), preview=stray[:200])
    return result


def _force_utf8() -> None:
    """Best-effort UTF-8 on the standard streams. Never raises.

    sys.stdin is None when fd 0 is closed, and reconfigure() can raise on an
    already-detached stream, so both cases are tolerated rather than allowed to
    abort the hook.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    """Standalone entrypoint for the Claude Code SessionStart hook."""
    # Configure UTF-8 for proper Chinese character handling
    _force_utf8()

    # --self-test: run the pipeline and dump the raw diagnostic payload instead
    # of the hook JSON. Useful to verify the entrypoint without launching Claude.
    if "--self-test" in sys.argv[1:]:
        result = _run_pipeline_quietly()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get("status") in ("context_ready", "no_thread") else 1)

    try:
        result = _run_pipeline_quietly()
    except Exception as exc:  # never take the session down with us
        print(f"voyager SessionStart hook crashed: {exc}", file=sys.stderr)
        _log_debug("claude", "hook_output_crash", error=str(exc)[:200])
        sys.exit(2)

    sys.exit(emit_claude_hook_output(result))


if __name__ == "__main__":
    main()
