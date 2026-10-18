"""Read paths must not create the index they are reading.

`sqlite3.connect` creates the file when it is absent, so a read against a path
with no index used to leave a stray empty database behind and answer "0 results"
-- which reads as "you have no data" rather than "there is no index yet".

Measured before the fix, with an isolated HOME and an explicit --db:

    voyager search hello --db <missing>   -> created the file
    voyager list --db <missing>           -> created the file
    api {overview,search} --db <missing>  -> created the file
    voyager doctor                        -> did NOT create it (already correct)

Everything here is synthetic and lives in tmp_path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from voyager import api
from voyager.cli import main
from voyager.model import new_event, new_session
from voyager.store import Store, open_read


def _seed(db) -> None:
    store = Store(db)
    src = db.parent / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    sid = "codex:ro"
    store.replace_session(
        new_session(id=sid, provider="codex", native_session_id="ro",
                    title="read only", started_at=1000, updated_at=1000,
                    cwd=str(db.parent), repo_root=str(db.parent),
                    message_count=2, can_resume=True),
        [new_event(sid=sid, ts=1000, seq=0, kind="user", content="hello read only"),
         new_event(sid=sid, ts=1001, seq=1, kind="assistant", content="ack")],
        "codex", src)
    store.close()


def _logical(db):
    """What a user would call "my data": rows and schema, not bytes."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            "sessions": con.execute("select count(*) from sessions").fetchone()[0],
            "events": con.execute("select count(*) from events").fetchone()[0],
            "schema": hashlib.sha256("\n".join(
                r[0] or "" for r in con.execute(
                    "select sql from sqlite_master order by name")).encode()).hexdigest(),
        }
    finally:
        con.close()


# --- a missing index is not created ---------------------------------------

@pytest.mark.parametrize("argv", [
    ["list"],
    ["search", "hello"],
    ["show", "codex:ro"],
    ["stats"],
])
def test_a_cli_read_does_not_create_a_missing_index(tmp_path, capsys, argv):
    db = tmp_path / "absent.db"
    assert not db.exists()

    # `show` exits 2 when the session is not found, which is the right answer for
    # an empty index; the point here is only that nothing was written.
    try:
        main(argv + ["--db", str(db)])
    except SystemExit:
        pass
    capsys.readouterr()

    assert not db.exists(), (
        "reading must not leave a database behind at %s" % db
    )


@pytest.mark.parametrize("op,params", [
    ("overview", {"hours": 48}),
    ("search", {"query": "hello"}),
    ("sessions", {}),
])
def test_an_api_read_does_not_create_a_missing_index(tmp_path, op, params):
    db = tmp_path / "absent.db"
    assert not db.exists()

    res = api.handle_request({"id": 1, "op": op, "params": params}, db=db)

    assert res.get("error") is None, res
    assert not db.exists(), (
        "the plugin's read ops must not create an index either"
    )


def test_open_read_returns_an_empty_store_for_a_missing_path(tmp_path):
    """The fallback is an empty index, so callers keep their empty-result path."""
    db = tmp_path / "absent.db"
    store = open_read(db)
    try:
        assert store.sessions() == []
        assert store.search("anything", limit=5) == []
    finally:
        store.close()
    assert not db.exists()


def test_scan_still_creates_the_index(tmp_path):
    """The guard must not stop the one command whose job is to build it."""
    db = tmp_path / "fresh.db"
    assert not db.exists()
    main(["scan", "--db", str(db), "--platform", "codex"])
    assert db.exists(), "scan is the writer; it must still create the index"


# --- a healthy index is read without changing it ---------------------------

def test_reads_do_not_change_logical_content(tmp_path):
    db = tmp_path / "index.db"
    _seed(db)
    before = _logical(db)

    api.overview(db=db, hours=48)
    api.search(db=db, query="hello")
    api.current_work(db=db, repo=str(tmp_path))
    store = open_read(db)
    store.sessions()
    store.close()

    assert _logical(db) == before, "a read changed the data it was reading"
