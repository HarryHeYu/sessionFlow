"""Write failures must not leave a WorkThread leased to a non-existent handoff.

Regression for R14 BUG-1 / ENG-2.  `handoff_thread` takes the single-writer
lease before it compiles and writes the bundle; the existing early returns for
an unsupported transcript and an invalid budget both release it.  A filesystem
error while creating the output directory or writing the file must obey the
same invariant, or the next legal handoff is refused until someone knows to
run `thread unlock --steal`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voyager.continuity import handoff_thread
from voyager.store import Store


@pytest.fixture
def leased_thread(tmp_path):
    """A two-member WorkThread whose source files exist (no native resume)."""
    store = Store(tmp_path / "index.db")
    src = tmp_path / "source.jsonl"
    src.write_text("{}\n", encoding="utf-8")

    for i, (sid, provider) in enumerate((
            ("codex:m0", "codex"), ("claude:m1", "claude"))):
        session = {
            "id": sid, "provider": provider,
            "native_session_id": sid.split(":")[-1],
            "title": "work {0}".format(i),
            "started_at": 1000.0 + i, "updated_at": 2000.0 + i,
            "cwd": "E:/proj/demo", "repo_root": "E:/proj/demo",
            "message_count": 2, "tool_count": 1,
            "can_resume": False, "can_fork": False, "resume_cmd": None,
            "metadata": {}, "raw_metadata": {},
        }
        event = {
            "sid": sid, "ts": 2000.0 + i, "seq": 0, "kind": "user",
            "content": "conv work {0}".format(i),
        }
        store.replace_session(session, [event], provider, src)

    tid = store.thread_create(repo_root="E:/proj/demo", title="the task")
    for sid in ("codex:m0", "claude:m1"):
        store.thread_attach(tid, sid)
    yield store, tid
    store.close()


def _handoff(store, tid, output, target="grok"):
    return handoff_thread(
        store=store, target=target, thread=tid, mode="bundle",
        style="continuation", output=str(output), force_bundle=True)


def test_mkdir_failure_releases_lease_and_allows_retry(tmp_path, leased_thread):
    store, tid = leased_thread
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    failed_out = blocker / "bundle.md"

    first = _handoff(store, tid, failed_out)
    assert first["action"] == "error"
    assert first["exit_code"] == 2
    assert "cannot create bundle directory" in first["error"]
    assert not failed_out.exists()
    assert store.thread_lease_get(tid) is None
    assert store.thread_pending_list(tid) == []

    good_out = tmp_path / "good.md"
    second = _handoff(store, tid, good_out, target="claude")
    assert second["action"] == "bundle"
    assert good_out.exists()
    assert "Continuation Bundle" in good_out.read_text(encoding="utf-8")


def test_atomic_write_failure_releases_lease(tmp_path, leased_thread,
                                             monkeypatch):
    import voyager.continuity as continuity

    store, tid = leased_thread

    def boom(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(continuity, "_write_atomic", boom)
    out = tmp_path / "bundle.md"

    res = _handoff(store, tid, out)
    assert res["action"] == "error"
    assert res["exit_code"] == 2
    assert "cannot write bundle" in res["error"]
    assert not out.exists()
    assert store.thread_lease_get(tid) is None
    assert store.thread_pending_list(tid) == []

    monkeypatch.undo()
    retry = _handoff(store, tid, out, target="claude")
    assert retry["action"] == "bundle"
    assert out.exists()
