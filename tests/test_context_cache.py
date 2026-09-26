"""Tests for the persisted continuation-context cache in voyager.startup.

Regression target (P1-4): the cache used to be read from `store._last_compile`,
an attribute nothing in the codebase ever wrote. Callers such as the Claude
SessionStart hook construct a fresh Store per invocation, so even a written
in-memory attribute could not have survived. Consequences were:

  * the 5-minute TTL was dead code — every session start recompiled the
    ~79 KB bundle (~13-17 s, blocking session start),
  * `context_source` was always "fresh_compile" and `compiled_at` was always
    "now", so the reported metadata was misleading.

The cache now lives in the store's `meta` table and survives process
boundaries. These tests pin that behaviour.

Run with any interpreter (stdlib unittest):
    python tests/test_context_cache.py
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

from voyager.continuity import (  # noqa: E402
    CONTEXT_FORMAT_FLAT,
    CONTEXT_FORMAT_TIERED,
)
from voyager.store import Store  # noqa: E402
from voyager.startup import (  # noqa: E402
    CONTEXT_TTL_SECONDS,
    _context_cache_key,
    _load_context_cache,
    _save_context_cache,
    startup_continuity,
)

PROVIDER = "claude"
BUDGET = "auto"


def _fake_compiler(context, calls):
    """A stand-in for voyager.auto.get_continuation_context."""

    def _compile(store=None, cwd=None, provider=None, native_session_id=None,
                 thread_id=None, repo=None, goal=None, budget=None,
                 target=None, sync=True):
        calls.append({"thread_id": thread_id, "budget": budget})
        return {"continuity_available": True, "context": context}

    return _compile


class CacheCase(unittest.TestCase):
    """Shared fixture: an isolated Store backed by a throwaway database."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="voyager-cache-test-")
        self.db_path = Path(self._tmp.name) / "index.db"
        self.store = Store(db_path=self.db_path)
        self.addCleanup(self._close_store)

    def _close_store(self):
        try:
            self.store.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def fresh_store(self):
        """A new Store on the same database — i.e. the next process."""
        return Store(db_path=self.db_path)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


class TestCacheHelpers(CacheCase):
    def test_cold_cache_is_a_miss(self):
        loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
        self.assertEqual(loaded, {"compiled_at": 0.0, "context": None})

    def test_round_trip_preserves_bundle_and_timestamp(self):
        stamp = time.time() - 12.5
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                            "BUNDLE-BODY", stamp)
        loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
        self.assertEqual(loaded["context"], "BUNDLE-BODY")
        self.assertAlmostEqual(loaded["compiled_at"], stamp, places=3)

    def test_cache_survives_a_new_store_instance(self):
        """The heart of the P1-4 fix: a fresh Store must still see the cache."""
        stamp = time.time()
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                            "BUNDLE-BODY", stamp)
        other = self.fresh_store()
        self.addCleanup(other.close)
        loaded = _load_context_cache(other, "thr_x", PROVIDER, BUDGET)
        self.assertEqual(loaded["context"], "BUNDLE-BODY")
        self.assertAlmostEqual(loaded["compiled_at"], stamp, places=3)

    def test_timestamp_is_not_restamped_on_read(self):
        stamp = time.time() - 120
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET, "B", stamp)
        loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
        self.assertLess(loaded["compiled_at"], time.time() - 100)

    def test_large_bundle_is_stored_intact(self):
        bundle = "x" * 120_000  # larger than the 9k hook payload cap
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET, bundle,
                            time.time())
        loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
        self.assertEqual(len(loaded["context"]), len(bundle))

    def test_different_budget_is_a_miss(self):
        """A bundle compiled for another budget must not be reused."""
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET, "B",
                            time.time())
        loaded = _load_context_cache(self.store, "thr_x", PROVIDER, "compact")
        self.assertIsNone(loaded["context"])

    def test_entries_are_keyed_per_thread_and_provider(self):
        _save_context_cache(self.store, "thr_a", PROVIDER, BUDGET, "A",
                            time.time())
        self.assertIsNone(
            _load_context_cache(self.store, "thr_b", PROVIDER, BUDGET)["context"])
        self.assertIsNone(
            _load_context_cache(self.store, "thr_a", "codex", BUDGET)["context"])

    def test_corrupt_entry_is_a_miss_not_a_crash(self):
        key = _context_cache_key("thr_x", PROVIDER, BUDGET)
        for bad in ("{not json", "[]", '"a string"', "null", ""):
            with self.subTest(entry=bad):
                self.store.meta_set(key, bad)
                loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
                self.assertIsNone(loaded["context"])

    def test_wrong_field_types_are_a_miss(self):
        key = _context_cache_key("thr_x", PROVIDER, BUDGET)
        cases = [
            '{"compiled_at": "soon", "budget": "auto", "context": "c"}',
            '{"compiled_at": 1.0, "budget": "auto", "context": 42}',
            '{"compiled_at": 1.0, "budget": "auto", "context": ""}',
            '{"compiled_at": 0, "budget": "auto", "context": "c"}',
            '{"compiled_at": -5, "budget": "auto", "context": "c"}',
            '{"budget": "auto", "context": "c"}',
        ]
        for bad in cases:
            with self.subTest(entry=bad):
                self.store.meta_set(key, bad)
                loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
                self.assertIsNone(loaded["context"])

    def test_missing_context_field_is_a_miss(self):
        self.store.meta_set(_context_cache_key("thr_x", PROVIDER, BUDGET),
                            '{"compiled_at": 1.0, "budget": "auto"}')
        self.assertIsNone(
            _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)["context"])

    def test_save_never_raises_when_the_store_is_broken(self):
        broken = mock.Mock()
        broken.meta_set.side_effect = RuntimeError("disk on fire")
        _save_context_cache(broken, "thr_x", PROVIDER, BUDGET, "B", time.time())


class TestMetaAccessors(CacheCase):
    def test_set_get_delete_round_trip(self):
        self.assertIsNone(self.store.meta_get("k"))
        self.assertEqual(self.store.meta_get("k", "fallback"), "fallback")
        self.store.meta_set("k", "v1")
        self.assertEqual(self.store.meta_get("k"), "v1")
        self.store.meta_set("k", "v2")  # upsert, not duplicate
        self.assertEqual(self.store.meta_get("k"), "v2")
        rows = self.store.q("SELECT COUNT(*) c FROM meta WHERE key='k'")
        self.assertEqual(rows[0]["c"], 1)
        self.store.meta_delete("k")
        self.assertIsNone(self.store.meta_get("k"))

    def test_set_survives_a_new_store_instance(self):
        self.store.meta_set("persisted", "yes")
        other = self.fresh_store()
        self.addCleanup(other.close)
        self.assertEqual(other.meta_get("persisted"), "yes")

    def test_accessors_never_raise_on_a_broken_connection(self):
        broken = Store(db_path=self.db_path)
        broken.con.close()
        self.assertIsNone(broken.meta_get("k"))
        broken.meta_set("k", "v")       # must not raise
        broken.meta_delete("k")         # must not raise


# ---------------------------------------------------------------------------
# End-to-end through startup_continuity()
# ---------------------------------------------------------------------------


class TestStartupContinuityCaching(CacheCase):
    """Drive the real primitive: real git repo, real Store, stubbed compiler."""

    def setUp(self):
        super().setUp()
        self.repo = Path(self._tmp.name) / "workrepo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)],
                       capture_output=True, timeout=30)
        self.tid = self.store.thread_create(
            repo_root=str(self.repo), title="cache test", goal="pin the cache")

    def _run(self, store=None, calls=None, budget=BUDGET,
             compiler_context="FRESH-BUNDLE", compiler_raises=None):
        """Drive startup_continuity once and return (result, recorded_calls)."""
        store = store or self.store
        calls = calls if calls is not None else []

        if compiler_raises is not None:
            def compiler(**_kwargs):
                calls.append({"raised": True})
                raise compiler_raises
        else:
            compiler = _fake_compiler(compiler_context, calls)

        with mock.patch("voyager.auto.get_continuation_context", compiler):
            result = startup_continuity(
                provider=PROVIDER,
                cwd=str(self.repo),
                native_session_id=None,
                auto_attach=True,
                budget=budget,
                store=store,
            )
        return result, calls

    def test_first_start_compiles_fresh_and_persists(self):
        result, calls = self._run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.context, "FRESH-BUNDLE")
        self.assertEqual(result.context_source, "fresh_compile")
        self.assertTrue(result.context_stale)
        self.assertAlmostEqual(result.compiled_at, time.time(), delta=30)

    def test_second_start_serves_from_cache_without_recompiling(self):
        """The headline fix: no recompile on a repeat start in the same thread."""
        self._run()
        result, calls = self._run()
        self.assertEqual(calls, [], "compiler must not run while the cache is warm")
        self.assertEqual(result.context, "FRESH-BUNDLE")
        self.assertEqual(result.context_source, "cached")
        self.assertFalse(result.context_stale)

    def test_cache_is_honoured_across_a_new_store_instance(self):
        """What actually happens on the next SessionStart: a brand-new Store."""
        first, _ = self._run()
        other = self.fresh_store()
        self.addCleanup(other.close)
        result, calls = self._run(store=other)
        self.assertEqual(calls, [])
        self.assertEqual(result.context, "FRESH-BUNDLE")
        self.assertEqual(result.context_source, "cached")
        # compiled_at must report the original compile, not "now".
        self.assertAlmostEqual(result.compiled_at, first.compiled_at, places=3)

    def test_expired_ttl_forces_a_recompile(self):
        _save_context_cache(self.store, self.tid, PROVIDER, BUDGET,
                            "OLD-BUNDLE", time.time() - CONTEXT_TTL_SECONDS - 60)
        result, calls = self._run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.context, "FRESH-BUNDLE")
        self.assertEqual(result.context_source, "fresh_compile")

    def test_stale_cache_is_still_usable_when_recompilation_fails(self):
        """Serving a known-stale bundle beats injecting nothing."""
        _save_context_cache(self.store, self.tid, PROVIDER, BUDGET,
                            "OLD-BUNDLE", time.time() - CONTEXT_TTL_SECONDS - 60)
        result, calls = self._run(compiler_context=None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.context, "OLD-BUNDLE")
        self.assertEqual(result.context_source, "cached")
        self.assertTrue(result.context_stale, "a stale fallback must be flagged")

    def test_compiler_exception_does_not_break_the_start(self):
        _save_context_cache(self.store, self.tid, PROVIDER, BUDGET,
                            "OLD-BUNDLE", time.time() - CONTEXT_TTL_SECONDS - 60)
        result, calls = self._run(compiler_raises=RuntimeError("compiler exploded"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.context, "OLD-BUNDLE")
        self.assertEqual(result.context_source, "cached")

    def test_a_newer_member_update_invalidates_the_cache(self):
        self._run()
        # Attach a member session touched after the compile.
        self.store.con.execute(
            "INSERT INTO sessions(id, provider, native_id, updated_at) "
            "VALUES (?,?,?,?)",
            (f"{PROVIDER}:later", PROVIDER, "later", time.time() + 5),
        )
        self.store.con.commit()
        self.store.con.execute(
            "INSERT OR IGNORE INTO thread_sessions VALUES (?,?,?,?)",
            (self.tid, f"{PROVIDER}:later", time.time(), 1),
        )
        self.store.con.commit()
        result, calls = self._run()
        self.assertEqual(len(calls), 1, "a newer member must invalidate the cache")
        self.assertEqual(result.context_source, "fresh_compile")

    def test_a_budget_change_forces_a_recompile(self):
        self._run()
        result, calls = self._run(budget="compact")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["budget"], "compact")
        self.assertEqual(result.context, "FRESH-BUNDLE")

    def test_cached_start_is_much_cheaper_than_a_compile(self):
        """A behavioural guard on the actual benefit: cached starts skip work."""
        self._run()
        result, calls = self._run()
        self.assertEqual(calls, [])
        self.assertEqual(result.context_source, "cached")

    def test_already_attached_session_still_receives_context(self):
        """A `compact`/`resume` start reuses the session id and is already
        attached. The old early return answered with context=None, which the
        hook reads as "nothing to inject" — so it went completely silent at
        exactly the moment re-injection matters most."""
        sid = f"{PROVIDER}:mine"
        self.store.con.execute(
            "INSERT INTO sessions(id, provider, native_id, updated_at) "
            "VALUES (?,?,?,?)",
            (sid, PROVIDER, "mine", time.time()),
        )
        self.store.con.commit()
        self.assertTrue(self.store.thread_attach(self.tid, sid))

        result, calls = self._run_with_session("mine")
        self.assertEqual(result.attach_status, "already_attached")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.context, "FRESH-BUNDLE")
        self.assertEqual(result.context_source, "fresh_compile")

    def test_already_attached_second_start_is_cached(self):
        sid = f"{PROVIDER}:mine"
        self.store.con.execute(
            "INSERT INTO sessions(id, provider, native_id, updated_at) "
            "VALUES (?,?,?,?)",
            (sid, PROVIDER, "mine", time.time()),
        )
        self.store.con.commit()
        self.assertTrue(self.store.thread_attach(self.tid, sid))

        self._run_with_session("mine")
        result, calls = self._run_with_session("mine")
        self.assertEqual(result.attach_status, "already_attached")
        self.assertEqual(calls, [])
        self.assertEqual(result.context_source, "cached")

    def test_attaching_another_member_invalidates_the_cache(self):
        """`thread_attach` bumps threads.updated_at but leaves the new member's
        own updated_at untouched, so the member-set change needs its own
        invalidation term — otherwise the stale bundle is served without the new
        member, silently, and looks like a healthy cache hit."""
        self._run()
        sid = f"{PROVIDER}:later"
        self.store.con.execute(
            "INSERT INTO sessions(id, provider, native_id, updated_at) "
            "VALUES (?,?,?,?)",
            # Deliberately older than the compile, so only the thread-level
            # touch can be responsible for the invalidation.
            (sid, PROVIDER, "later", time.time() - 10_000),
        )
        self.store.con.commit()
        self.assertTrue(self.store.thread_attach(self.tid, sid))

        result, calls = self._run()
        self.assertEqual(len(calls), 1, "the new member must invalidate the cache")
        self.assertEqual(result.context_source, "fresh_compile")

    def test_a_poisoned_cache_entry_is_recompiled_away(self):
        """A hostile timestamp must be treated as a miss and overwritten, not
        served forever."""
        self.store.meta_set(
            _context_cache_key(self.tid, PROVIDER, BUDGET),
            '{"compiled_at": Infinity, "context": "POISON"}',
        )
        result, calls = self._run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.context, "FRESH-BUNDLE")
        # ...and the poisoned row is now gone.
        loaded = _load_context_cache(self.store, self.tid, PROVIDER, BUDGET)
        self.assertEqual(loaded["context"], "FRESH-BUNDLE")

    def _run_with_session(self, native_session_id):
        calls = []
        with mock.patch("voyager.auto.get_continuation_context",
                        _fake_compiler("FRESH-BUNDLE", calls)):
            result = startup_continuity(
                provider=PROVIDER,
                cwd=str(self.repo),
                native_session_id=native_session_id,
                auto_attach=True,
                budget=BUDGET,
                store=self.store,
            )
        return result, calls


# ---------------------------------------------------------------------------
# Hostile cache input
# ---------------------------------------------------------------------------


class TestHostileCacheInput(CacheCase):
    """Regressions for the defects the adversarial round reproduced.

    Each of these made the cache worse than useless: a crafted or corrupt value
    either crashed the hook on *every* session start (with the bad row left
    behind, so the session never recovered) or froze the cache so it could never
    refresh again.
    """

    def _poison(self, raw):
        self.store.meta_set(_context_cache_key("thr_x", PROVIDER, BUDGET), raw)

    def test_huge_integer_timestamp_is_a_miss_not_an_overflow(self):
        # float(10**400) raises OverflowError. Unguarded, that escaped
        # startup_continuity (the call site sits outside the compile `try`) and
        # the hook turned it into exit 2 on every start.
        self._poison('{"compiled_at": %d, "context": "POISON"}' % 10 ** 400)
        loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
        self.assertIsNone(loaded["context"])
        self.assertEqual(loaded["compiled_at"], 0.0)

    def test_non_finite_timestamps_are_a_miss(self):
        # json.loads accepts NaN / Infinity / 1e400 by default. They survive
        # float() and then make every freshness test false (now - inf = -inf;
        # anything > nan = False), freezing the cache permanently.
        for raw in ('{"compiled_at": NaN, "context": "POISON"}',
                    '{"compiled_at": Infinity, "context": "POISON"}',
                    '{"compiled_at": -Infinity, "context": "POISON"}',
                    '{"compiled_at": 1e400, "context": "POISON"}'):
            with self.subTest(raw=raw):
                self._poison(raw)
                loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
                self.assertEqual(loaded, {"compiled_at": 0.0, "context": None})

    def test_boolean_timestamp_is_a_miss(self):
        # bool is an int subclass, so True would otherwise coerce to 1.0.
        self._poison('{"compiled_at": true, "context": "POISON"}')
        self.assertIsNone(
            _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)["context"])

    def test_loader_never_raises_on_any_crafted_entry(self):
        hostile = [
            '{"compiled_at": %d, "context": "c"}' % 10 ** 400,
            '{"compiled_at": 1e999, "context": "c"}',
            '{"compiled_at": {}, "context": "c"}',
            '{"compiled_at": [], "context": "c"}',
            '{"compiled_at": null, "context": "c"}',
            '{"compiled_at": "1.0", "context": "c"}',
            '{"context": "c"}',
            '{"compiled_at": 1.0, "context": null}',
            '{"compiled_at": 1.0, "context": ["c"]}',
            '{"compiled_at": 1.0, "context": {"a": 1}}',
            '"\ud800"',
            '[1, 2, 3]',
            '0',
            'null',
            'true',
            '',
        ]
        for raw in hostile:
            with self.subTest(raw=raw[:40]):
                self._poison(raw)
                loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
                self.assertEqual(loaded, {"compiled_at": 0.0, "context": None})

    def test_lone_surrogate_context_round_trips(self):
        # SQLite cannot UTF-8-encode a lone surrogate, so an unescaped write
        # vanished silently and the cache could never hit. ensure_ascii=True
        # escapes it, and json.loads restores the original string exactly.
        bundle = "before \ud800 after"
        self.assertTrue(
            _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                                bundle, time.time()),
            "the write must report success rather than silently vanishing",
        )
        loaded = _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)
        self.assertEqual(loaded["context"], bundle)


class TestSaveReportsFailure(CacheCase):
    """`meta_set` used to swallow every error, which made a permanently broken
    cache indistinguishable from a cold one."""

    def test_save_returns_true_on_success(self):
        self.assertTrue(_save_context_cache(
            self.store, "thr_x", PROVIDER, BUDGET, "B", time.time()))

    def test_save_returns_false_when_the_store_rejects_the_write(self):
        broken = mock.Mock()
        broken.meta_set.return_value = False
        self.assertFalse(_save_context_cache(
            broken, "thr_x", PROVIDER, BUDGET, "B", time.time()))

    def test_save_returns_false_when_the_store_raises(self):
        broken = mock.Mock()
        broken.meta_set.side_effect = RuntimeError("disk on fire")
        self.assertFalse(_save_context_cache(
            broken, "thr_x", PROVIDER, BUDGET, "B", time.time()))

    def test_meta_set_reports_failure_on_a_closed_connection(self):
        closed = Store(db_path=self.db_path)
        closed.con.close()
        self.assertFalse(closed.meta_set("k", "v"))
        self.assertFalse(closed.meta_delete("k"))


# ---------------------------------------------------------------------------
# Static regression guards
# ---------------------------------------------------------------------------


class TestNoPhantomCacheAttribute(unittest.TestCase):
    """Stop the dead `store._last_compile` read from coming back."""

    def setUp(self):
        self.src = (REPO_ROOT / "voyager" / "startup.py").read_text(
            encoding="utf-8")

    def test_last_compile_is_not_read_from_the_store(self):
        code_lines = [
            line for line in self.src.splitlines()
            if "_last_compile" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(
            code_lines, [],
            "_last_compile is never written anywhere; reading it silently "
            "disables the context cache",
        )

    def test_ttl_is_a_named_constant(self):
        self.assertIn("CONTEXT_TTL_SECONDS", self.src)
        self.assertNotIn("> 300 or", self.src)

    def test_compiled_at_reports_the_real_compile_time(self):
        self.assertNotIn('"compiled_at": time.time() if context else None',
                         self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCacheFormatIsolation(CacheCase):
    """The cache identity carries the context format (Phase C / Step B).

    A flat bundle and a tiered one are different documents for the same
    thread/provider/budget. If the format were not in the key, whichever was
    compiled first would be served for the other -- silently, and with nothing
    in the output to say it was the wrong shape.
    """

    def test_keys_differ_by_format(self):
        assert _context_cache_key("thr_x", PROVIDER, BUDGET,
                                  CONTEXT_FORMAT_FLAT) != \
            _context_cache_key("thr_x", PROVIDER, BUDGET, CONTEXT_FORMAT_TIERED)

    def test_flat_entry_is_not_served_for_a_tiered_request(self):
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET, "FLAT BODY",
                            123.0, context_format=CONTEXT_FORMAT_FLAT)
        assert _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                                   CONTEXT_FORMAT_FLAT)["context"] == "FLAT BODY"
        assert _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                                   CONTEXT_FORMAT_TIERED)["context"] is None

    def test_tiered_entry_is_not_served_for_a_flat_request(self):
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET, "TIERED BODY",
                            124.0, context_format=CONTEXT_FORMAT_TIERED)
        assert _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                                   CONTEXT_FORMAT_TIERED)["context"] == \
            "TIERED BODY"
        assert _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                                   CONTEXT_FORMAT_FLAT)["context"] is None

    def test_both_formats_coexist_for_the_same_thread(self):
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET, "FLAT BODY",
                            123.0, context_format=CONTEXT_FORMAT_FLAT)
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET, "TIERED BODY",
                            124.0, context_format=CONTEXT_FORMAT_TIERED)
        assert _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                                   CONTEXT_FORMAT_FLAT)["context"] == "FLAT BODY"
        assert _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                                   CONTEXT_FORMAT_TIERED)["context"] == \
            "TIERED BODY"

    def test_the_default_stays_flat(self):
        """Existing callers pass no format and must keep today's behaviour."""
        _save_context_cache(self.store, "thr_x", PROVIDER, BUDGET,
                            "DEFAULT BODY", 125.0)
        assert _load_context_cache(self.store, "thr_x", PROVIDER, BUDGET)[
            "context"] == "DEFAULT BODY"
