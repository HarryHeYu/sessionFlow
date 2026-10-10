"""Issue #15 — one canonical session order for every renderer.

Three things were being conflated, and only one of them may decide what the
bundle leads with:

* **thread order** — ``thread_sessions.ord``, the persisted handoff sequence.
  This is the canonical order.  It survives a re-scan: an old session whose
  provider file is touched again must not jump to the front of the work.
* **latest activity** — ``updated_at``.  Reported as provenance, never used to
  select.  A session being written to more recently is not evidence that its
  conclusion is more correct.
* **relevant conclusion** — a goal-conditioned judgement made on evidence
  (``Goal-ranked evidence``), which is a separate mechanism and stays separate.

The defect: the flat bundle ordered sessions by ``updated_at`` while the tiered
renderer ordered them by ``ord``, and both then headlined "the latest".  For a
thread whose activity order and handoff order disagree, the two renderers told
different stories about the same work.

The fixture below is exactly that shape — activity order crossed with handoff
order — so every assertion here fails against the old behaviour.
"""

from __future__ import annotations

import re

import pytest

from voyager.continuity import (
    ORDER_ACTIVITY,
    ORDER_THREAD,
    build_continuation_bundle,
    build_tiered_bundle,
    canonical_session_order,
)
from voyager.model import new_event, new_session
from voyager.store import Store

REPO = "E:/proj/authsvc"
GOAL = "finish the auth work"

#: provider, thread position (ord, 1-based), updated_at, conclusion
CROSSED = [
    ("codex", 1, 3000.0, "Approach X is done and verified by the test suite."),
    ("claude", 2, 1000.0, "Approach Y is risky; do not adopt it without more evidence."),
    ("dsh", 3, 2000.0, "Still need to evaluate X and Y before choosing."),
]


def _build(tmp_path, specs, *, title="auth work", goal=GOAL, repo=REPO):
    """A WorkThread whose attach order is the order of `specs`."""
    db = tmp_path / "order.db"
    store = Store(db)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")

    sids = []
    for provider, _ord, updated, conclusion in specs:
        sid = f"{provider}:{_ord}"
        sids.append(sid)
        store.replace_session(
            new_session(id=sid, provider=provider,
                        native_session_id=sid.split(":")[-1],
                        title=f"{provider} work", started_at=updated - 100,
                        updated_at=updated, repo_root=repo, cwd=repo,
                        can_resume=True),
            [new_event(sid=sid, seq=1, kind="user", ts=updated - 100,
                       content="continue the auth work"),
             new_event(sid=sid, seq=2, kind="assistant", ts=updated,
                       content=conclusion)],
            provider, src)

    tid = store.thread_create(repo_root=repo, title=title, goal=goal)
    for sid in sids:
        store.thread_attach(tid, sid)
    return store, tid


@pytest.fixture
def crossed(tmp_path):
    """ord 1/2/3 but activity T3/T1/T2 — the two orders disagree."""
    return _build(tmp_path, CROSSED)


def _sessions_only(tmp_path, specs, name="merge.db"):
    """Sessions with no WorkThread yet, so a merge has to create one."""
    db = tmp_path / name
    store = Store(db)
    src = tmp_path / f"{name}.jsonl"
    src.write_text("{}", encoding="utf-8")
    for provider, pos, updated, conclusion in specs:
        sid = f"{provider}:{pos}"
        store.replace_session(
            new_session(id=sid, provider=provider,
                        native_session_id=sid.split(":")[-1], title=sid,
                        started_at=updated - 100, updated_at=updated,
                        repo_root=REPO, cwd=REPO),
            [new_event(sid=sid, seq=1, kind="assistant", ts=updated,
                       content=conclusion)],
            provider, src)
    return store


# --- readers -----------------------------------------------------------------

_HEADLINE_RE = re.compile(r"active session: `(?P<provider>[^`]+)` `(?P<native>[^`]+)`")
_L0_RE = re.compile(r"^latest_session_id: (?P<sid>\S+)$", re.MULTILINE)


def flat_headline(bundle: str) -> str:
    m = _HEADLINE_RE.search(bundle)
    assert m, f"no headline in the flat bundle:\n{bundle[:600]}"
    return f"{m.group('provider')}:{m.group('native')}"


def tiered_headline(bundle: str) -> str:
    m = _L0_RE.search(bundle)
    assert m, f"no latest_session_id in the tiered bundle:\n{bundle[:600]}"
    return m.group("sid")


def flat(store, tid, **kw):
    return build_continuation_bundle(store, store.thread_members(tid),
                                     goal=GOAL, live_git=False,
                                     thread=store.thread_get(tid), **kw)


def tiered(store, tid, **kw):
    thread = store.thread_get(tid)
    members = store.thread_members(tid)
    return build_tiered_bundle(store, thread, members, members, **kw)


# --- 1. the reported defect --------------------------------------------------

def test_flat_and_tiered_agree_on_where_the_work_stopped(crossed):
    store, tid = crossed
    a = flat_headline(flat(store, tid))
    b = tiered_headline(tiered(store, tid))
    assert a == b, f"flat says {a}, tiered says {b}"


def test_the_headline_is_the_handoff_endpoint_not_the_newest_file(crossed):
    """A is newest by activity; C is where the thread handed the work to."""
    store, tid = crossed
    assert flat_headline(flat(store, tid)) == "dsh:3"
    assert tiered_headline(tiered(store, tid)) == "dsh:3"


def test_the_bundle_says_why_that_session_leads(crossed):
    store, tid = crossed
    bundle = flat(store, tid)
    assert "last in this WorkThread's handoff order" in bundle, \
        "the headline does not name the rule that chose it"


def test_a_disagreement_between_the_clock_and_the_handoff_is_stated(crossed):
    """A is later in time, earlier in the thread — the reader is told."""
    store, tid = crossed
    bundle = flat(store, tid)
    assert "most recently *active* session" in bundle
    note = next(l for l in bundle.splitlines() if "most recently *active*" in l)
    assert "codex:1" in note, f"the note does not name the session: {note!r}"
    assert "not evidence" in note, "the note does not say activity is not a verdict"


def test_no_note_when_the_clock_and_the_handoff_agree(tmp_path):
    """The note is for a real disagreement, not decoration."""
    specs = [("codex", 1, 1000.0, "first"), ("claude", 2, 2000.0, "second")]
    store, tid = _build(tmp_path, specs)
    bundle = flat(store, tid)
    assert "most recently *active* session" not in bundle
    assert flat_headline(bundle) == "claude:2"


# --- 2. order stability ------------------------------------------------------

def test_a_rescan_does_not_reorder_the_thread(tmp_path):
    """Touching an old session's file must not move it in the work."""
    store, tid = _build(tmp_path, CROSSED)
    before = flat_headline(flat(store, tid))

    # simulate the provider rewriting the OLDEST thread session, as a re-scan
    # would see it: the file's mtime moves, the thread's order must not
    store.q("UPDATE sessions SET updated_at=99999 WHERE id='codex:1'")

    assert flat_headline(flat(store, tid)) == before, \
        "a re-scanned old session took over the headline"
    assert tiered_headline(tiered(store, tid)) == before


def test_thread_order_wins_even_when_it_is_the_oldest_by_activity(crossed):
    store, tid = crossed
    ordered, basis = canonical_session_order(store, store.thread_members(tid),
                                            store.thread_get(tid))
    assert basis == ORDER_THREAD
    assert [r["id"] for r in ordered] == ["codex:1", "claude:2", "dsh:3"]


def test_identical_timestamps_still_order_deterministically(tmp_path):
    """No thread: the fallback must be a *total* order, not a lucky one."""
    specs = [("codex", 1, 1000.0, "x"), ("claude", 2, 1000.0, "y"),
             ("grok", 3, 1000.0, "z")]
    store, _tid = _build(tmp_path, specs)
    rows = store.sessions(None)
    first, basis = canonical_session_order(store, rows)
    assert basis == ORDER_ACTIVITY
    again, _ = canonical_session_order(store, list(reversed(rows)))
    assert [r["id"] for r in first] == [r["id"] for r in again]
    assert [r["id"] for r in first] == sorted(r["id"] for r in rows)


# --- 3. the newest session cannot hijack the headline ------------------------

def test_a_chatty_newest_session_does_not_take_the_headline(tmp_path):
    """Newest by activity, earliest in the thread, nothing to say."""
    specs = [
        ("codex", 1, 9000.0, "Sure, happy to help! Let me know what you need."),
        ("claude", 2, 1000.0, "Implemented the rotation endpoint; tests pass."),
        ("dsh", 3, 2000.0, "Still open: replay detection is not implemented."),
    ]
    store, tid = _build(tmp_path, specs)
    assert flat_headline(flat(store, tid)) == "dsh:3"
    bundle = flat(store, tid)
    assert "most recently *active* session" in bundle
    assert "codex:1" in next(l for l in bundle.splitlines()
                             if "most recently *active*" in l)


# --- 4. conflicts, provenance, evidence --------------------------------------

def test_conflicting_conclusions_keep_both_sources(crossed):
    store, tid = crossed
    bundle = flat(store, tid)
    assert "Approach X is done" in bundle
    assert "Approach Y is risky" in bundle
    assert "Still need to evaluate" in bundle
    for sid in ("codex:1", "claude:2", "dsh:3"):
        assert sid in bundle, f"{sid} lost its provenance"
    # the newer session must not have erased the older reading
    assert "superseded" not in bundle.lower()


def test_evidence_survives_every_budget(crossed):
    from voyager.budget import apply_budget, estimate_tokens, parse_budget

    store, tid = crossed
    bundle = flat(store, tid)
    for tier in ("compact", "balanced", "full"):
        limit = parse_budget(tier)
        packed, _ = apply_budget(bundle, limit)
        assert estimate_tokens(packed) <= limit, f"{tier} exceeded its budget"
        assert "dsh:3" in packed, f"{tier} lost the headline session's provenance"
    for tier in ("balanced", "full"):
        packed, _ = apply_budget(bundle, parse_budget(tier))
        assert "Still need to evaluate" in packed, f"{tier} dropped the open question"
        assert "Approach Y is risky" in packed, f"{tier} dropped the conflict"


def test_an_older_session_keeps_its_verifiable_evidence(tmp_path):
    """Evidence in the oldest thread step must not be crowded out."""
    specs = [
        ("codex", 1, 1000.0,
         "The migration ran clean: `voyager db check` reported 0 problems."),
        ("claude", 2, 2000.0, "Reviewed the migration; nothing outstanding."),
        ("dsh", 3, 3000.0, "Continuing with the follow-up work."),
    ]
    store, tid = _build(tmp_path, specs)
    bundle = flat(store, tid)
    assert "voyager db check" in bundle
    assert flat_headline(bundle) == "dsh:3"


# --- 5. isolation between threads -------------------------------------------

def test_two_threads_in_one_repo_do_not_bleed(tmp_path):
    store, first = _build(tmp_path, CROSSED, title="auth work")
    store2_rows = store.thread_members(first)
    other = store.thread_create(repo_root=REPO, title="other work", goal="other")
    src = tmp_path / "s.jsonl"
    store.replace_session(
        new_session(id="grok:z", provider="grok", native_session_id="z",
                    title="grok work", started_at=500.0, updated_at=500.0,
                    repo_root=REPO, cwd=REPO),
        [new_event(sid="grok:z", seq=1, kind="assistant", ts=500.0,
                   content="Unrelated project work.")],
        "grok", src)
    store.thread_attach(other, "grok:z")

    auth = flat(store, first)
    assert "Unrelated project work." not in auth, "another thread leaked in"
    assert "grok:z" not in auth
    assert [r["id"] for r in store2_rows] == ["codex:1", "claude:2", "dsh:3"]


# --- 6. every entry point shares the semantics -------------------------------

def test_the_two_context_formats_agree(crossed, monkeypatch):
    """The DSH / MCP surface: one call site, two formats, one answer."""
    from voyager.auto import get_continuation_context

    store, tid = crossed
    monkeypatch.setenv("VOYAGER_NO_SYNC", "1")
    flat_ctx = get_continuation_context(store=store, thread_id=tid, goal=GOAL,
                                        context_format="flat")
    tiered_ctx = get_continuation_context(store=store, thread_id=tid, goal=GOAL,
                                          context_format="tiered-v1")
    assert flat_headline(flat_ctx["context"]) == "dsh:3"
    assert tiered_headline(tiered_ctx["context"]) == "dsh:3"


def test_bundle_preview_follows_the_thread_order(crossed):
    """`voyager continue --sessions …` must not invent its own order."""
    from voyager import api

    store, tid = crossed
    db = store.db_path if hasattr(store, "db_path") else None
    store.close()
    out = api.bundle_preview(db=db, session_refs=["codex:1", "claude:2", "dsh:3"],
                             goal=GOAL)
    assert "bundle" in out, out
    assert flat_headline(out["bundle"]) == "dsh:3"


def test_the_runtime_hint_follows_the_handoff_endpoint(tmp_path):
    """A thread's members can sit in different directories.

    The Runtime State section describes where the work *is*, so it has to take
    its repository from the handoff endpoint — not from whichever member's
    provider file happened to be written last.  Mutation testing found this
    line uncovered: with every fixture member sharing one repo, dropping the
    handoff order changed nothing observable.
    """
    db = tmp_path / "runtime.db"
    store = Store(db)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")

    first_repo = str(tmp_path / "repo-one")
    last_repo = str(tmp_path / "repo-two")
    specs = [("codex", "codex:1", first_repo, 5000.0, "early work"),   # newest
             ("dsh", "dsh:2", last_repo, 1000.0, "where it stands now")]
    for provider, sid, repo, ts, text in specs:
        store.replace_session(
            new_session(id=sid, provider=provider,
                        native_session_id=sid.split(":")[-1], title=sid,
                        started_at=ts - 10, updated_at=ts, repo_root=repo, cwd=repo),
            [new_event(sid=sid, seq=1, kind="assistant", ts=ts, content=text)],
            provider, src)

    # No repo_root on the thread itself, so the fallback to the latest member
    # is what decides the Runtime State.  (When the thread declares a repo, that
    # declaration wins -- deliberately, and tested elsewhere.)
    tid = store.thread_create(repo_root=None, title="t", goal="g")
    for _p, sid, *_ in specs:
        store.thread_attach(tid, sid)

    out = tiered(store, tid, max_field_chars=200)
    repo_line = next(l for l in out.splitlines() if l.startswith("repository:"))
    assert repo_line.endswith("repo-two"), (
        f"the runtime hint points at the wrong member: {repo_line!r}")


def test_merge_attaches_in_the_order_the_sessions_were_given(tmp_path):
    """`voyager merge a b c` must not depend on set iteration order.

    `_thread_from_merge` attached its members by iterating a *set*, so a newly
    created merge thread got whatever order that set happened to have — and
    since the bundle now follows `ord` (D20), that would have made the merged
    order and its headline vary from process to process.
    """
    from types import SimpleNamespace

    from voyager.cli import _thread_from_merge

    store = _sessions_only(tmp_path, CROSSED)
    given = ["dsh:3", "codex:1", "claude:2"]
    rows = [store.session(sid)[0] for sid in given]

    tid = _thread_from_merge(store, rows,
                             SimpleNamespace(thread_title=None, goal=None,
                                             json=True))
    assert [r["id"] for r in store.thread_members(tid)] == given, \
        "the merge thread did not keep the order the sessions were given in"
    assert flat_headline(flat(store, tid)) == "claude:2"
