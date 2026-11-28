"""R20/C5 regression: pending attach resolution must be atomic.

A crash between attaching the session and marking the pending record resolved
used to leave an orphan pending row that could never resolve again.
"""
from __future__ import annotations

import time

import pytest

from voyager import auto
from voyager.model import new_event, new_session
from voyager.store import Store


def _seed(db=None):
    store = Store(":memory:")
    now = time.time()
    tid = store.thread_create(repo_root="/synthetic/repo")
    session = new_session(provider="codex", native_session_id="crash-one",
                          started_at=now - 1, updated_at=now,
                          cwd="/synthetic/repo", repo_root="/synthetic/repo")
    session["id"] = "codex:crash-one"
    event = new_event(sid=session["id"], kind="user", role="user",
                      content="hello", ts=now)
    store.replace_session(session, [event], "codex",
                          "/synthetic/repo/rollout.jsonl",
                          stamp_sources=False)
    store.pending_record(tid, "codex", native_session_id="crash-one",
                         repo_root="/synthetic/repo", cwd="/synthetic/repo",
                         created_at=now - 1)
    return store, tid


def test_pending_attach_is_atomic_across_mark_failure(monkeypatch):
    store, tid = _seed()

    def boom(self, *args, **kwargs):
        raise RuntimeError("injected pending_mark failure")

    monkeypatch.setattr(Store, "pending_mark", boom)
    # Avoid verification-harness file I/O; this test only asserts the
    # store-level atomicity of attach + pending resolution.
    monkeypatch.setattr(
        "voyager.verification_harness.note_attach_resolved_for_session",
        lambda *a, **k: False)
    try:
        auto.resolve_pending_attaches(store, now=time.time())
    except RuntimeError:
        pass  # old code propagated; new code may avoid the failing call

    members = store.thread_members(tid)
    pending = store.pending_open()
    attached = len(members) == 1
    resolved = not pending and store.q(
        "SELECT resolved_sid FROM thread_pending WHERE thread_id=?", (tid,)
    )[0]["resolved_sid"] is not None
    assert attached == resolved, (
        "session attach and pending resolution must commit together; "
        f"attached={attached}, pending_open={len(pending)}")
