"""Tests for `scripts/verify_claude_sessionstart.py` -- the e2e probe.

The script is loaded by file path, the same way
`test_claude_session_start_hook.py` loads the hook entrypoint: importing
`voyager.integrations` drags in every provider adapter and its optional
dependencies, none of which this needs.

The regression these tests exist for: the `--e2e` check used to accept
`claude --init-only` exiting 0 as proof that the SessionStart hook fired. It is
not proof -- exit 0 only says Claude started and quit. The probe now reads the
hook's own trace log and demands an invocation whose `session_id` is not the
synthetic one the script itself uses in check 4, and `find_real_trigger` is
where that decision lives.

`TestE2EVerdict` runs the probe for real against a fake `claude` and a scratch
profile, so the whole decision table is covered rather than just the helpers.
That test also pins a Windows-specific defect it found: `claude` is normally a
`claude.CMD` shim, and `subprocess` does not consult PATHEXT the way a shell
does, so invoking the bare name raised `FileNotFoundError` (WinError 2) even
though `shutil.which()` had just resolved it. The probe now runs the resolved
path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFIER = REPO_ROOT / "scripts" / "verify_claude_sessionstart.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "voyager_sessionstart_verifier_under_test", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


# --- hook_log_path ----------------------------------------------------------

def test_hook_log_path_honours_voyager_log_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("VOYAGER_LOG_DIR", str(tmp_path / "logs"))
    assert verifier.hook_log_path() == tmp_path / "logs" / "provider-hooks.jsonl"


def test_hook_log_path_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("VOYAGER_LOG_DIR", raising=False)
    assert verifier.hook_log_path(tmp_path) == (
        tmp_path / ".voyager" / "logs" / "provider-hooks.jsonl")


# --- read_hook_events -------------------------------------------------------

def test_read_hook_events_skips_unparseable_lines(tmp_path):
    """A truncated line is a logging problem, not a verification result."""
    log = tmp_path / "provider-hooks.jsonl"
    log.write_text(
        json.dumps({"event": "SessionStart_parsed", "session_id": "abc"}) + "\n"
        + "{ truncated\n"
        + "\n"
        + json.dumps({"event": "other"}) + "\n",
        encoding="utf-8",
    )
    assert [e["event"] for e in verifier.read_hook_events(log)] == [
        "SessionStart_parsed", "other"]


def test_read_hook_events_missing_file_is_empty(tmp_path):
    assert verifier.read_hook_events(tmp_path / "nope.jsonl") == []


# --- find_real_trigger ------------------------------------------------------

def test_find_real_trigger_ignores_the_synthetic_session_id():
    """The script's own check-4 invocation must not satisfy the e2e probe."""
    events = [{"event": "SessionStart_parsed",
               "session_id": verifier.SYNTHETIC_SESSION_ID,
               "cwd": "X"}]
    assert verifier.find_real_trigger(events) is None


def test_find_real_trigger_accepts_claudes_own_session_id():
    events = [{"event": "SessionStart_parsed",
               "session_id": verifier.SYNTHETIC_SESSION_ID, "cwd": "X"},
              {"event": "SessionStart_parsed",
               "session_id": "3f9c1e2a-real", "cwd": "E:/proj"}]
    assert verifier.find_real_trigger(events) == ("3f9c1e2a-real", "E:/proj")


def test_find_real_trigger_ignores_events_without_a_session_id():
    events = [{"event": "hook_execution_env"},
              {"event": "SessionStart_parsed", "session_id": None},
              {"event": "SessionStart_parsed"}]
    assert verifier.find_real_trigger(events) is None


def test_saw_hook_without_stdin():
    assert verifier.saw_hook_without_stdin([{"event": "SessionStart_no_stdin"}])
    assert not verifier.saw_hook_without_stdin(
        [{"event": "SessionStart_parsed", "session_id": "x"}])


# --- freshness is judged by timestamp, not file position --------------------
#
# The hook's trace log trims itself to its last ~200 KB once it passes 1 MB
# (`_append_jsonl`). A probe that took a record *count* before running claude
# and sliced `events[count:]` afterwards would silently drop the record it was
# looking for whenever a trim happened in between -- a false FAIL on the one
# run that is supposed to settle the question. Filtering on the `ts` field the
# hook stamps on every record is immune to that.

def test_find_real_trigger_ignores_records_written_before_the_probe():
    """The check-4 synthetic record predates the probe and must not count."""
    events = [{"event": "SessionStart_parsed", "session_id": "claude-real",
               "cwd": "E:/proj", "ts": 100.0}]
    assert verifier.find_real_trigger(events, since=200.0) is None


def test_find_real_trigger_accepts_a_record_written_during_the_probe():
    events = [{"event": "SessionStart_parsed", "session_id": "claude-real",
               "cwd": "E:/proj", "ts": 200.0}]
    assert verifier.find_real_trigger(events, since=200.0) == (
        "claude-real", "E:/proj")


def test_find_real_trigger_rejects_a_record_with_no_timestamp():
    """Freshness cannot be established, and the probe demands positive proof."""
    events = [{"event": "SessionStart_parsed", "session_id": "claude-real"}]
    assert verifier.find_real_trigger(events, since=200.0) is None


def test_saw_hook_without_stdin_honours_since():
    events = [{"event": "SessionStart_no_stdin", "ts": 100.0}]
    assert not verifier.saw_hook_without_stdin(events, since=200.0)
    assert verifier.saw_hook_without_stdin(events, since=50.0)


# --- the probe's verdict, run for real --------------------------------------
#
# These drive `main()` in a subprocess against a fake `claude` and a scratch
# profile, so they cover the decision the whole script exists to make. The
# helper tests above cannot catch a wiring mistake between the pieces.
#
# Windows-only: the defect they pin is `.cmd` shim resolution, and building a
# fake `claude` that PATH can find means shipping a `claude.cmd`.

def _scratch_profile(tmp_path: Path) -> Path:
    """A throwaway profile with a SessionStart hook that does nothing.

    The hook deliberately writes no trace record: the trace log must only ever
    contain records produced by the fake `claude`, so the assertions cannot be
    satisfied by check 4's own invocation.
    """
    home = tmp_path / "scratch-home"
    (home / ".claude").mkdir(parents=True)
    hook = home / "noop_hook.py"
    hook.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    settings = {"hooks": {"SessionStart": [{
        "matcher": "startup",
        "hooks": [{"type": "command",
                   "command": f'"{sys.executable}" "{hook}"',
                   "timeout": 120}],
    }]}}
    (home / ".claude" / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8")
    return home


def _install_fake_claude(bin_dir: Path, mode: str) -> None:
    """Put a `claude.cmd` on PATH that records what it was asked to do.

    `mode` decides what lands in the trace log: "fire" a real invocation,
    "nostdin" one with no payload, "silent" nothing at all.
    """
    body = bin_dir / "fake_claude_body.py"
    body.write_text(textwrap.dedent(f"""
        import json, os, sys, time
        mode = {mode!r}
        log = os.path.join(os.environ["VOYAGER_LOG_DIR"], "provider-hooks.jsonl")
        record = None
        if mode == "fire":
            record = {{"event": "SessionStart_parsed",
                      "session_id": "claude-real-0001", "cwd": "E:/proj"}}
        elif mode == "nostdin":
            record = {{"event": "SessionStart_no_stdin", "cwd": "E:/proj"}}
        if record is not None:
            record["ts"] = time.time()
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\\n")
        sys.exit(0)
    """).lstrip(), encoding="utf-8")
    (bin_dir / "claude.cmd").write_text(
        f'@echo off\r\n"{sys.executable}" "{body}" %*\r\n', encoding="ascii")


def _run_probe(tmp_path: Path, mode: str):
    home = _scratch_profile(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fake_claude(bin_dir, mode)
    logs = tmp_path / "logs"
    logs.mkdir()

    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["VOYAGER_LOG_DIR"] = str(logs)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)

    return subprocess.run(
        [sys.executable, str(VERIFIER), "--e2e", "--home", str(home)],
        capture_output=True,
        text=True,
        # The verifier promises UTF-8 on stdout (`_configure_stdout()`), and its
        # output is not ASCII-only -- the INFO line for an empty payload contains
        # an em dash. Decoding with the locale default instead (GBK on a zh-CN
        # Windows) kills the reader thread, so `proc.stdout` comes back as None
        # and every assertion below fails as `TypeError: argument of type
        # 'NoneType' is not iterable`, which reads as a probe failure rather than
        # as a harness bug. `errors="strict"` is deliberate: the UTF-8 promise is
        # part of the contract under test, so a non-UTF-8 byte should turn the
        # test red rather than be silently replaced.
        encoding="utf-8",
        errors="strict",
        env=env,
        timeout=300,
    )


@pytest.mark.skipif(
    os.name != "nt",
    reason="the .cmd shim resolution this covers is Windows-specific")
@pytest.mark.parametrize("mode, expected_rc, expected", [
    # The hook fired: only positive evidence turns this into a PASS.
    ("fire", 0, "fired with Claude's own session_id='claude-real-0001'"),
    # The trigger works but Claude handed the hook nothing to parse.
    ("nostdin", 1, "received no stdin payload"),
    # Nothing fired at all -- and `claude --init-only` still exited 0, which is
    # exactly why the exit code alone was never proof.
    ("silent", 1, "no hook invocation recorded"),
])
def test_e2e_verdict(tmp_path, mode, expected_rc, expected):
    proc = _run_probe(tmp_path, mode)
    assert expected in proc.stdout, (
        f"mode={mode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    # Distinguish "the hook did not fire" from "claude never started" -- the
    # latter also ends in rc=1, which would let the "silent" case pass for
    # entirely the wrong reason.
    assert "could not start" not in proc.stdout, (
        f"mode={mode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert proc.returncode == expected_rc, (
        f"mode={mode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


@pytest.mark.skipif(
    os.name != "nt",
    reason="the .cmd shim resolution this covers is Windows-specific")
def test_e2e_verdict_proves_the_resolved_shim_is_runnable(tmp_path):
    """The probe must be able to *start* a `claude.cmd` shim on Windows.

    Regression guard: invoking the bare name raises FileNotFoundError
    (WinError 2) because `subprocess` does not do PATHEXT lookup, so the run
    died with a traceback instead of a verdict. The fake `claude` here is a
    `.cmd`, so a PASS can only happen if the resolved path was used.
    """
    proc = _run_probe(tmp_path, "fire")
    assert "could not start" not in proc.stdout
    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


@pytest.mark.skipif(
    os.name != "nt",
    reason="the .cmd shim resolution this covers is Windows-specific")
def test_e2e_probe_captures_the_verifier_output_as_utf8(tmp_path):
    """The probe's output is UTF-8 and must be captured *as* UTF-8.

    `_configure_stdout()` makes the verifier promise UTF-8 on its streams, and
    its output is not ASCII-only: the INFO line printed when the hook injects
    nothing contains an em dash. Capturing that with the locale default instead
    (GBK on a zh-CN Windows) kills the reader thread, `proc.stdout` becomes
    None, and every assertion in `test_e2e_verdict` then fails as
    `TypeError: argument of type 'NoneType' is not iterable` -- a harness bug
    wearing the costume of a probe failure.

    Pins both halves of the contract: the capture produced a `str` at all, and
    the non-ASCII text survived intact rather than being mangled by a lossy
    decode. No sentinel is added to the verifier for this -- the em dash is
    already there, and asserting on it keeps the test honest about what the
    real output contains.
    """
    proc = _run_probe(tmp_path, "nostdin")

    assert isinstance(proc.stdout, str), (
        f"stdout was not captured as text: {proc.stdout!r} / "
        f"stderr: {proc.stderr!r}")
    assert "—" in proc.stdout, (
        f"the verifier's non-ASCII output did not survive the capture:\n"
        f"{proc.stdout}")


# --- the probe must not regress to an exit-code-only check ------------------

def test_e2e_probe_requires_positive_evidence_the_hook_ran():
    """Guard against 'simplifying' the probe back to `returncode == 0`.

    A clean exit only proves Claude started and quit; the hook may never have
    been invoked at all. If this fails, the `--e2e` check has stopped proving
    the thing it is named after.
    """
    source = VERIFIER.read_text(encoding="utf-8")
    assert "read_hook_events(" in source, (
        "the --e2e probe no longer inspects the hook trace log")
    assert "find_real_trigger(" in source, (
        "the --e2e probe no longer looks for a real hook invocation")
    assert "since=probe_started" in source, (
        "the --e2e probe no longer scopes its search to the probe window, so a "
        "self-trimming log can hide the record it is looking for")
    assert "SYNTHETIC_SESSION_ID" in source


def test_e2e_probe_invokes_the_resolved_claude_path():
    """Guard the Windows fix: the bare name cannot be spawned.

    `shutil.which("claude")` resolves to `claude.CMD` on Windows, but
    `subprocess.run(["claude", ...])` does not do PATHEXT lookup and raises
    FileNotFoundError (WinError 2). The resolved path must be the one used.
    """
    source = VERIFIER.read_text(encoding="utf-8")
    assert '[claude_path, "--init-only"]' in source, (
        "the --e2e probe is invoking the bare name again")
    assert '["claude", "--init-only"]' not in source
