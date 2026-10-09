"""P5 — semantics the bundle must not get wrong.

Three properties, each one a bug that was observed in a real bundle:

1. An outstanding item raised by an *older* session must survive a newer
   session saying something else.  It used to be filed under "may be
   superseded" purely because it was older.
2. Two sessions that propose different things must both appear with their
   source.  The bundle must not silently present one as settled.
3. An assistant conclusion is not a verified fact.  Nothing in the bundle may
   label the newest assistant message as verified state.

These are wording/selection properties, so they are asserted against the real
rendered bundle rather than against internals.
"""

from __future__ import annotations

import pytest

from voyager.continuity import build_continuation_bundle
from voyager.model import new_event, new_session
from voyager.store import Store

PHRASE = "read as what it said rather than as verified fact"


def _mk(store, sid, provider, native, ts, turns, src, repo):
    store.replace_session(
        new_session(id=sid, provider=provider, native_session_id=native,
                    title=f"{provider} work", started_at=ts, updated_at=ts + 10,
                    repo_root=repo, cwd=repo, can_resume=True,
                    resume_cmd=f"{provider} resume {native}"),
        [new_event(sid=sid, seq=i, kind=k, ts=ts + i, content=c)
         for i, (k, c) in enumerate(turns, start=1)],
        provider, src)
    return sid


@pytest.fixture
def two_sessions(tmp_path):
    """codex leaves work outstanding; claude later answers a different question.

    Deliberately the shape that broke: the outstanding item is in the OLDER
    session, so the old overlay rule demoted it.
    """
    store = Store(tmp_path / "semantics.db")
    repo = str(tmp_path)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")

    tid = store.thread_create(repo_root=repo, title="refresh tokens")

    codex = _mk(store, "codex:r1", "codex", "r1", 100.0, [
        ("user", "add refresh token rotation"),
        ("assistant", "Rotation is in place. Replay detection is NOT implemented "
                      "yet, and the rotation test suite is still missing."),
    ], src, repo)

    claude = _mk(store, "claude:r2", "claude", "r2", 200.0, [
        ("user", "review the refresh token rotation design"),
        ("assistant", "Reviewed. I recommend an absolute TTL over the sliding "
                      "window the Codex session proposed: a sliding window lets "
                      "a stolen token live forever if it keeps being used."),
    ], src, repo)

    for sid in (codex, claude):
        store.thread_attach(tid, sid)

    rows = [r for r in store.thread_members(tid)]
    return store, rows, tid


def _bundle(store, rows):
    return build_continuation_bundle(store, rows, live_git=False)


def test_outstanding_item_from_the_older_session_survives(two_sessions):
    """The older session's open work is still open, and still attributed."""
    store, rows, _ = two_sessions
    bundle = _bundle(store, rows)

    assert "Replay detection is NOT implemented" in bundle, \
        "the older session's outstanding item was dropped"
    assert "rotation test suite is still missing" in bundle

    # attributed to codex, not silently re-attributed to the newer session
    line = next(l for l in bundle.splitlines() if "Replay detection" in l)
    assert "codex" in line, f"lost the source of the outstanding item: {line!r}"

    # and it is NOT presented as done
    for claim in ("replay detection is done", "Replay detection implemented",
                  "test suite passed", "rotation test suite: complete"):
        assert claim.lower() not in bundle.lower(), \
            f"outstanding work was upgraded to a completed claim: {claim!r}"


def test_outstanding_item_is_not_filed_as_superseded(two_sessions):
    """Being older is not evidence of being superseded."""
    store, rows, _ = two_sessions
    bundle = _bundle(store, rows)

    assert "may be superseded" not in bundle, \
        "an older session's conclusions are still labelled as possibly stale"
    assert "takes precedence" not in bundle, \
        "the bundle still lets the newest session override the others by age"


def test_conflicting_proposals_both_survive_with_sources(two_sessions):
    """Both readings stay visible; neither is presented as settled."""
    store, rows, _ = two_sessions
    bundle = _bundle(store, rows)

    assert "absolute TTL" in bundle, "claude's proposal was dropped"
    # claude is the active session, so its proposal carries its source in the
    # section heading rather than inline; codex's is listed with its own id.
    assert "active session: `claude`" in bundle, \
        "claude's proposal lost its source"
    assert "`[codex:r1]`" in bundle, "codex's session lost its provenance"

    lowered = bundle.lower()
    for settled in ("we decided", "the decision is", "has been adopted",
                    "agreed on absolute ttl", "final decision"):
        assert settled not in lowered, \
            f"a contested proposal was presented as settled: {settled!r}"


def test_latest_assistant_message_is_not_called_verified(two_sessions):
    """Assistant statement != verified fact."""
    store, rows, _ = two_sessions
    bundle = _bundle(store, rows)

    assert "## Current verified state" not in bundle, \
        "the newest assistant message is still presented as verified state"
    assert "## Latest assistant conclusion" in bundle
    assert PHRASE in bundle, \
        "the section does not say the conclusion is unverified"


def test_budget_tiers_keep_the_outstanding_item(two_sessions):
    """Budget may compress detail, but must not invent or drop the open item."""
    from voyager.budget import apply_budget, estimate_tokens, parse_budget

    store, rows, _ = two_sessions
    bundle = _bundle(store, rows)

    for tier in ("compact", "balanced", "full"):
        limit = parse_budget(tier)
        packed, info = apply_budget(bundle, limit)
        assert estimate_tokens(packed) <= (limit or 10 ** 9), \
            f"{tier} exceeded its budget"
        if tier in ("balanced", "full"):
            assert "Replay detection" in packed, \
                f"{tier} dropped the outstanding item"
            assert "absolute TTL" in packed, \
                f"{tier} dropped the conflicting proposal"
        # compression must never claim more than the source did
        assert "verified" not in packed.lower().replace(
            "rather than as verified fact", ""), \
            f"{tier} introduced a verification claim the source never made"
