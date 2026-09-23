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
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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
