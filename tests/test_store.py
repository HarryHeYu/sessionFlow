"""Minimal regression tests for the index layer (no provider data needed).

Run:  python tests/test_store.py
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

    # like cmd_scan does: live_ids seeded from source rows still on disk
    def live_from_disk(store: Store, provider: str, disk_paths: set) -> set:
        return {
            r["sid"] for r in store.q(
                "SELECT path, sid FROM sources WHERE provider=?", (provider,))
            if r["sid"] and r["path"] in disk_paths
        }

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

        # 2. source unchanged -> source_changed False; prune (with live ids
        #    derived the way cmd_scan derives them) keeps every session
        check("multi: unchanged detected",
              not store.source_changed("prov", src))
        live = live_from_disk(store, "prov", {str(src)})
        store.prune_missing_sessions("prov", live)
        check("multi: unchanged source survives prune",
              store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 3)

        # 3. rescan now returns only s1,s2 -> s3 pruned
        for sid in ("s1", "s2"):
            s, evs = bundle(sid)
            store.replace_session(s, evs, "prov", src)
        store.prune_missing_sessions("prov", {"prov:s1", "prov:s2"})
        check("multi: missing session pruned",
              store.q("SELECT COUNT(*) n FROM sessions")[0]["n"] == 2)

        # 4. touch the source -> changed True again
        src.write_bytes(b"y")
        check("multi: content change detected",
              store.source_changed("prov", src))

        # 5. single-session artifact per file: delete one file -> only it prunes
        f1, f2 = make_source("f1"), make_source("f2")
        for sid, f in (("a", f1), ("b", f2)):
            s, evs = bundle(sid)
            store.replace_session(s, evs, "prov2", f)
        os.remove(f1)
        live = live_from_disk(store, "prov2", {str(f2)})
        store.prune_missing_sessions("prov2", live)
        ids = {r["native_id"] for r in store.q(
            "SELECT native_id FROM sessions WHERE provider='prov2'")}
        check("single: deleted source pruned, other kept", ids == {"b"})

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
