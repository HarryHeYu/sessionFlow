"""Minimal regression tests for the index layer (no provider data needed).

Run:  python tests/test_store.py

NOTE: this file is a **manual script**, not a pytest module — it has no
`test_*` functions, so `pytest tests/` collects nothing from it (verify with
`pytest tests/test_store.py --collect-only`).  It had drifted for that reason:
it passed session *ids* to `prune_missing_sessions`, which compares *paths*, so
it would have failed loudly had anything ever run it.  The collected equivalents
are `tests/test_retention.py` (O2 retention semantics) and `tests/test_store.py`
peers such as `tests/test_threads.py`.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voyager.store import Store  # noqa: E402

TMP = Path(tempfile.gettempdir())
DB = TMP / "voyager_unit_test.db"


def bundle(sid: str, n_events: int = 2):
    session = {
        "id": f"prov:{sid}", "provider": "prov", "native_session_id": sid,
        "title": f"session {sid}", "started_at": 1000.0, "updated_at": 2000.0,
        "cwd": "E:/x", "message_count": n_events, "tool_count": 0,
        "can_resume": False, "can_fork": False, "resume_cmd": None,
        "metadata": {}, "raw_metadata": {},
    }
    events = [
        {"sid": session["id"], "ts": 1000.0 + i, "seq": i, "kind": "user",
         "content": f"event {i}"}
        for i in range(n_events)
    ]
    return session, events


def make_source(name: str, content: bytes = b"x") -> Path:
    p = TMP / f"voyager_unit_src_{name}"
    p.write_bytes(content)
    return p


def main() -> int:
    if DB.exists():
        os.remove(DB)
    failures = []

    def check(name: str, cond: bool):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            failures.append(name)

    # like cmd_scan does: the set of source PATHS still on disk.  The helper
    # this used to be returned session ids, which is not what
    # prune_missing_sessions compares against -- see the module docstring.
    with Store(DB) as store:
        # 1. multi-session artifact: N sessions from ONE source path
        src = make_source("multi")
        for sid in ("s1", "s2", "s3"):
            s, evs = bundle(sid)
            store.replace_session(s, evs, "prov", src)
        check("multi: 3 sessions from 1 source",
              store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 3)
        check("multi: 3 source rows (one per sid)",
              store.q("SELECT COUNT(*) n FROM sources")[0]["n"] == 3)

        # 2. source unchanged -> source_changed False; prune with the path still
        #    on disk keeps every session LIVE
        check("multi: unchanged detected",
              not store.source_changed("prov", src))
        store.prune_missing_sessions("prov", {str(src)})
        check("multi: unchanged source keeps every session",
              store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 3)
        check("multi: still LIVE",
              store.q("SELECT COUNT(*) n FROM sessions WHERE "
                      "COALESCE(source_state,'LIVE')='LIVE'")[0]["n"] == 3)

        # 3. the source vanishes -> O2 RETAINS the history; it does not delete.
        #    (This block used to assert `== 2`, i.e. that a session was dropped.)
        store.prune_missing_sessions("prov", set())
        check("multi: vanished source retains every session",
              store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 3)
        check("multi: marked SOURCE_MISSING",
              store.q("SELECT COUNT(*) n FROM sessions WHERE "
                      "source_state='SOURCE_MISSING'")[0]["n"] == 3)

        # 4. touch the source -> changed True again
        src.write_bytes(b"y")
        check("multi: content change detected",
              store.source_changed("prov", src))

        # 5. single-session artifact per file: removing one file retains only it
        f1, f2 = make_source("f1"), make_source("f2")
        for sid, f in (("a", f1), ("b", f2)):
            s, evs = bundle(sid)
            store.replace_session(s, evs, "prov2", f)
        os.remove(f1)
        store.prune_missing_sessions("prov2", {str(f2)})
        rows = {r["native_id"]: r["source_state"] for r in store.q(
            "SELECT native_id, source_state FROM sessions WHERE provider='prov2'")}
        check("single: both sessions kept", set(rows) == {"a", "b"})
        check("single: only the vanished one is SOURCE_MISSING",
              rows.get("a") == "SOURCE_MISSING"
              and rows.get("b") in (None, "LIVE"))

        # 6. FTS: rowid alignment + search
        s, evs = bundle("fts", 3)
        evs[0]["content"] = "tensorboard unique_marker"
        store.replace_session(s, evs, "prov3", make_source("fts"))
        hit = store.q("SELECT sid FROM event_fts WHERE event_fts MATCH ?", ("unique_marker",))
        check("fts: match", bool(hit) and hit[0]["sid"] == "prov:fts")

        # 7. replace is atomic: no orphan fts rows after re-replace
        s, evs = bundle("fts", 1)
        store.replace_session(s, evs, "prov3", make_source("fts"))
        orphan = store.q(
            "SELECT COUNT(*) n FROM event_fts WHERE sid='prov:fts' AND rowid NOT IN "
            "(SELECT id FROM events WHERE sid='prov:fts')")[0]["n"]
        check("fts: no orphan rows after replace", orphan == 0)

    if os.path.exists(DB):
        os.remove(DB)
    for p in TMP.glob("voyager_unit_src_*"):
        try:
            os.remove(p)
        except OSError:
            pass

    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURES: {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
