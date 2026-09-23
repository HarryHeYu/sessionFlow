"""Tests for the Claude Code SessionStart hook entrypoint.

Written against the standard library's ``unittest`` so they run anywhere with
no install step:

    python tests/test_claude_session_start_hook.py
    python -m unittest tests.test_claude_session_start_hook -v

pytest collects these too (``python -m pytest tests/test_claude_session_start_hook.py``)
because it understands ``unittest.TestCase``.

The entrypoint is loaded by *file path* on purpose: importing the
``voyager.integrations`` package drags in every provider adapter and their
optional dependencies, none of which are relevant here.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "voyager" / "integrations" / "claude_session_start.py"

# Claude Code's documented cap for hook JSON string fields.
CLAUDE_HOOK_STRING_CAP = 10_000


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "voyager_claude_session_start_under_test", ENTRYPOINT
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_entrypoint()


class HookTestCase(unittest.TestCase):
    """Hermetic base: isolated spill dir, no debug-log writes, captured stdio."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.spill_dir = Path(self._tmp.name) / "context"

        self._env_backup = os.environ.get("VOYAGER_CONTEXT_DIR")
        os.environ["VOYAGER_CONTEXT_DIR"] = str(self.spill_dir)
        self.addCleanup(self._restore_env)

        self._real_log_debug = hook._log_debug
        hook._log_debug = lambda *args, **kwargs: None
        self.addCleanup(self._restore_log_debug)

    def _restore_env(self):
        if self._env_backup is None:
            os.environ.pop("VOYAGER_CONTEXT_DIR", None)
        else:
            os.environ["VOYAGER_CONTEXT_DIR"] = self._env_backup

    def _restore_log_debug(self):
        hook._log_debug = self._real_log_debug

    def _capture(self, func):
        """Run func with stdout/stderr captured; return (returncode, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func()
        return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# payload cap / spill
# ---------------------------------------------------------------------------

class TestSpillContext(HookTestCase):

    def test_cap_stays_under_claude_limit(self):
        self.assertLess(hook.MAX_ADDITIONAL_CONTEXT_CHARS, CLAUDE_HOOK_STRING_CAP)

    def test_small_context_passes_through_untouched(self):
        payload, spilled = hook._spill_context("hello")
        self.assertEqual(payload, "hello")
        self.assertIsNone(spilled)
        self.assertFalse(self.spill_dir.exists())

    def test_context_exactly_at_cap_is_not_truncated(self):
        context = "x" * hook.MAX_ADDITIONAL_CONTEXT_CHARS
        payload, spilled = hook._spill_context(context)
        self.assertEqual(payload, context)
        self.assertIsNone(spilled)

    def test_oversized_context_is_truncated_and_fully_spilled(self):
        context = "y" * (hook.MAX_ADDITIONAL_CONTEXT_CHARS + 5000)

        payload, spilled = hook._spill_context(context)

        self.assertIsNotNone(spilled)
        self.assertTrue(spilled.exists())
        # Nothing is lost: the file holds the complete bundle.
        self.assertEqual(spilled.read_text(encoding="utf-8"), context)
        # The payload keeps the head, advertises the file, stays near the cap.
        self.assertTrue(payload.startswith("y" * 100))
        self.assertIn(str(spilled), payload)
        self.assertIn("truncated", payload)
        self.assertLess(len(payload), hook.MAX_ADDITIONAL_CONTEXT_CHARS + 400)

    def test_prune_keeps_only_the_newest_bundles(self):
        self.spill_dir.mkdir(parents=True, exist_ok=True)
        total = hook.SPILL_KEEP + 5
        for index in range(total):
            path = self.spill_dir / f"claude-sessionstart-{index}-{os.getpid()}.md"
            path.write_text("x", encoding="utf-8")
            stamp = 1_700_000_000 + index
            os.utime(path, (stamp, stamp))

        hook._prune_spills(self.spill_dir)

        survivors = sorted(self.spill_dir.glob("claude-sessionstart-*.md"))
        self.assertEqual(len(survivors), hook.SPILL_KEEP)
        # Compare by the index encoded in the name — sorting the paths
        # lexicographically would put "10" before "9".
        kept = {int(p.name.split("-")[2]) for p in survivors}
        expected = set(range(total - hook.SPILL_KEEP, total))
        self.assertEqual(kept, expected)


# ---------------------------------------------------------------------------
# output protocol
# ---------------------------------------------------------------------------

class TestPayloadLengthSemantics(HookTestCase):
    """Claude Code is JavaScript, so its cap counts UTF-16 code units.

    Measuring with Python's `len()` (code points) made the cap silently fail to
    cap: 9000 emoji is 9000 code points but 18,000 UTF-16 units.
    """

    def test_bmp_text_costs_one_unit_per_character(self):
        text = "abc \u4e2d\u6587 \u00e9"
        self.assertEqual(hook._payload_len(text), len(text))

    def test_astral_characters_cost_two_units(self):
        self.assertEqual(hook._payload_len("\U0001F600"), 2)
        self.assertEqual(hook._payload_len("a\U0001F600b"), 4)
        self.assertEqual(hook._payload_len("\U0001F600" * 100), 200)

    def test_payload_len_never_raises_on_lone_surrogates(self):
        self.assertEqual(hook._payload_len("\ud800"), 1)

    def test_truncate_respects_the_unit_budget_for_all_astral_text(self):
        context = "\U0001F600" * (hook.MAX_ADDITIONAL_CONTEXT_CHARS * 2)
        head = hook._truncate_to_budget(context, hook.MAX_ADDITIONAL_CONTEXT_CHARS)
        self.assertLessEqual(hook._payload_len(head),
                             hook.MAX_ADDITIONAL_CONTEXT_CHARS)
        # 9000 units / 2 units per emoji = 4500 emoji
        self.assertEqual(len(head), hook.MAX_ADDITIONAL_CONTEXT_CHARS // 2)

    def test_truncate_respects_the_unit_budget_for_mixed_text(self):
        context = ("a\U0001F600" * hook.MAX_ADDITIONAL_CONTEXT_CHARS)
        head = hook._truncate_to_budget(context, 1000)
        self.assertLessEqual(hook._payload_len(head), 1000)

    def test_truncate_edge_cases(self):
        self.assertEqual(hook._truncate_to_budget("abc", 0), "")
        self.assertEqual(hook._truncate_to_budget("abc", -5), "")
        self.assertEqual(hook._truncate_to_budget("", 10), "")
        self.assertEqual(hook._truncate_to_budget("abc", 10), "abc")

    def test_emoji_payload_is_capped_in_utf16_units(self):
        """The regression: 9000 emoji used to be emitted un-spilled."""
        context = "\U0001F600" * hook.MAX_ADDITIONAL_CONTEXT_CHARS
        payload, spilled = hook._spill_context(context)
        self.assertLessEqual(hook._payload_len(payload),
                             hook.MAX_ADDITIONAL_CONTEXT_CHARS)
        self.assertIsNotNone(spilled, "an over-budget bundle must be spilled")

    def test_emoji_payload_round_trips_through_the_hook_json(self):
        context = "\U0001F600" * (hook.MAX_ADDITIONAL_CONTEXT_CHARS * 2)
        payload, _ = hook._spill_context(context)
        _, out, _ = self._capture(
            lambda: hook.emit_claude_hook_output(
                {"status": "context_ready", "context": context}))
        decoded = json.loads(out)
        emitted = decoded["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(emitted, payload)
        self.assertLessEqual(hook._payload_len(emitted),
                             hook.MAX_ADDITIONAL_CONTEXT_CHARS)


class TestAppendJsonlRotation(HookTestCase):
    """Rotation must never leave the file in an unparseable state."""

    def _write_records(self, path, count, pad):
        hook._append_jsonl(path, {"i": 0, "pad": "x" * pad})
        for i in range(1, count):
            hook._append_jsonl(path, {"i": i, "pad": "x" * pad})

    def test_rotation_keeps_the_file_valid_jsonl(self):
        path = Path(self._tmp.name) / "rot.jsonl"
        with mock.patch.object(hook, "LOG_MAX_BYTES", 4096), \
             mock.patch.object(hook, "LOG_KEEP_BYTES", 1024):
            self._write_records(path, 200, 100)
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
        self.assertTrue(lines, "rotation must not empty a file that keeps growing")
        for ln in lines:
            json.loads(ln)  # every retained line must parse

    def test_rotation_without_a_newline_in_the_window_resets_cleanly(self):
        path = Path(self._tmp.name) / "nonl.jsonl"
        # A single >1MB line with no newline: the retained tail is one
        # unterminated fragment and must be dropped, not kept.
        path.write_bytes(b"z" * 2_000_000)
        hook._append_jsonl(path, {"i": "after"})
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1]), {"i": "after"})


class TestEmitHookOutput(HookTestCase):

    def test_context_ready_emits_claude_protocol(self):
        result = {"status": "context_ready", "context": "GOAL: finish the roadmap"}

        code, out, err = self._capture(
            lambda: hook.emit_claude_hook_output(result))

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)  # must be parseable
        # No bespoke top-level keys — Claude Code ignores those.
        self.assertEqual(list(payload), ["hookSpecificOutput"])
        hook_output = payload["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "SessionStart")
        self.assertEqual(hook_output["additionalContext"], "GOAL: finish the roadmap")

    def test_no_thread_is_silent_and_non_blocking(self):
        code, out, err = self._capture(
            lambda: hook.emit_claude_hook_output({"status": "no_thread"}))

        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_error_goes_to_stderr_with_exit_2(self):
        code, out, err = self._capture(
            lambda: hook.emit_claude_hook_output(
                {"status": "error", "message": "boom"}))

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("boom", err)

    def test_ready_without_context_is_reported_not_silently_dropped(self):
        code, out, err = self._capture(
            lambda: hook.emit_claude_hook_output(
                {"status": "context_ready", "context": ""}))

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("no context", err)


# ---------------------------------------------------------------------------
# stdout isolation
# ---------------------------------------------------------------------------

class TestPipelineIsolation(HookTestCase):

    def test_pipeline_stdout_is_suppressed(self):
        """A stray print() would splice into the hook JSON and break it."""
        original = hook.handle_claude_session_start

        def noisy_pipeline():
            print("this would corrupt the hook payload")
            return {"status": "no_thread"}

        hook.handle_claude_session_start = noisy_pipeline
        self.addCleanup(
            lambda: setattr(hook, "handle_claude_session_start", original))

        result, out, err = self._capture(hook._run_pipeline_quietly)

        self.assertEqual(result, {"status": "no_thread"})
        self.assertEqual(out, "")  # nothing leaked to the real stdout


# ---------------------------------------------------------------------------
# hardening regressions
# ---------------------------------------------------------------------------

class TestHardening(HookTestCase):

    def test_utc_iso_is_valid_and_has_milliseconds(self):
        """time.strftime('%f') raises ValueError — that must not come back."""
        stamp = hook._utc_iso()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}")

    def test_hook_logging_actually_writes(self):
        """Regression: %f in strftime made every hook log write fail silently."""
        log_dir = Path(self._tmp.name) / "logs"
        os.environ["VOYAGER_LOG_DIR"] = str(log_dir)
        self.addCleanup(os.environ.pop, "VOYAGER_LOG_DIR", None)

        self._real_log_debug("claude", "unit_test_event", detail="x")

        log_file = log_dir / "provider-hooks.jsonl"
        self.assertTrue(log_file.exists(),
                        "hook logging must not be silently dead")
        record = json.loads(log_file.read_text(encoding="utf-8").strip())
        self.assertEqual(record["event"], "unit_test_event")
        self.assertEqual(record["provider"], "claude")

    def test_env_logging_actually_writes(self):
        log_dir = Path(self._tmp.name) / "logs-env"
        os.environ["VOYAGER_LOG_DIR"] = str(log_dir)
        self.addCleanup(os.environ.pop, "VOYAGER_LOG_DIR", None)

        hook._log_env_debug()

        log_file = log_dir / "claude-hook-env.jsonl"
        self.assertTrue(log_file.exists())
        record = json.loads(log_file.read_text(encoding="utf-8").strip())
        self.assertEqual(record["event"], "hook_execution_env")

    def test_payload_stays_under_cap_with_an_unwritable_spill_dir(self):
        """If spilling fails the payload must still respect the cap."""
        # Beyond Windows' MAX_PATH, so the spill write itself fails.
        long_dir = Path(self._tmp.name) / ("d" * 900)
        os.environ["VOYAGER_CONTEXT_DIR"] = str(long_dir)

        context = "z" * (hook.MAX_ADDITIONAL_CONTEXT_CHARS + 5000)
        payload, _spilled = hook._spill_context(context)

        self.assertLessEqual(len(payload), hook.MAX_ADDITIONAL_CONTEXT_CHARS)

    def test_payload_is_capped_when_the_spill_path_is_absurdly_long(self):
        """On POSIX the spill path can be long enough to overshoot the cap.

        head + note must still fit; truncating first and appending the note
        afterwards would push the payload past Claude Code's limit.
        """
        from unittest import mock

        long_dir = Path(self._tmp.name) / ("e" * 1200)

        with mock.patch.object(hook, "_spill_dir", return_value=long_dir), \
                mock.patch.object(hook, "_prune_spills", return_value=None), \
                mock.patch.object(Path, "mkdir", return_value=None), \
                mock.patch.object(Path, "write_text", return_value=None):
            context = "q" * (hook.MAX_ADDITIONAL_CONTEXT_CHARS + 100)
            payload, spilled = hook._spill_context(context)

        self.assertLessEqual(len(payload), hook.MAX_ADDITIONAL_CONTEXT_CHARS)
        self.assertIsNotNone(spilled)

    def test_note_is_dropped_when_the_path_alone_exceeds_the_cap(self):
        from unittest import mock

        long_dir = Path(self._tmp.name) / ("e" * 9500)

        with mock.patch.object(hook, "_spill_dir", return_value=long_dir), \
                mock.patch.object(hook, "_prune_spills", return_value=None), \
                mock.patch.object(Path, "mkdir", return_value=None), \
                mock.patch.object(Path, "write_text", return_value=None):
            context = "q" * (hook.MAX_ADDITIONAL_CONTEXT_CHARS + 100)
            payload, _spilled = hook._spill_context(context)

        self.assertLessEqual(len(payload), hook.MAX_ADDITIONAL_CONTEXT_CHARS)
        self.assertNotIn("Full bundle:", payload)  # note sacrificed, not the cap

    def test_prune_never_deletes_recent_bundles(self):
        """Concurrent sessions must not lose bundles they still advertise."""
        self.spill_dir.mkdir(parents=True, exist_ok=True)
        for index in range(hook.SPILL_KEEP + 5):
            path = self.spill_dir / f"claude-sessionstart-{index}-{os.getpid()}.md"
            path.write_text("x", encoding="utf-8")  # mtime = now

        hook._prune_spills(self.spill_dir)

        remaining = list(self.spill_dir.glob("claude-sessionstart-*.md"))
        self.assertEqual(len(remaining), hook.SPILL_KEEP + 5)

    def test_stdout_payload_is_pure_ascii_and_lossless(self):
        """A GBK console must not be able to mangle Chinese into '?'."""
        original = sys.stdout
        buffer = io.StringIO()
        sys.stdout = buffer
        try:
            hook._write_stdout_json({"text": "中文 context — emoji 🚀"})
        finally:
            sys.stdout = original

        raw = buffer.getvalue()
        raw.encode("ascii")  # must not raise
        self.assertEqual(json.loads(raw)["text"], "中文 context — emoji 🚀")

    def test_force_utf8_tolerates_missing_stdin(self):
        original = sys.stdin
        sys.stdin = None
        try:
            hook._force_utf8()  # must not raise
        finally:
            sys.stdin = original


class TestEnvDirOverrides(unittest.TestCase):
    """`VOYAGER_LOG_DIR` / `VOYAGER_CONTEXT_DIR` name directories and come from
    the environment, so `~` has to be expanded.  Left literal, `Path("~")` is
    *relative*, and the directory is created inside whatever the process cwd
    happens to be instead of under HOME.

    Deliberately NOT a ``HookTestCase``: its ``setUp`` rewrites
    ``VOYAGER_CONTEXT_DIR``, which is one of the variables under test here.
    """

    _VARS = ("HOME", "USERPROFILE", "VOYAGER_LOG_DIR", "VOYAGER_CONTEXT_DIR")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in self._VARS}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _point_home_at_tmp(self):
        # expanduser() reads USERPROFILE on Windows and HOME elsewhere.
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)

    def test_log_dir_expands_tilde(self):
        self._point_home_at_tmp()
        os.environ["VOYAGER_LOG_DIR"] = "~/voy-logs"

        d = hook._log_dir()
        self.assertTrue(d.is_absolute(), f"not absolute: {d}")
        self.assertEqual(d, self.home / "voy-logs")

    def test_spill_dir_expands_tilde(self):
        self._point_home_at_tmp()
        os.environ["VOYAGER_CONTEXT_DIR"] = "~/voy-ctx"

        d = hook._spill_dir()
        self.assertTrue(d.is_absolute(), f"not absolute: {d}")
        self.assertEqual(d, self.home / "voy-ctx")


# ---------------------------------------------------------------------------
# the native-start chain, end to end (everything except Claude Code itself)
# ---------------------------------------------------------------------------

def test_native_start_hook_chains_into_a_thread_membership(tmp_path, monkeypatch):
    """Drive the whole chain the way it runs on a real machine.

    The live verification has one step no test can perform here — Claude Code
    itself firing `SessionStart` (it cannot even start in this environment: its
    managed-policy read shells out to `reg.exe`, which the host blocks). Every
    step *below* that one is real code and is exercised here:

        hook stdin {session_id}      (the payload Claude Code sends)
          -> startup_continuity       records a pending attach
          -> the Claude adapter       indexes the transcript as claude:<session_id>
          -> resolve_pending_attaches matches it **by identity**
          -> the session is a WorkThread member

    This pins the invariant the identity match rests on: the `session_id` the
    hook reads from stdin is the same value the adapter derives from the
    transcript's filename. Before the pending fix nothing depended on the
    resolver honouring it; now the attach does, so a drift there would look
    exactly like "the pending never resolves".
    """
    from voyager import store as store_mod
    from voyager.cli import run_scan
    from voyager.model import new_event, new_session
    from voyager.store import Store

    # a real repo: the hook and the adapter must independently resolve the same
    # repo_root, and that only happens the way production does it
    repo = tmp_path / "workrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)],
                   capture_output=True, timeout=60)

    db_path = tmp_path / "index.db"
    monkeypatch.setattr(store_mod, "default_db_path", lambda: db_path)

    # an existing WorkThread for the repo, with one member, so this models
    # "continue the work that is already here" rather than an empty thread
    store = Store(db_path)
    tid = store.thread_create(repo_root=str(repo), title="native start",
                              goal="prove the chain")
    seed_src = tmp_path / "seed.jsonl"
    seed_src.write_text("{}\n", encoding="utf-8")
    store.replace_session(
        new_session(id="codex:prev", provider="codex", native_session_id="prev",
                    title="earlier work", started_at=1000.0, updated_at=1000.0,
                    cwd=str(repo), repo_root=str(repo), message_count=1),
        [new_event(sid="codex:prev", ts=1000.0, seq=0, kind="user",
                   content="earlier work")],
        "codex", seed_src)
    assert store.thread_attach(tid, "codex:prev") is True
    store.close()

    # Claude's transcript for a session that has *just* started. The timestamps
    # must be current: the resolver refuses sessions born before the pending.
    native = "live-0001"
    projects = tmp_path / "claude-projects"
    (projects / "E--workrepo").mkdir(parents=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    transcript = projects / "E--workrepo" / f"{native}.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in (
        {"type": "user", "sessionId": native, "cwd": str(repo),
         "gitBranch": "main", "timestamp": now_iso,
         "message": {"role": "user", "content": "continue the work"}},
        {"type": "assistant", "sessionId": native, "cwd": str(repo),
         "gitBranch": "main", "timestamp": now_iso,
         "message": {"role": "assistant", "content": "on it",
                     "model": "claude-sonnet-4"}},
    )) + "\n", encoding="utf-8")

    from voyager.adapters import load_all
    load_all()
    claude_mod = importlib.import_module("voyager.adapters.claude")
    monkeypatch.setattr(claude_mod, "PROJECTS_DIR", projects)
    monkeypatch.setenv("VOYAGER_CONTEXT_DIR", str(tmp_path / "spill"))
    monkeypatch.setenv("VOYAGER_CLAUDE_HOOK_BUDGET", "compact")

    # Claude Code fires SessionStart with its own session id on stdin
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": native, "cwd": str(repo), "source": "startup"})))

    result = hook.handle_claude_session_start()

    # The hook cannot attach a session it cannot resolve to a Voyager id, so it
    # records the intent — and that record must actually exist.
    assert result["attach_status"] == "pending_resolve", result
    store = Store(db_path)
    pends = store.pending_open(thread_id=tid)
    assert len(pends) == 1, "pending_resolve must be backed by a record"
    assert pends[0]["native_session_id"] == native
    assert store.thread_member_ids(tid) == ["codex:prev"]
    store.close()

    # the next scan indexes the transcript; the resolver matches by identity
    store = Store(db_path)
    run_scan(store, providers=["claude"], quiet=True)

    assert store.thread_member_ids(tid) == ["codex:prev", f"claude:{native}"]
    row = store.q("SELECT status, resolved_sid FROM thread_pending "
                  "WHERE thread_id=?", (tid,))[0]
    assert row["status"] == "resolved"
    assert row["resolved_sid"] == f"claude:{native}"
    store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
