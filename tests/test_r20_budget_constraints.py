"""R20/I-constraint regression: explicit user constraints must survive a
tight budget even when a primary goal is supplied.
"""
from __future__ import annotations

from voyager.budget import apply_budget
from voyager.continuity import build_continuation_bundle
from voyager.model import new_event, new_session
from voyager.store import Store

CONSTRAINT = "do NOT use dependency X"


def test_goal_bundle_keeps_explicit_constraint_under_tiny_budget():
    store = Store(":memory:")
    session = new_session(provider="codex", native_session_id="budget-1",
                          started_at=1.0, updated_at=2.0,
                          cwd="/synthetic/repo", repo_root="/synthetic/repo")
    session["id"] = "codex:budget-1"
    events = [
        new_event(sid=session["id"], kind="user", role="user", ts=1.0,
                  content="Finish textutil.normalize. " + CONSTRAINT),
    ]
    # Enough low-value material that the ranked-evidence section is trimmed
    # under a 300-token budget, reproducing the R20/I failure.
    for i in range(60):
        events.append(new_event(
            sid=session["id"], kind="assistant", role="assistant",
            ts=2.0 + i,
            content=f"noise event {i}: unrelated refactoring note with extra "
                    "words to grow the bundle"))
    store.replace_session(session, events, "codex",
                          "/synthetic/repo/rollout.jsonl",
                          stamp_sources=False)
    rows = store.q("SELECT * FROM sessions")
    bundle = build_continuation_bundle(store, rows, goal="finish the task",
                                       thread=None)
    packed, info = apply_budget(bundle, 300)

    assert CONSTRAINT in packed, (
        "explicit user constraint was dropped before lower-priority material; "
        f"dropped={info['dropped']}, trimmed={info['trimmed']}")
