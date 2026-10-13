"""The scan pipeline: discover, parse, write, stamp, prune.

This is the core primitive ``voyager scan`` runs and ``voyager watch`` repeats.
It lives here rather than in ``voyager/cli.py`` because it is not a CLI
concern: :func:`voyager.auto.get_continuation_context` needs it too, and it
used to reach up into the command module for it with a function-local import
-- a lower layer depending on the top layer to get at a core operation.

Nothing here parses arguments, formats user output for a command, or launches
anything.  The invariants it maintains are specified in
``docs/SCAN_CONSISTENCY.md``: one session is one transaction, a source's
fingerprint describes the version that was actually read, and a multi-session
artifact is never stamped complete unless every session that claims it was
written.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from .adapters import load_all
from .adapters.base import enabled_adapters
from .store import Store

#: How many session writes may fail before the scan gives up.
#:
#: One source failing (a locked provider database, a file that vanished between
#: discovery and write) must not throw away the whole round -- that is the
#: difference between "Cursor was busy" and "you get nothing".  A failure on
#: *every* session is a different situation: the index is unwritable, and
#: grinding through 100k sessions to print 100k warnings hides it.
MAX_WRITE_FAILURES = 25

#: ...but a count alone cannot tell a lock that will clear from an index that
#: never will.  Measured (`E:\sessionflow-scratch\p14\probe_lockcost.py`): a
#: write blocked by another process fails *immediately* (SQLite refuses to wait
#: inside a transaction that has already read), so 25 of them take about a
#: second.  Aborting after that second would give up on a lock that clears in
#: eight.  The scan therefore only stops when the failures also *last*: both
#: this many, and spread over this long.
WRITE_FAILURE_GRACE_SECONDS = 30.0


def _note_write_failure(name: str, exc: BaseException, count: int) -> None:
    """Report a failed session write, bounded so a systemic failure stays readable."""
    if count <= 3:
        print(f"  ! {name}: write failed ({exc}); will retry on the next scan",
              file=sys.stderr)


def _note_derived_failure(what: str, exc: BaseException) -> None:
    """Report a failure in post-index bookkeeping that the next scan redoes.

    prune and the auto-continuity pass are both derived from the index and both
    idempotent, so a failure here must not throw away the round's indexing.
    """
    print(f"  ! {what} failed ({exc}); the index is written and the next scan"
          " will redo it", file=sys.stderr)


def _pre_read_fingerprints(sources) -> dict:
    """stat every source *before* any of them is read.

    The fingerprint recorded for a source has to describe the version that was
    indexed.  Taking it after the read meant a provider that appended during the
    scan got its new fingerprint recorded against the old content, so the next
    scan saw "unchanged" and the new turns were never indexed.  For a
    multi-session artifact the window is the whole ``ad.scan()`` call -- seconds
    to minutes for a large database, while ``voyager watch`` is scanning in the
    background next to a running provider agent.

    Only called on a round that is going to read (a no-op scan returns before
    this), so it costs one stat per source on rounds that were already going to
    touch every source.  ``None`` marks a path that could not be stat'ed, so it
    is never recorded as fresh.
    """
    out: dict = {}
    for p in sources:
        try:
            out[str(p)] = Store.source_fingerprint(p)
        except OSError:
            out[str(p)] = None
    return out


def run_scan(store: Store, providers: Optional[List[str]] = None,
             force: bool = False, quiet: bool = False) -> dict:
    """Incremental scan core (Phase 1b). Returns stats; respects mtime+size,
    never rewrites provider files, prunes vanished sources.

    A failure to write one session is isolated to that session: the scan keeps
    going, the failed source keeps its old fingerprint so the next scan retries
    it, and no artifact is stamped fresh unless every session that claims it was
    actually written.  See ``docs/SCAN_CONSISTENCY.md`` §11.
    """
    import time as _time
    t0 = _time.time()
    load_all()
    adapters = enabled_adapters(providers)

    grand_new = grand_skip = grand_evt = grand_changed = 0
    write_failures = 0
    first_write_failure = None

    def _write_failure_is_systemic() -> bool:
        """True only when failures are both numerous AND persistent."""
        return (write_failures >= MAX_WRITE_FAILURES
                and first_write_failure is not None
                and _time.time() - first_write_failure
                >= WRITE_FAILURE_GRACE_SECONDS)

    def _count_write_failure(name, exc):
        nonlocal write_failures, first_write_failure
        write_failures += 1
        if first_write_failure is None:
            first_write_failure = _time.time()
        _note_write_failure(name, exc, write_failures)
        if _write_failure_is_systemic():
            print(f"  ! {write_failures} session writes failed over"
                  f" {WRITE_FAILURE_GRACE_SECONDS:.0f}s; stopping so a systemic"
                  " failure is not hidden (nothing is half-written).",
                  file=sys.stderr)
            raise exc
    for ad in adapters:
        new = skip = evt = 0
        changed = 0
        multi = hasattr(ad, "scan") and callable(getattr(ad, "scan"))
        rescan = force

        sources = ad.discover()

        # A source that is present but unreadable (corrupt SQLite, locked
        # database, permission denied) is NOT a vanished source.  Marking its
        # sessions SOURCE_MISSING would retire a history that is still on disk,
        # so this round keeps the index and reports what it could not read.
        # Adapters opt in by exposing unusable_sources(); the rest are
        # unaffected.
        unusable_paths: set = set()
        probe_unusable = getattr(ad, "unusable_sources", None)
        if callable(probe_unusable):
            try:
                unusable_paths = {str(p) for p in probe_unusable()}
            except Exception as e:      # a probe must never break the scan
                print(f"  ! {ad.provider}: could not probe sources ({e})",
                      file=sys.stderr)

        if not sources:
            if unusable_paths:
                if not quiet:
                    print(f"  {ad.provider}: {len(unusable_paths)} source(s) "
                          f"present but unreadable; keeping the existing index")
                continue
            # nothing on disk anymore: the provider's history is RETAINED, not
            # dropped (O2 — a vanished source is not a request to delete)
            gone = store.prune_missing_sessions(ad.provider, set())
            if gone and not quiet:
                print(f"  {ad.provider}: retained {gone} session(s) whose "
                      f"sources vanished")
            elif not quiet:
                print(f"  {ad.provider}: no sources found")
            continue

        for src in sources:
            try:
                if store.source_changed(ad.provider, src):
                    changed += 1
                    rescan = True
            except OSError:
                continue

        # Sessions whose sources are still on disk survive pruning even when
        # they were skipped (unchanged) or failed to parse this round; only
        # sources that vanished from disk release their sessions.  A file that
        # is present but unreadable still counts as on disk.
        disk_paths = {str(p) for p in sources} | unusable_paths

        if not rescan:
            skip = len(sources)
        elif multi:
            # Snapshot the artifact state BEFORE reading it, so anything a
            # provider writes while we are reading shows up as a change the
            # next scan has to handle (docs/SCAN_CONSISTENCY.md §12).
            pre_fp = _pre_read_fingerprints(sources)
            # one artifact (SQLite DB) -> many sessions; scan() returns ALL
            # bundles exactly once — never call it inside a per-source loop.
            # If the artifact is temporarily unreadable (locked DB) treat the
            # round as skipped instead of pruning everything it owns.
            try:
                # `--force` means "re-read everything", and an adapter's own
                # per-file fingerprint check is exactly what force is meant to
                # bypass.  Without this, force only made the pipeline *call*
                # scan(); the adapter still skipped every unchanged file, so a
                # session whose source was recorded but never indexed could never
                # come back.
                bundles = ad.scan((lambda p, f: True) if force
                                  else store.source_changed)
            except Exception as e:
                print(f"  ! {ad.provider}: scan failed ({e}); keeping existing index",
                      file=sys.stderr)
                bundles = None
                skip = len(sources)
            if bundles is not None:
                anchor = sources[0]
                # One artifact -> many sessions.  Freshness is recorded only
                # after the whole artifact has been written (see the
                # stamp_sources() call below): stamping each session as it
                # lands would make a half-finished artifact look unchanged, and
                # the sessions that were never written would never be indexed
                # again -- source_changed() is satisfied by any single row for
                # the path.  Deferring costs one extra commit and makes an
                # interrupted scan recoverable by a plain re-scan
                # (docs/SCAN_CONSISTENCY.md, defect D5).
                pending_stamps = []
                failed_paths: set = set()
                for bundle in bundles:
                    src_path = Path(bundle.get("source_path") or anchor)
                    try:
                        store.replace_session(
                            bundle["session"], bundle["events"], ad.provider,
                            src_path, bundle.get("extra_sources"),
                            stamp_sources=False,
                        )
                    except Exception as e:
                        failed_paths.add(str(src_path))
                        _count_write_failure(src_path.name, e)
                        continue
                    pending_stamps.append(
                        (ad.provider, src_path, bundle["session"]["id"],
                         bundle.get("extra_sources")))
                    new += 1
                    evt += len(bundle["events"])
                # A path may only be stamped fresh if every session that claims
                # it was actually written.  For a shared artifact (Cursor, ZCode,
                # the Codex store) all sessions claim the same path, so one
                # failure withholds the stamp for the whole artifact and the
                # next scan re-reads it -- stamping the survivors would make the
                # artifact look complete and strand the session that failed
                # (defect D5, one layer up).
                to_stamp = []
                for prov, path, sid, extras in pending_stamps:
                    if str(path) in failed_paths:
                        continue
                    to_stamp.append((
                        prov, path, sid,
                        [p for p in (extras or [])
                         if str(p) not in failed_paths]))
                if to_stamp:
                    store.stamp_sources(to_stamp, fingerprints=pre_fp)
        else:
            pre_fp = _pre_read_fingerprints(sources)
            for src in sources:
                try:
                    if not force and not store.source_changed(ad.provider, src):
                        skip += 1
                        continue
                except OSError:
                    continue
                try:
                    result = ad.parse(src)
                except Exception as e:
                    print(f"  ! {src.name}: {e}", file=sys.stderr)
                    continue
                if not result:
                    continue
                if "__error__" in result:
                    print(f"  ! {src.name}: {result['__error__']}", file=sys.stderr)
                    continue
                try:
                    store.replace_session(
                        result["session"], result["events"], ad.provider,
                        src, result.get("extra_sources"),
                        fingerprints=pre_fp,
                    )
                except Exception as e:
                    # One source, one transaction: isolating the failure keeps
                    # the other sources of this provider, and the source row was
                    # written inside the rolled-back transaction, so the file
                    # still reads as changed and the next scan retries it.
                    _count_write_failure(src.name, e)
                    continue
                new += 1
                evt += len(result["events"])

        try:
            gone = store.prune_missing_sessions(ad.provider, disk_paths)
        except Exception as e:
            # prune only *marks* (O2), so skipping it leaves a session looking
            # ACTIVE_SOURCE for one more round and the next scan redoes it.
            # It writes on EVERY scan, so a concurrent writer made even a no-op
            # scan die here with a traceback -- losing the round over
            # bookkeeping is not a trade worth making.
            _note_derived_failure(f"{ad.provider}: prune", e)
            gone = 0
        if gone and not quiet:
            print(f"  {ad.provider}: retained {gone} session(s) whose "
                  f"sources vanished")

        stats = store.stats()
        if not quiet:
            print(f"  {ad.provider}: {stats['by_provider'].get(ad.provider, 0)} sessions indexed "
                  f"({new} new/refreshed, {skip} unchanged)", flush=True)
        grand_new += new; grand_skip += skip; grand_evt += evt
        grand_changed += changed

    # Automatic Continuity: resolve pending attaches against the freshly
    # indexed sessions (Phase A). Quiet scans still resolve — only the
    # output is suppressed.
    from .auto import resolve_pending_attaches, continuity_cycle
    try:
        pend = resolve_pending_attaches(store, quiet=quiet)
        renewed = store.thread_lease_renew_alive()
    except Exception as e:
        # Both are derived from the index and idempotent; the next scan redoes
        # them.  They write, so they can fail on a locked index the same way
        # prune can -- after all the indexing has already been committed.
        _note_derived_failure("post-scan continuity pass", e)
        pend = {"attached": [], "ambiguous": [], "stale": []}
        renewed = 0
    if not quiet:
        for a in pend["attached"]:
            print("  ↳ auto-attached {0} → {1}".format(a["session"], a["thread"]))
        for a in pend["ambiguous"]:
            print("  ↳ pending {0}: AMBIGUOUS ({1} candidates) — not attached".format(
                a["thread"], len(a["candidates"])))
        for st in pend["stale"]:
            print("  ↳ pending stale: {0}".format(st["thread"]))
        if renewed:
            print("  ↳ lease heartbeat renewed ({0})".format(renewed))

    stats = store.stats()
    res = {"new": grand_new, "skip": grand_skip, "events_added": grand_evt,
           "changed_sources": grand_changed, "elapsed": _time.time() - t0,
           "sessions": stats["sessions"], "events": stats["events"],
           "write_failures": write_failures,
           "auto_attached": pend["attached"], "pending_stale": pend["stale"]}
    if not quiet:
        print(f"\nscan complete: {stats['sessions']} sessions, {stats['events']} events "
              f"(new/refreshed: {grand_new}, unchanged: {grand_skip})")
        if write_failures:
            print(f"{write_failures} session(s) could not be written and will be"
                  " retried on the next scan; the index is consistent and"
                  " nothing was half-written")
        print(f"index: {store.db_path}")
    return res
