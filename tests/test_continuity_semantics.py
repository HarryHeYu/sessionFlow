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


# ---------------------------------------------------------------------------
# Issue #14 -- a session's earlier proposal and open work must survive its own
# final message.  Codex proposes a sliding window early, raises its TODO in the
# middle, and its LAST message is a progress note only.
# ---------------------------------------------------------------------------

def _issue14(store, tmp_path, repo, src):
    cx = "codex:r1"
    store.replace_session(
        new_session(id=cx, provider="codex", native_session_id="r1",
                    title="Rotation", started_at=100.0, updated_at=140.0,
                    repo_root=repo, cwd=repo),
        [
            new_event(sid=cx, seq=1, kind="user", ts=101.0,
                      content="add refresh token rotation"),
            new_event(sid=cx, seq=2, kind="assistant", ts=110.0,
                      content="For expiry I propose a sliding window: each use "
                              "extends the token's life."),
            new_event(sid=cx, seq=3, kind="assistant", ts=125.0,
                      content="Still open: replay detection is NOT implemented "
                              "yet, and the rotation test suite is still missing."),
            new_event(sid=cx, seq=4, kind="assistant", ts=140.0,
                      content="Rotation endpoint is in place. Pushing on."),
        ], "codex", src)

    cl = "claude:r2"
    store.replace_session(
        new_session(id=cl, provider="claude", native_session_id="r2",
                    title="Review", started_at=200.0, updated_at=230.0,
                    repo_root=repo, cwd=repo),
        [
            new_event(sid=cl, seq=1, kind="user", ts=201.0,
                      content="review the design"),
            new_event(sid=cl, seq=2, kind="assistant", ts=230.0,
                      content="I recommend an absolute TTL instead. I have not "
                              "ruled the sliding window out."),
        ], "claude", src)

    tid = store.thread_create(repo_root=repo, title="refresh tokens")
    for sid in (cx, cl):
        store.thread_attach(tid, sid)
    return [r for r in store.thread_members(tid)]


@pytest.fixture
def issue14(tmp_path):
    store = Store(tmp_path / "i14.db")
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    rows = _issue14(store, tmp_path, str(tmp_path), src)
    return store, rows


def test_issue14_early_proposal_and_todo_survive_the_final_message(issue14):
    """The whole point of #14: the last message must not be the only one kept."""
    store, rows = issue14
    bundle = _bundle(store, rows)

    assert "I propose a sliding window" in bundle, \
        "codex's early proposal was lost behind its progress-only last message"
    assert "replay detection" in bundle.lower(), "the TODO was lost"
    assert "rotation test suite" in bundle.lower(), "the second TODO was lost"
    assert "absolute TTL" in bundle, "claude's counter-proposal was lost"

    # attribution: the proposal line must name codex
    line = next(l for l in bundle.splitlines() if "I propose a sliding window" in l)
    assert "codex" in line, f"the proposal lost its source: {line!r}"

    # and it must not be presented as settled or as fact
    assert "[proposal]" in line, "the proposal is not labelled as a proposal"


def test_issue14_open_item_is_labelled_open_not_done(issue14):
    store, rows = issue14
    bundle = _bundle(store, rows)
    line = next(l for l in bundle.splitlines() if "replay detection" in l.lower())
    assert "[open]" in line, f"the open item is not labelled open: {line!r}"
    assert "not implemented" in line.lower()


def test_issue14_conflict_is_not_resolved_by_recency(issue14):
    store, rows = issue14
    bundle = _bundle(store, rows)
    # claude is newer and is the active session; codex's reading must still be
    # present and must not be described as rejected or superseded
    assert "I propose a sliding window" in bundle
    low = bundle.lower()
    for wrong in ("codex's proposal was rejected", "superseded by claude",
                  "no longer relevant"):
        assert wrong not in low, f"the bundle resolved the conflict by age: {wrong!r}"


def test_issue14_deterministic_output(issue14):
    """Same DB + same query -> byte-identical bundle, twice."""
    store, rows = issue14
    a = _bundle(store, rows)
    b = _bundle(store, rows)
    assert a == b, "the bundle is not deterministic"


def test_issue14_coverage_holds_across_budgets(issue14):
    """Coverage must be measured against the source, not against length."""
    from voyager.budget import apply_budget, estimate_tokens, parse_budget

    store, rows = issue14
    bundle = _bundle(store, rows)
    need = ("I propose a sliding window", "absolute TTL",
            "replay detection", "rotation test suite")

    for tier in ("balanced", "full"):
        limit = parse_budget(tier)
        packed, _ = apply_budget(bundle, limit)
        missing = [n for n in need if n.lower() not in packed.lower()]
        assert not missing, f"{tier} lost {missing}"
        assert estimate_tokens(packed) <= limit, f"{tier} exceeded its budget"

    # compact may compress, but must not fabricate or mis-attribute
    packed, _ = apply_budget(bundle, parse_budget("compact"))
    assert estimate_tokens(packed) <= parse_budget("compact")
    assert "verified fact" not in packed.lower().replace(
        "rather than as verified fact", "")


def test_issue14_does_not_dump_the_transcript(issue14):
    """Selection, not a transcript dump.

    The source has 4 codex + 2 claude assistant/user messages; the bundle must
    carry a bounded selection, not everything.
    """
    store, rows = issue14
    bundle = _bundle(store, rows)
    codex_lines = [l for l in bundle.splitlines()
                   if l.startswith("- `[codex:r1]`")]
    # 4 assistant messages exist; at most the last + _EXTRA_ASST_PER_SESSION
    assert len(codex_lines) <= 4, \
        f"the bundle is listing everything, not selecting: {len(codex_lines)} lines"
    # and no rendered line carries a whole raw message
    for line in codex_lines:
        assert len(line) <= 300, f"a line is not truncated: {len(line)} chars"
