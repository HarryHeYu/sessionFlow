"""Membership invalidation for the continuation-context cache.

The invariant this file pins:

    same thread membership + same inputs  -> cache hit
    thread membership changes             -> cache miss / recompile

Crucially it must NOT depend on "the newly attached session's updated_at happens
to be later than compiled_at" -- membership itself is the semantic change.

Written for the A/B decision on the unreviewed `startup.py` fix:

  Variant A = thread refresh only          (member_count fingerprint removed)
  Variant B = thread refresh + member_count fingerprint

`test_pruning_a_member_invalidates_the_cache` is the discriminating case: pruning
a member deletes its `sessions` row, so `thread_members()`'s JOIN silently drops
it -- the effective membership changed while neither `threads.updated_at` nor any
remaining member's `updated_at` moved.

Run with any interpreter (stdlib unittest):
    python tests/test_context_cache_membership.py
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voyager.model import new_event, new_session  # noqa: E402
from voyager.startup import _context_cache_key, startup_continuity  # noqa: E402
from voyager.store import Store  # noqa: E402

PROVIDER = "claude"
BUDGET = "auto"


def _fake_compiler(context, calls):
    def _compile(store=None, cwd=None, provider=None, native_session_id=None,
                 thread_id=None, repo=None, goal=None, budget=None,
                 target=None, sync=True, context_format=None):
        calls.append({"thread_id": thread_id, "budget": budget})
        return {"continuity_available": True, "context": context}

    return _compile


class MembershipCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "workrepo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)],
                       capture_output=True, timeout=30)
        self.store = Store(Path(self._tmp.name) / "index.db")
        self.tid = self.store.thread_create(
            repo_root=str(self.repo), title="membership", goal="pin invalidation")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _add_session(self, sid, *, updated_at=None):
        s = new_session(id=sid, provider=PROVIDER, native_session_id=sid.split(":")[1],
                        title=sid, started_at=1.0,
                        updated_at=updated_at if updated_at is not None else time.time(),
                        repo_root=str(self.repo), cwd=str(self.repo))
        ev = new_event(sid=sid, seq=1, kind="user", role="user", content="hello")
        src = Path(self._tmp.name) / (sid.replace(":", "_") + ".jsonl")
        src.write_text("{}", encoding="utf-8")
        self.store.replace_session(s, [ev], PROVIDER, src)
        return sid

    def _run(self, calls=None):
        calls = calls if calls is not None else []
        with mock.patch("voyager.auto.get_continuation_context",
                        _fake_compiler("BUNDLE", calls)):
            result = startup_continuity(provider=PROVIDER, cwd=str(self.repo),
                                        native_session_id=None, auto_attach=True,
                                        budget=BUDGET, store=self.store)
        return result, calls

    # 1 -------------------------------------------------------------------
    def test_attaching_a_new_member_invalidates_the_cache(self):
        self._add_session("claude:a")
        self.store.thread_attach(self.tid, "claude:a")
        self._run()
        self._add_session("claude:b")
        self.store.thread_attach(self.tid, "claude:b")
        _result, calls = self._run()
        self.assertTrue(calls, "a new member must force a recompile")

    # 2 -------------------------------------------------------------------
    def test_pending_resolution_attaching_a_member_invalidates_the_cache(self):
        """The pending path attaches through the same primitive."""
        self._add_session("claude:a")
        self.store.thread_attach(self.tid, "claude:a")
        self._run()
        # what auto.py's pending resolution does when it matches a candidate
        self._add_session("claude:pend")
        self.store.pending_record(self.tid, PROVIDER, native_session_id="pend",
                                  cwd=str(self.repo))
        self.store.thread_attach(self.tid, "claude:pend")
        _result, calls = self._run()
        self.assertTrue(calls, "a member attached by pending resolution must invalidate")

    # 3 -------------------------------------------------------------------
    def test_an_older_member_attaching_invalidates_the_cache(self):
        """Membership is the semantic change -- not the new member's timestamp."""
        self._add_session("claude:a")
        self.store.thread_attach(self.tid, "claude:a")
        self._run()
        time.sleep(0.01)
        self._add_session("claude:old", updated_at=1.0)     # far older than compiled_at
        self.store.thread_attach(self.tid, "claude:old")
        _result, calls = self._run()
        self.assertTrue(calls, "an older member still changes membership")

    # 4 -------------------------------------------------------------------
    def test_no_membership_change_stays_a_cache_hit(self):
        self._add_session("claude:a")
        self.store.thread_attach(self.tid, "claude:a")
        self._run()
        _result, calls = self._run()
        self.assertEqual(calls, [], "unchanged membership must stay a cache hit")

    # 5 -------------------------------------------------------------------
    def test_a_payload_without_member_count_is_still_compatible(self):
        """Old cache rows predate the field; they must not be treated as corrupt."""
        self._add_session("claude:a")
        self.store.thread_attach(self.tid, "claude:a")
        self._run()
        key = _context_cache_key(self.tid, PROVIDER, BUDGET, "flat")
        raw = self.store.meta_get(key)
        self.assertIsNotNone(raw, "cache row must exist")
        import json
        payload = json.loads(raw)
        payload.pop("member_count", None)                   # simulate a pre-fix row
        self.store.meta_set(key, json.dumps(payload))
        _result, calls = self._run()
        self.assertEqual(calls, [], "a pre-fix payload with unchanged membership is a hit")

    # 6 --- discriminating -------------------------------------------------
    def test_pruning_a_member_invalidates_the_cache(self):
        """Pruning deletes the member's session row: membership changes while
        neither the thread nor any remaining member is touched."""
        self._add_session("claude:a")
        self._add_session("claude:b")
        self.store.thread_attach(self.tid, "claude:a")
        self.store.thread_attach(self.tid, "claude:b")
        self._run()
        self.store.con.execute("DELETE FROM sessions WHERE id=?", ("claude:a",))
        self.store.con.commit()
        _result, calls = self._run()
        self.assertTrue(calls, "a pruned member must force a recompile")


if __name__ == "__main__":
    unittest.main(verbosity=2)
