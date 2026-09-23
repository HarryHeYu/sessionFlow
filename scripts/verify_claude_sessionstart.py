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
     it is the authoritative end-to-end probe. A clean exit code is *not*
     accepted as proof: the check reads the hook's own trace log and requires
     an invocation carrying a `session_id` other than the synthetic one this
     script uses in check 4.

Usage:
    python scripts/verify_claude_sessionstart.py
    python scripts/verify_claude_sessionstart.py --expect-context
    python scripts/verify_claude_sessionstart.py --home /tmp/scratch-home
    python scripts/verify_claude_sessionstart.py --e2e

`--home` scopes checks 1-5 to a scratch profile, which is how the chain is
exercised in CI. The `--e2e` probe (`claude --init-only`) always reads the real
profile and therefore cannot be home-scoped.

Exit code 0 only if every check that was requested passed. `[SKIP]` lines are
reported as failures when the corresponding flag asked for them.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_ADDITIONAL_CONTEXT_CHARS = 10_000

# The session_id this script puts in its own synthetic payload (check 4).  The
# e2e probe must see a *different* id, otherwise it would be satisfied by this
# script's own invocation rather than by Claude Code's.
SYNTHETIC_SESSION_ID = "voyager-verify-0001"

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


def settings_path(home=None) -> Path:
    """Path to the settings file being verified.

    `home` exists so the chain can be exercised against a scratch profile
    (`voyager integrate install claude --home <dir>`) without touching the
    real one. It used to hardcode `Path.home()`, which made the whole script
    untestable on a machine whose real profile had no Voyager hook.
    """
    return (Path(home) if home is not None else Path.home()) / ".claude" / "settings.json"


def load_settings(path: Path):
    """Read settings.json, tolerating a UTF-8 BOM (PowerShell writes one)."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def hook_log_path(home=None) -> Path:
    """The hook trace log that `claude_session_start._log_debug()` appends to.

    Mirrors that module's `_log_dir()`: `VOYAGER_LOG_DIR` wins, else
    `<home>/.voyager/logs`.  Kept in sync deliberately -- the e2e probe reads
    this file to prove the hook ran, so a divergence here would quietly turn the
    strongest check in the script back into a no-op.
    """
    override = os.environ.get("VOYAGER_LOG_DIR")
    if override:
        return Path(override).expanduser() / "provider-hooks.jsonl"
    base = Path(home) if home is not None else Path.home()
    return base / ".voyager" / "logs" / "provider-hooks.jsonl"


def read_hook_events(path: Path):
    """Parse the hook trace log, skipping lines that are not JSON.

    A truncated or interleaved line is a logging problem, not a verification
    result, so it is ignored rather than allowed to abort the probe.
    """
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _written_since(event, since) -> bool:
    """True if the trace record was written at or after `since`.

    Freshness is judged from the `ts` field the hook stamps on every record,
    NOT from a record's offset in the file. `_append_jsonl()` trims the log to
    its last ~200 KB once it passes 1 MB, so a file position captured before
    the probe can shift out from under a slice taken after it -- which would
    silently drop the very record being looked for and report a false FAIL.
    The timestamp is unaffected by trimming.
    """
    if since is None:
        return True
    ts = event.get("ts")
    if not isinstance(ts, (int, float)):
        # No timestamp means freshness cannot be established; the probe demands
        # positive evidence, so an unverifiable record does not count.
        return False
    return ts >= since


def find_real_trigger(events, synthetic_id: str = SYNTHETIC_SESSION_ID,
                      since=None):
    """Return `(session_id, cwd)` for the first invocation Claude Code itself
    produced, or None.

    `events` are the trace records. A record carrying `synthetic_id` is this
    script's *own* check-4 invocation rather than Claude's, so it does not
    count -- which is the whole point: accepting it would make the probe pass
    without anything having fired. `since` (a wall-clock timestamp) additionally
    restricts the search to records written during the probe.
    """
    for event in events:
        if event.get("event") != "SessionStart_parsed":
            continue
        if not _written_since(event, since):
            continue
        sid = event.get("session_id")
        if sid and sid != synthetic_id:
            return sid, event.get("cwd")
    return None


def saw_hook_without_stdin(events, since=None) -> bool:
    """True if the hook ran but Claude handed it nothing to parse.

    Distinct from "the hook never ran": the trigger works, the payload plumbing
    does not. Worth reporting separately rather than lumping together.
    """
    return any(e.get("event") == "SessionStart_no_stdin"
               and _written_since(e, since) for e in events)


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
                        help="also run `claude --init-only` and require the hook "
                             "to actually fire (FAIL if claude is missing, exits "
                             "non-zero, or leaves no trace record)")
    parser.add_argument("--expect-context", action="store_true",
                        help="require the hook to actually emit additionalContext "
                             "(use when running from a directory with an active WorkThread)")
    parser.add_argument("--probe-file",
                        help="path the hook is expected to touch (only meaningful "
                             "when the hook is a native probe command)")
    parser.add_argument("--home",
                        help="profile to verify instead of the real one (checks "
                             "1-5 only; `--e2e` always exercises the real profile)")
    parser.add_argument("--timeout", type=int, default=180,
                        help="seconds to wait for the hook command")
    args = parser.parse_args()

    home = Path(args.home).expanduser() if args.home else Path.home()

    failures = 0
    print("=" * 66)
    print("Claude Code native SessionStart hook verification")
    print("=" * 66)
    print(f"{INFO} home={home}")
    if args.home and args.e2e:
        print(f"{INFO} note: --home scopes checks 1-5. `claude --init-only` always "
              f"reads the real profile, so the --e2e probe is NOT home-scoped.")

    # ---- 1. settings.json -------------------------------------------------
    path = settings_path(home)
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
        "session_id": SYNTHETIC_SESSION_ID,
        "transcript_path": str(home / ".claude" / "projects" / "verify.jsonl"),
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

        # Records written from here on are the ones Claude Code produced.
        # check 4 has already invoked the hook once (appending a record of its
        # own), so freshness is tracked by timestamp rather than by file offset:
        # the log trims itself, so offsets are not stable across the probe.
        log_path = hook_log_path()
        print(f"{INFO} watching hook trace: {log_path}")
        probe_started = time.time()

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

            # A clean exit only proves Claude started and quit. What is being
            # verified is the *hook*, so require positive evidence that it ran,
            # carrying a session_id that is not this script's synthetic one.
            new_events = read_hook_events(log_path)
            trigger = find_real_trigger(new_events, since=probe_started)
            if trigger:
                sid, trigger_cwd = trigger
                print(f"{PASS} SessionStart hook fired with Claude's own "
                      f"session_id={sid!r} (cwd={trigger_cwd})")
            elif saw_hook_without_stdin(new_events, since=probe_started):
                print(f"{FAIL} the hook ran but received no stdin payload, so "
                      f"there is no session_id to show -- the trigger works, the "
                      f"payload plumbing does not")
                failures += 1
            else:
                print(f"{FAIL} no hook invocation recorded after "
                      f"`claude --init-only`. Exit 0 does NOT prove the hook ran; "
                      f"it only proves Claude started and quit.")
                print(f"{INFO} check that the hook in {settings_path(home)} is the "
                      f"one Claude Code actually reads, and that VOYAGER_LOG_DIR "
                      f"does not point somewhere else.")
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
