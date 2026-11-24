"""Read-only CLI Handoff Preview.

A user should be able to answer "what would I hand off?" without taking a
lease, writing a bundle file, recording a pending attach or launching an agent.
The regression tests below pin the observable contract and the read-only
guarantee; they are deliberately written against the CLI, not the API helper,
because the friction being fixed is the missing user entry point.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from voyager.cli import main
from voyager.model import new_event, new_session
from voyager.store import Store


def _seed(store, sid, provider, title, content, born):
    src = store.db_path.parent / (sid.replace(":", "_") + ".jsonl")
    src.write_text("{}", encoding="utf-8")
    s = new_session(id=sid, provider=provider, native_session_id=sid.split(":", 1)[1],
                    title=title, started_at=born, updated_at=born,
                    cwd="E:/proj/demo", repo_root="E:/proj/demo", message_count=1)
    store.replace_session(
        s,
        [new_event(sid=sid, ts=born, seq=0, kind="user", content=content)],
        provider, src)


def _db_fingerprint(db: Path) -> dict:
    out = {}
    for p in sorted(db.parent.glob(db.name + "*")):
        if p.is_file():
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _fixture(tmp_path):
    db = tmp_path / "preview.db"
    store = Store(db)
    _seed(store, "codex:a1", "codex", "strict flag",
          "TODO: raise ValueError in the strict branch; command failed: pytest -q exit 1",
          1000.0)
    _seed(store, "claude:a2", "claude", "claude review",
          "TODO remains: strict branch still returns; rerun pytest -q",
          2000.0)
    tid = store.thread_create(repo_root="E:/proj/demo", title="strict work",
                              goal="finish strict=True")
    store.thread_attach(tid, "codex:a1")
    store.thread_attach(tid, "claude:a2")
    store.close()
    return db, tid


def test_preview_json_is_read_only_and_shows_the_selected_work(tmp_path, capsys):
    db, tid = _fixture(tmp_path)
    before = _db_fingerprint(db)

    rc = main(["preview", "--db", str(db), "--thread", tid,
               "--to", "claude", "--budget", "balanced", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["read_only"] is True
    assert payload["selected_thread"]["id"] == tid
    assert payload["selected_thread"]["goal"] == "finish strict=True"
    assert payload["thread_requested"] == tid
    assert "thread_ambiguous" not in payload
    assert payload["budget"] == 20000
    assert "TODO" in payload["bundle"]
    assert "pytest -q" in payload["bundle"]
    assert payload["members"][0]["source_state"] == "ACTIVE_SOURCE"
    assert _db_fingerprint(db) == before, "preview must not write the index"


def test_preview_marks_ambiguous_threads_without_pretending_to_choose(tmp_path, capsys):
    db, tid = _fixture(tmp_path)
    store = Store(db)
    _seed(store, "codex:b1", "codex", "unrelated task", "done", 3000.0)
    other = store.thread_create(repo_root="E:/proj/demo", title="other work",
                                goal="unrelated")
    store.thread_attach(other, "codex:b1")
    store.close()

    rc = main(["preview", "--db", str(db), "--repo", "E:/proj/demo", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["thread_ambiguous"] is True
    candidates = {c["id"] for c in payload["thread_candidates"]}
    assert tid in candidates
    assert len(candidates) >= 2
    assert "default" in payload["thread_default_reason"]
    assert "not a choice" in payload["thread_default_reason"]
    # A default preview is not an explicit selection. The JSON must not grow a
    # selection-shaped field merely because the newest candidate was compiled.
    assert "selected_thread" not in payload
    assert "thread" not in payload

    # Naming a thread among the candidates is an explicit choice: the ambiguity
    # markers disappear and the chooser metadata is added instead.
    rc = main(["preview", "--db", str(db), "--repo", "E:/proj/demo",
               "--thread", other, "--json"])
    chosen = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert chosen["thread_requested"] == other
    assert chosen["selected_thread"]["id"] == other
    assert "thread_ambiguous" not in chosen


def test_preview_human_output_states_that_nothing_was_written(tmp_path, capsys):
    db, tid = _fixture(tmp_path)
    before = _db_fingerprint(db)

    rc = main(["preview", "--db", str(db), "--thread", tid])

    out = capsys.readouterr().out
    assert rc == 0
    assert "READ ONLY" in out
    assert "no lease" in out.lower()
    assert "bundle" in out.lower()
    assert "strict work" in out
    assert _db_fingerprint(db) == before
