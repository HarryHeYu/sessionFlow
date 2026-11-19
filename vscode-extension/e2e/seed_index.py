#!/usr/bin/env python3
"""Seed a SYNTHETIC index for the Extension Host E2E harness.

Never reads a real user session, index or home. Everything is built from
literal strings below into the DB path the caller passes. The scenarios cover
the states UI acceptance needs: two WorkThreads (selection + ambiguity), a
SOURCE_MISSING member (retention caveat), a cross-thread contradiction
(provenance), and a repo with no WorkThread (empty state).

Usage:
    python seed_index.py <output-db-path>

The E2E launcher sets HOME/USERPROFILE to an isolated dir and points the
extension's `voyager.repoPath` at a throwaway workspace, so the spawned
`python -m voyager.api serve` reads THIS db, not the developer's real index.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Continuity is a read-only context facet; never trigger a real scan.
os.environ.setdefault("VOYAGER_NO_SYNC", "1")

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from voyager.model import new_event, new_session  # noqa: E402
from voyager.store import Store  # noqa: E402

REPO_A = "synthetic-workspace"
REPO_B = "other-synthetic-repo"
NOW = 1_700_000_000.0


def _session(sid, provider, native, title, repo, ts, events, source_missing=False):
    evs = []
    for seq, (kind, content, extra) in enumerate(events):
        kw = dict(sid=sid, ts=ts + seq * 10, seq=seq, kind=kind,
                  content=content, origin="human" if kind == "user" else
                  "provider_system", raw_event={})
        kw.update(extra)
        evs.append(new_event(**kw))
    row = new_session(
        id=sid, provider=provider, native_session_id=native, title=title,
        started_at=ts, updated_at=ts + len(evs) * 10,
        cwd=repo, repo_root=repo, message_count=1,
        tool_count=sum(1 for e in evs if e["kind"] == "tool_call"),
        can_resume=False, metadata={}, raw_metadata={})
    return row, evs, source_missing


def build(db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db)
    store.con.commit()

    specs = [
        _session("codex:e2e-auth", "codex", "e2e-auth",
                 "implement auth flow", REPO_A, NOW, [
                     ("user", "Implement the authentication flow", {}),
                     ("assistant", "PROPOSE: stateless JWT access token", {}),
                     ("tool_call", "", {"command": "pytest -q",
                                        "exit_code": 1}),
                     ("error", "FAIL: test_auth_expiry", {}),
                 ]),
        _session("claude:e2e-review", "claude", "e2e-review",
                 "review auth flow", REPO_A, NOW + 400, [
                     ("assistant", "REVIEW: rotation looks good", {}),
                 ]),
        _session("codex:e2e-old", "codex", "e2e-old",
                 "old config approach", REPO_A, NOW + 800, [
                     ("assistant", "OLD: config/db.py", {}),
                 ], source_missing=True),
        _session("claude:e2e-other", "claude", "e2e-other",
                 "unrelated UI work", REPO_B, NOW + 1200, [
                     ("user", "NOISE: UI color palette", {}),
                 ]),
    ]
    for row, evs, src_missing in specs:
        src = db.parent / (row["id"].replace(":", "-") + ".jsonl")
        src.write_text("{}\n", encoding="utf-8")
        store.replace_session(row, evs, row["provider"], src)
        if src_missing:
            store.q("UPDATE sessions SET source_state='SOURCE_MISSING' WHERE id=?",
                    (row["id"],))
            store.con.commit()

    # Two active WorkThreads in REPO_A -> thread_ambiguous must be reported.
    t1 = store.thread_create(repo_root=REPO_A, title="Auth flow", goal="finish auth")
    store.thread_attach(t1, "codex:e2e-auth")
    store.thread_attach(t1, "claude:e2e-review")
    store.thread_attach(t1, "codex:e2e-old")
    t2 = store.thread_create(repo_root=REPO_A, title="Config migration", goal="move config")
    store.thread_attach(t2, "codex:e2e-old")
    store.con.commit()
    store.close()
    print("seeded %s (threads: %s, %s)" % (db, t1, t2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python seed_index.py <output-db-path>")
    build(Path(sys.argv[1]))
