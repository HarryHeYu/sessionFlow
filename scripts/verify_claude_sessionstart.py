#!/usr/bin/env python3
"""Verify the Claude Code native SessionStart hook end to end.

This exists because the original "verification" only checked that a `hooks`
key was present in settings.json and then declared success. That proves
nothing. This script checks the whole chain:

  1. settings.json is parseable and really contains a SessionStart hook.
  2. The `matcher` is one Claude Code will actually match.
  3. The hook command's interpreter and script paths exist.
  4. The command runs the way Claude Code runs it (through the system shell,
     with a SessionStart payload on stdin) and returns valid hook JSON.
  5. `additionalContext` is within Claude Code's 10,000 character cap.
  6. Optionally, `claude --init-only` fires the hook for real. That flag is
     the only headless way to run Setup + SessionStart:startup and exit, so
     it is the authoritative end-to-end probe.

Usage:
    python scripts/verify_claude_sessionstart.py
    python scripts/verify_claude_sessionstart.py --expect-context
    python scripts/verify_claude_sessionstart.py --e2e

Exit code 0 only if every check that was requested passed. `[SKIP]` lines are
reported as failures when the corresponding flag asked for them.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_ADDITIONAL_CONTEXT_CHARS = 10_000

# SessionStart matchers Claude Code matches against the session `source`.
# An empty matcher (""), "*", or an omitted matcher means "match everything".
SESSION_START_MATCHERS = {"startup", "resume", "clear", "compact", "fork"}

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"
INFO = "[INFO]"


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def load_settings(path: Path):
    """Read settings.json, tolerating a UTF-8 BOM (PowerShell writes one)."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def extract_session_start_hook(settings: dict):
    """Return (matcher, command, timeout) for the first SessionStart command hook."""
    entries = (settings.get("hooks") or {}).get("SessionStart") or []
    for entry in entries:
        matcher = entry.get("matcher", "")
        for hook in entry.get("hooks") or []:
            if hook.get("type") == "command" and hook.get("command"):
                return matcher, hook["command"], hook.get("timeout")
    return None, None, None


def matcher_is_valid(matcher) -> bool:
    """Empty / '*' / omitted match everything; otherwise it must be a real source."""
    if matcher is None:
        return True
    text = str(matcher).strip()
    if text in ("", "*"):
        return True
    # Claude Code matches pipe-separated alternatives against `source`.
    return all(part.strip().lower() in SESSION_START_MATCHERS
               for part in text.split("|") if part.strip())


def resolve_command(command: str):
    """Split the hook command and locate its interpreter.

    Returns (argv, problems). Unlike a naive extension check this actually
    resolves the interpreter on PATH, so a `python -m ...` form is validated
    rather than silently passed.
    """
    problems = []
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return None, [f"cannot parse hook command: {exc}"]
    if not argv:
        return None, ["hook command is empty"]

    interpreter = argv[0]
    if not Path(interpreter).exists() and shutil.which(interpreter) is None:
        problems.append(f"interpreter not found: {interpreter}")

    # Any remaining token that looks like a path to a file must exist.
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        if token.endswith((".py", ".exe", ".cmd", ".bat", ".ps1")):
            if not Path(token).exists():
                problems.append(f"missing: {token}")

    # A `-m module` form is only valid if that module is importable.
    if "-m" in argv:
        index = argv.index("-m")
        module = argv[index + 1] if index + 1 < len(argv) else ""
        if not module:
            problems.append("-m given without a module name")
        else:
            probe = subprocess.run(
                [interpreter, "-c", f"import {module}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
            )
            if probe.returncode != 0:
                detail = probe.stderr.decode("utf-8", "replace").strip().splitlines()
                problems.append(
                    f"module {module!r} is not importable by {interpreter}: "
                    f"{detail[-1] if detail else 'no detail'}"
                )
    return argv, problems


def run_hook_command(command: str, payload: dict, timeout: int):
    """Run the hook command through the system shell, like Claude Code does."""
    started = time.time()
    proc = subprocess.run(
        command,
        shell=True,
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return proc, time.time() - started


def main() -> int:
    _configure_stdout()

    parser = argparse.ArgumentParser()
    parser.add_argument("--e2e", action="store_true",
                        help="also run `claude --init-only` for a real trigger "
                             "(counts as FAIL if claude is missing or exits non-zero)")
    parser.add_argument("--expect-context", action="store_true",
                        help="require the hook to actually emit additionalContext "
                             "(use when running from a directory with an active WorkThread)")
    parser.add_argument("--probe-file",
                        help="path the hook is expected to touch (only meaningful "
                             "when the hook is a native probe command)")
    parser.add_argument("--timeout", type=int, default=180,
                        help="seconds to wait for the hook command")
    args = parser.parse_args()

    failures = 0
    print("=" * 66)
    print("Claude Code native SessionStart hook verification")
    print("=" * 66)

    # ---- 1. settings.json -------------------------------------------------
    path = settings_path()
    if not path.exists():
        print(f"{FAIL} {path} does not exist")
        return 1
    try:
        settings = load_settings(path)
    except Exception as exc:
        print(f"{FAIL} cannot parse {path}: {exc}")
        return 1
    bom = path.read_bytes()[:3] == b"\xef\xbb\xbf"
    print(f"{PASS} settings.json parsed (BOM present: {bom})")

    matcher, command, timeout = extract_session_start_hook(settings)
    if not command:
        print(f"{FAIL} no SessionStart command hook in settings.json")
        return 1
    print(f"{INFO} matcher={matcher!r} timeout={timeout}")
    print(f"{INFO} command={command}")

    # ---- 2. matcher ------------------------------------------------------
    if matcher_is_valid(matcher):
        print(f"{PASS} matcher {matcher!r} is one Claude Code will match")
    else:
        print(f"{FAIL} matcher {matcher!r} will never match a SessionStart "
              f"source (valid: {sorted(SESSION_START_MATCHERS)}, '', '*')")
        failures += 1

    # ---- 3. command resolves --------------------------------------------
    argv, problems = resolve_command(command)
    if problems:
        for problem in problems:
            print(f"{FAIL} {problem}")
        failures += 1
    else:
        print(f"{PASS} interpreter and script paths resolve")

    # ---- 4. run it exactly like Claude does ------------------------------
    payload = {
        "session_id": "voyager-verify-0001",
        "transcript_path": str(Path.home() / ".claude" / "projects" / "verify.jsonl"),
        "cwd": str(Path.cwd()),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    print(f"{INFO} invoking hook (timeout {args.timeout}s) ...")
    try:
        proc, elapsed = run_hook_command(command, payload, args.timeout)
    except subprocess.TimeoutExpired:
        print(f"{FAIL} hook timed out after {args.timeout}s")
        return 1

    stdout = proc.stdout.decode("utf-8", "replace").strip()
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    print(f"{INFO} exit={proc.returncode} elapsed={elapsed:.1f}s "
          f"stdout={len(stdout)}B stderr={len(stderr)}B")

    if proc.returncode != 0:
        print(f"{FAIL} hook exited {proc.returncode} "
              f"(0 is expected for SessionStart; 2 means a reported error)")
        if stderr:
            print(f"{INFO} stderr: {stderr[:400]}")
        failures += 1
    else:
        print(f"{PASS} hook exited 0")

    # ---- 5. output protocol ---------------------------------------------
    context_chars = 0
    if not stdout:
        if args.expect_context:
            print(f"{FAIL} no stdout, but --expect-context was given "
                  f"(is there an active WorkThread for {Path.cwd()}?)")
            failures += 1
        else:
            print(f"{INFO} no stdout — hook decided there was nothing to inject. "
                  f"This is valid for a clean start but does NOT demonstrate "
                  f"context injection; re-run with --expect-context from a "
                  f"directory that has an active WorkThread.")
    else:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            print(f"{FAIL} stdout is not valid JSON: {exc}")
            print(f"{INFO} first 200B: {stdout[:200]}")
            failures += 1
            parsed = None
        if parsed is not None:
            hook_out = parsed.get("hookSpecificOutput") or {}
            if hook_out.get("hookEventName") != "SessionStart":
                print(f"{FAIL} hookSpecificOutput.hookEventName is "
                      f"{hook_out.get('hookEventName')!r}, expected 'SessionStart'")
                failures += 1
            else:
                print(f"{PASS} hookSpecificOutput.hookEventName = SessionStart")
            ctx = hook_out.get("additionalContext")
            if ctx is None:
                print(f"{FAIL} payload has no additionalContext — Claude Code "
                      f"would inject nothing")
                failures += 1
            elif len(ctx) > MAX_ADDITIONAL_CONTEXT_CHARS:
                print(f"{FAIL} additionalContext is {len(ctx)} chars, over "
                      f"Claude Code's {MAX_ADDITIONAL_CONTEXT_CHARS} cap")
                failures += 1
            else:
                context_chars = len(ctx)
                print(f"{PASS} additionalContext {context_chars} chars "
                      f"(under the {MAX_ADDITIONAL_CONTEXT_CHARS} cap)")

    # ---- 6. optional real E2E via claude --init-only ---------------------
    if args.e2e:
        print("-" * 66)
        print(f"{INFO} running `claude --init-only` (Setup + "
              f"SessionStart:startup, then exit)")
        if shutil.which("claude") is None:
            print(f"{FAIL} `claude` is not on PATH, so the trigger was NOT "
                  f"verified (--e2e was requested)")
            failures += 1
        else:
            before = None
            if args.probe_file:
                probe = Path(args.probe_file)
                before = probe.stat().st_mtime if probe.exists() else None
            try:
                e2e = subprocess.run(
                    ["claude", "--init-only"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
                )
                if e2e.returncode == 0:
                    print(f"{PASS} `claude --init-only` exited 0")
                else:
                    print(f"{FAIL} `claude --init-only` exited {e2e.returncode}")
                    detail = e2e.stderr.decode("utf-8", "replace").strip()
                    if detail:
                        print(f"{INFO} stderr: {detail[:400]}")
                    failures += 1
            except subprocess.TimeoutExpired:
                print(f"{FAIL} `claude --init-only` timed out")
                failures += 1
            if args.probe_file:
                probe = Path(args.probe_file)
                if probe.exists() and (before is None or probe.stat().st_mtime != before):
                    print(f"{PASS} probe file was written: {probe}")
                else:
                    print(f"{FAIL} probe file not written: {probe}")
                    failures += 1

    # ---- summary ---------------------------------------------------------
    print("-" * 66)
    print(f"context delivered: {'yes, ' + str(context_chars) + ' chars' if context_chars else 'NOT DEMONSTRATED'}")
    if failures:
        print(f"RESULT: {failures} check(s) FAILED")
        return 1
    print("RESULT: all requested checks passed")
    if not context_chars:
        print("NOTE: the hook ran and the protocol is correct, but no context was "
              "produced in this directory, so injection is still unproven.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
