"""SQLite store: sessions / events / files / sources + FTS5.

Idempotency model
-----------------
Every parsed artifact (a rollout file, a project JSONL, a conversation DB row
group, ...) is registered in `sources` with (mtime, size). A scan skips
sources whose mtime+size are unchanged; changed/missing ones are re-parsed
and their events replaced atomically (delete + insert inside one
transaction). Therefore repeated scans never duplicate data.

FTS5 rows use rowid == events.id, so per-session rebuilds are trivial.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from .model import ORIGIN_UNKNOWN
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Raw events bigger than this are truncated (full data stays at source_path;
# raw's unique value is provider fields missing from the normalized columns,
# which cluster at the head of the payload).
RAW_TRUNCATE = 8_000
CONTENT_TRUNCATE = 600_000
# FTS body cap — trigram tokenizes every 3-char window, so the index runs
# ~10-30x the body size. 600B/event keeps a 130k-event corpus ~200MB;
# full text stays in `events` and at the provider source.
FTS_BODY_CAP = 600

# D13 / #11: a WorkThread is leased to at most one live writer. The lease
# expires when its heartbeat is older than this, or its holder pid is gone —
# a crashed agent must never block the next one forever.
LEASE_HEARTBEAT_TIMEOUT = 120.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,      -- "provider:native_id"
    provider      TEXT NOT NULL,
    native_id     TEXT NOT NULL,
    title         TEXT,
    started_at    REAL,
    updated_at    REAL,
    cwd           TEXT,
    repo_root     TEXT,
    git_remote    TEXT,
    git_branch    TEXT,
    git_commit    TEXT,
    model         TEXT,
    message_count INTEGER DEFAULT 0,
    tool_count    INTEGER DEFAULT 0,
    can_resume    INTEGER DEFAULT 0,
    can_fork      INTEGER DEFAULT 0,
    resume_cmd    TEXT,
    metadata_json TEXT,
    raw_metadata_json TEXT,
    -- O2 retention.  Canonical runtime states (the only values runtime code
    -- writes or branches on):
    --   'ACTIVE_SOURCE'      at least one usable backing source; eligible
    --                        for live continuity
    --   'SOURCE_MISSING'     all known provider sources missing; normalized
    --                        history is retained, search/timeline keep it
    --   'ARCHIVED_CANONICAL' explicitly archived canonical Voyager copy;
    --                        historical, not live; only an explicit archive
    --                        action sets or clears it
    -- NULL / 'LIVE' / '' are migration INPUTS from pre-O2 databases only --
    -- the additive migration rewrites them to 'ACTIVE_SOURCE' at open, and
    -- no runtime path generates them.  There is deliberately no separate
    -- 'RETAINED' state: O2 behaves identically for it, and a state nothing
    -- sets is a state that gets set wrongly.
    source_state          TEXT,
    source_missing_since  REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_provider ON sessions(provider);
CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repo_root);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd ON sessions(cwd);
CREATE INDEX IF NOT EXISTS idx_sessions_source_state ON sessions(source_state);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    sid         TEXT NOT NULL,
    ts          REAL,
    seq         INTEGER,
    kind        TEXT,
    role        TEXT,
    content     TEXT,
    tool_name   TEXT,
    tool_call_id TEXT,
    tool_input  TEXT,
    tool_output TEXT,
    command     TEXT,
    stdout      TEXT,
    stderr      TEXT,
    exit_code   INTEGER,
    file_path   TEXT,
    old_content TEXT,
    new_content TEXT,
    diff        TEXT,
    files_json  TEXT,
    model       TEXT,
    usage_json  TEXT,
    origin      TEXT,
    raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_sid ON events(sid);
-- "the newest turns of one session" is the hot path for briefs and the L1
-- window; without the sort columns in the index SQLite scans every event of the
-- session and sorts them, which on a cold 1.6 GB page cache is ~38 ms per
-- session.  With them it stops after the LIMIT.
CREATE INDEX IF NOT EXISTS idx_events_sid_ts ON events(sid, ts DESC, seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_call ON events(tool_call_id);

CREATE TABLE IF NOT EXISTS files (
    sid        TEXT NOT NULL,
    path       TEXT,
    backup     TEXT,
    versions   INTEGER,
    versions_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_sid ON files(sid);

CREATE TABLE IF NOT EXISTS threads (
    id         TEXT PRIMARY KEY,
    repo_root  TEXT,
    title      TEXT,
    goal       TEXT,
    created_at REAL,
    updated_at REAL,
    status     TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS thread_sessions (
    thread_id   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    attached_at REAL,
    ord         INTEGER,
    PRIMARY KEY (thread_id, session_id)
);

CREATE TABLE IF NOT EXISTS thread_leases (
    thread_id         TEXT PRIMARY KEY,
    holder            TEXT NOT NULL,   -- provider currently allowed to write
    native_session_id TEXT,
    pid               INTEGER,
    acquired_at       REAL,
    heartbeat_at      REAL,
    lease_token       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    provider TEXT NOT NULL,
    path     TEXT NOT NULL,
    sid      TEXT NOT NULL,
    mtime    REAL,
    size     INTEGER,
    -- O2 source identity history: when we last saw this file on disk, and
    -- since when it has been missing.  Keeping this per-source (rather than a
    -- single bool on the session) is what lets doctor/timeline explain *why*
    -- a history was retained instead of just asserting that it was.
    last_seen     REAL,
    missing_since REAL,
    PRIMARY KEY (provider, path, sid)
);

CREATE TABLE IF NOT EXISTS thread_pending (
    thread_id  TEXT NOT NULL,
    provider   TEXT NOT NULL,
    -- The provider-native session id this pending expects to attach, when the
    -- recorder already knows it (a native session start does; a switch launch
    -- does not). Lets the resolver match by identity instead of by the
    -- "exactly one candidate" heuristic.
    native_session_id TEXT,
    note       TEXT,
    created_at REAL,
    repo_root  TEXT,
    cwd        TEXT,
    source_provider TEXT,
    source_session  TEXT,
    goal       TEXT,
    lease_token TEXT,
    launch_cmd TEXT,
    pid        INTEGER,
    status     TEXT DEFAULT 'open',
    resolved_at REAL,
    resolved_sid TEXT,
    PRIMARY KEY (thread_id, provider)
);

-- O3 lifecycle log.  Append-only, and deliberately small: it exists because
-- some facts have nowhere else to live.  `threads.status` has no timestamp
-- (and `updated_at` is also written by thread_touch, so it cannot stand in for
-- one), and a source coming back clears the only marker we had.  Everything
-- else in the timeline is DERIVED from canonical columns that already carry a
-- time, so this table never duplicates them -- see voyager/timeline.py.
CREATE TABLE IF NOT EXISTS thread_events (
    id         INTEGER PRIMARY KEY,
    thread_id  TEXT NOT NULL,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,
    provider   TEXT,
    session_id TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_thread_events ON thread_events(thread_id, ts, id);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(
    body, file_path, command, sid UNINDEXED,
    tokenize = 'trigram'
);
"""


def _fts_has_sid(con: sqlite3.Connection) -> bool:
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(event_fts)")]
        return "sid" in cols
    except sqlite3.Error:
        return True


def _rebuild_fts(con: sqlite3.Connection) -> None:
    """Upgrade an old index: drop and refill the FTS table from events."""
    con.execute("DROP TABLE IF EXISTS event_fts")
    con.executescript(SCHEMA)
    con.execute(
        """INSERT INTO event_fts(rowid, body, file_path, command, sid)
           SELECT id,
                  COALESCE(content,'') || ' ' || COALESCE(tool_input,'') || ' '
                    || COALESCE(tool_output,'') || ' ' || COALESCE(command,'')
                    || ' ' || COALESCE(stdout,''),
                  COALESCE(file_path,''), COALESCE(command,''),
                  COALESCE(sid,'')
           FROM events"""
    )


def default_db_path() -> Path:
    return Path.home() / ".voyager" / "index.db"


def _pid_alive(pid) -> bool:
    """Is a process with this pid running right now?

    Windows has no safe os.kill(pid, 0) — os.kill on nt with an arbitrary
    signal calls TerminateProcess — so probe via OpenProcess instead.
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            # 87 = bad parameters (no such process); 5 = exists, denied
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return ctypes.get_last_error() == 5
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    import errno
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def lease_state(row, now=None) -> Dict[str, Any]:
    """Classify a lease row: {"held", "expired", "why"}.

    Not held -> free. Held but expired when the heartbeat is stale
    (LEASE_HEARTBEAT_TIMEOUT) or the holder pid is gone; a pid-less lease
    lives and dies by its heartbeat alone.
    """
    import time as _time
    now = _time.time() if now is None else now
    if row is None:
        return {"held": False, "expired": False, "why": "free"}
    hb = row["heartbeat_at"]
    if hb is None or now - hb > LEASE_HEARTBEAT_TIMEOUT:
        return {"held": True, "expired": True, "why": "heartbeat stale"}
    if row["pid"] and not _pid_alive(row["pid"]):
        return {"held": True, "expired": True, "why": "pid gone"}
    return {"held": True, "expired": False, "why": "active"}


def _truncate(s: Optional[str], limit: int) -> Optional[str]:
    if s is None:
        return None
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... [truncated {len(s) - limit} chars; full text at source]"


def _fold_origin(origin: Any) -> Any:
    """`UNKNOWN` is a classifier sentinel, never a stored state.

    `new_event` folds it, but `replace_session` writes event dicts directly, so
    the same rule has to hold here or a hand-built row could store the literal
    and make `origin IS NOT NULL` stop meaning "classified".
    """
    return None if origin == ORIGIN_UNKNOWN else origin


#: O3 — the timeline event a WorkThread status change produces.  A status is a
#: single value with no history, so the transition is logged when it happens.
_STATUS_EVENT = {
    "closed": "THREAD_CLOSED",
    "archived": "THREAD_ARCHIVED",
    "active": "THREAD_REOPENED",
}


class Store:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.db_path))
        self.con.row_factory = sqlite3.Row
        # WAL + relaxed sync: bulk re-indexing does hundreds of MB in
        # many small transactions; the delete-journal default turns each
        # commit into a double-fsync on a growing file.
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("PRAGMA cache_size=-64000")   # 64MB page cache
        # migration: pre-0.1.1 sources had PK (provider, path) only, which
        # collapsed multi-session artifacts (one SQLite DB -> N sessions)
        # into a single row and let prune wipe sessions on unchanged scans.
        pk_cols = [r[1] for r in self.con.execute("PRAGMA table_info(sources)") if r[5]]
        if pk_cols and "sid" not in pk_cols:
            self.con.execute("DROP TABLE sources")
        # additive migration: pre-Automatic-Continuity thread_pending rows
        # lack the metadata columns the pending auto-resolution matches on
        pend_cols = [r[1] for r in self.con.execute("PRAGMA table_info(thread_pending)")]
        if pend_cols:
            for col, decl in (("native_session_id", "TEXT"),
                              ("repo_root", "TEXT"), ("cwd", "TEXT"),
                              ("source_provider", "TEXT"),
                              ("source_session", "TEXT"), ("goal", "TEXT"),
                              ("lease_token", "TEXT"), ("launch_cmd", "TEXT"),
                              ("pid", "INTEGER"), ("status", "TEXT DEFAULT 'open'"),
                              ("resolved_at", "REAL"), ("resolved_sid", "TEXT")):
                if col not in pend_cols:
                    self.con.execute(
                        f"ALTER TABLE thread_pending ADD COLUMN {col} {decl}")
        # additive migration: provenance.  Pre-provenance rows keep NULL, which
        # the API layer surfaces as "unknown"; nothing is backfilled here -- that
        # is the deterministic enrichment pass, and it only ever fills NULLs.
        ev_cols = [r[1] for r in self.con.execute("PRAGMA table_info(events)")]
        if ev_cols and "origin" not in ev_cols:
            self.con.execute("ALTER TABLE events ADD COLUMN origin TEXT")
        # additive migration: O2 retention + O2 state model.  Existing rows
        # keep NULL until the backfill below, which normalises the pre-O2
        # spellings ('LIVE' and NULL) to the canonical ACTIVE_SOURCE.  The
        # backfill is idempotent: after the first run it updates zero rows.
        ses_cols = [r[1] for r in self.con.execute("PRAGMA table_info(sessions)")]
        if ses_cols:
            for col, decl in (("source_state", "TEXT"),
                              ("source_missing_since", "REAL")):
                if col not in ses_cols:
                    self.con.execute(
                        f"ALTER TABLE sessions ADD COLUMN {col} {decl}")
            self.con.execute(
                "UPDATE sessions SET source_state='ACTIVE_SOURCE'"
                " WHERE source_state='LIVE' OR source_state IS NULL"
                " OR source_state=''")
        src_cols = [r[1] for r in self.con.execute("PRAGMA table_info(sources)")]
        if src_cols:
            for col, decl in (("last_seen", "REAL"), ("missing_since", "REAL")):
                if col not in src_cols:
                    self.con.execute(
                        f"ALTER TABLE sources ADD COLUMN {col} {decl}")
        self.con.executescript(SCHEMA)
        if not _fts_has_sid(self.con):
            _rebuild_fts(self.con)
        self.con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema', '1')")
        self.con.commit()

    # -- source tracking ---------------------------------------------------

    @staticmethod
    def source_fingerprint(path: Path) -> Tuple[float, int]:
        st = os.stat(path)
        return st.st_mtime, st.st_size

    def source_changed(self, provider: str, path: Path) -> bool:
        """A path is unchanged if ANY row for it carries the current fingerprint.

        Multi-session artifacts have one row per (path, sid); single-session
        artifacts have exactly one.
        """
        mtime, size = self.source_fingerprint(path)
        row = self.con.execute(
            "SELECT 1 FROM sources WHERE provider=? AND path=? AND mtime=? AND size=? LIMIT 1",
            (provider, str(path), mtime, size),
        ).fetchone()
        return row is None

    # -- writing -----------------------------------------------------------

    def replace_session(
        self,
        session: Dict[str, Any],
        events: Iterable[Dict[str, Any]],
        provider: str,
        source_path: Path,
        extra_sources: Optional[List[Path]] = None,
    ) -> None:
        """Atomically (re)write one session and mark its source fresh."""
        sid = session["id"]
        evs = list(events)
        raw_meta = session.get("raw_metadata") or {}
        meta = session.get("metadata") or {}
        import time as _time
        seen_at = _time.time()
        self.con.execute("BEGIN")
        # O3: read the retention state BEFORE it is overwritten below.
        # Re-ingesting a session whose sources had all vanished IS the
        # reconcile, and that transition is a timeline fact.
        prev = self.con.execute(
            "SELECT source_state FROM sessions WHERE id=?", (sid,)).fetchone()
        was_retained = bool(prev) and prev["source_state"] == "SOURCE_MISSING"
        # O2: an explicit archive is a user decision -- passive re-ingest
        # must never silently un-archive a session the user fixated.
        reingest_state = (
            "ARCHIVED_CANONICAL"
            if bool(prev) and prev["source_state"] == "ARCHIVED_CANONICAL"
            else "ACTIVE_SOURCE")
        try:
            self.con.execute(
                "DELETE FROM event_fts WHERE rowid IN (SELECT id FROM events WHERE sid=?)",
                (sid,),
            )
            self.con.execute("DELETE FROM events WHERE sid=?", (sid,))
            self.con.execute("DELETE FROM files WHERE sid=?", (sid,))
            self.con.execute(
                """INSERT OR REPLACE INTO sessions(
                       id, provider, native_id, title, started_at, updated_at,
                       cwd, repo_root, git_remote, git_branch, git_commit,
                       model, message_count, tool_count,
                       can_resume, can_fork, resume_cmd,
                       metadata_json, raw_metadata_json,
                       source_state, source_missing_since)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (
                    sid, provider, session.get("native_session_id"),
                    session.get("title"), session.get("started_at"),
                    session.get("updated_at"), session.get("cwd"),
                    session.get("repo_root"), session.get("git_remote"),
                    session.get("git_branch"), session.get("git_commit"),
                    session.get("model"), session.get("message_count", 0),
                    session.get("tool_count", 0),
                    1 if session.get("can_resume") else 0,
                    1 if session.get("can_fork") else 0,
                    session.get("resume_cmd"),
                    json.dumps(meta, ensure_ascii=False),
                    json.dumps(raw_meta, ensure_ascii=False),
                    reingest_state,
                ),
            )
            for ev in evs:
                cur = self.con.execute(
                    """INSERT INTO events(
                           sid, ts, seq, kind, role, content,
                           tool_name, tool_call_id, tool_input, tool_output,
                           command, stdout, stderr, exit_code,
                           file_path, old_content, new_content, diff,
                           files_json, model, usage_json, origin, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sid, ev.get("ts"), ev.get("seq"), ev.get("kind"),
                        ev.get("role"),
                        _truncate(ev.get("content"), CONTENT_TRUNCATE),
                        ev.get("tool_name"), ev.get("tool_call_id"),
                        _truncate(ev.get("tool_input"), CONTENT_TRUNCATE),
                        _truncate(ev.get("tool_output"), CONTENT_TRUNCATE),
                        ev.get("command"), _truncate(ev.get("stdout"), CONTENT_TRUNCATE),
                        _truncate(ev.get("stderr"), CONTENT_TRUNCATE),
                        ev.get("exit_code"), ev.get("file_path"),
                        None, None, ev.get("diff"),
                        json.dumps(ev.get("files") or [], ensure_ascii=False),
                        ev.get("model"),
                        json.dumps(ev.get("usage") or {}, ensure_ascii=False)
                        if ev.get("usage") else None,
                        (_fold_origin(ev.get("origin"))),
                        _truncate(
                            json.dumps(ev.get("raw_event"), ensure_ascii=False)
                            if ev.get("raw_event") is not None else None,
                            RAW_TRUNCATE,
                        ),
                    ),
                )
                body = " ".join(
                    x for x in (
                        ev.get("content"), ev.get("tool_input"), ev.get("tool_output"),
                        ev.get("command"), ev.get("stdout"),
                    ) if x
                )[:FTS_BODY_CAP]   # trigram over MB bodies explodes the index
                fpath = ev.get("file_path") or " ".join(ev.get("files") or [])
                self.con.execute(
                    "INSERT INTO event_fts(rowid, body, file_path, command, sid) VALUES (?,?,?,?,?)",
                    (cur.lastrowid, body, fpath, ev.get("command") or "", sid),
                )
            for f in session.get("_files", []) or []:
                self.con.execute(
                    "INSERT INTO files(sid, path, backup, versions, versions_json) VALUES (?,?,?,?,?)",
                    (sid, f.get("path"), f.get("backup"), f.get("versions"),
                     json.dumps(f.get("versions_json") or [], ensure_ascii=False)),
                )
            mtime, size = self.source_fingerprint(source_path)
            # last_seen/missing_since are cleared here: re-ingesting a source is
            # exactly the "source came back" reconcile, and leaving a stale
            # missing_since would make doctor report a healthy source as gone.
            self.con.execute(
                "INSERT OR REPLACE INTO sources(provider, path, sid, mtime, size,"
                " last_seen, missing_since) VALUES (?,?,?,?,?,?,NULL)",
                (provider, str(source_path), sid, mtime, size, seen_at),
            )
            for p in extra_sources or []:
                try:
                    m2, s2 = self.source_fingerprint(p)
                    self.con.execute(
                        "INSERT OR REPLACE INTO sources(provider, path, sid, mtime,"
                        " size, last_seen, missing_since)"
                        " VALUES (?,?,?,?,?,?,NULL)",
                        (provider, str(p), sid, m2, s2, seen_at),
                    )
                except OSError:
                    pass
            if was_retained:
                # O3: the session just came back.  Log it inside this
                # transaction so the marker and the timeline fact cannot
                # disagree.
                for t in self.q("SELECT thread_id FROM thread_sessions"
                                " WHERE session_id=?", (sid,)):
                    self.thread_event_record(
                        t["thread_id"], "SOURCE_RETURNED", provider=provider,
                        session_id=sid, detail={"via": "re-ingest"},
                        commit=False)
            self.con.execute("COMMIT")
        except Exception:
            try:
                self.con.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # COMMIT itself failed: sqlite already rolled back
            raise

    def prune_missing_sessions(self, provider: str, disk_paths: set) -> int:
        """Mark sessions of `provider` whose source files have ALL vanished.

        O2 invariant: **absence of a source is not proof that the user wants the
        history deleted.**  This used to DELETE the session, its events, its
        file rows and its FTS rows; because the provider file is already gone at
        that point, the index held the only normalized copy, so a provider
        rotating its own storage silently erased history.  It now marks instead:

        * every source of the session missing  -> ``source_state='SOURCE_MISSING'``
          (events, files and FTS rows are kept; search/timeline/thread summaries
          still find it, continuity does not);
        * at least one source still on disk    -> ``source_state`` stays LIVE and
          the marker is cleared, so a source that comes back reconciles itself.

        Only sessions that HAVE source rows are managed here: sessions seeded
        without one (synthetic tests, manual inserts) have a lifecycle the
        caller cannot know, so they are never touched.

        Returns the number of sessions *newly* marked missing.  There is no
        automatic purge — a real deletion has to be an explicit command.
        """
        import time as _time
        now = _time.time()
        rows = self.con.execute(
            "SELECT id, source_state FROM sessions WHERE provider=?", (provider,)
        ).fetchall()
        marked = []
        for r in rows:
            sid = r["id"]
            src_rows = self.q(
                "SELECT path FROM sources WHERE provider=? AND sid=?",
                (provider, sid),
            )
            if not src_rows:
                continue  # manually seeded; not ours to manage
            all_missing = not any(sr["path"] in disk_paths for sr in src_rows)
            if all_missing:
                # per-source history first: this is what lets doctor/timeline
                # explain why the history was retained
                for sr in src_rows:
                    self.con.execute(
                        "UPDATE sources SET missing_since=COALESCE(missing_since, ?)"
                        " WHERE provider=? AND path=? AND sid=?",
                        (now, provider, sr["path"], sid))
                self.con.execute(
                    "UPDATE sources SET last_seen=? WHERE provider=? AND sid=?"
                    " AND missing_since IS NULL", (now, provider, sid))
                if r["source_state"] != "SOURCE_MISSING":
                    marked.append(sid)
                    # O3: record the episode when it happens.  Once the source
                    # comes back nothing else remembers it was ever gone, and
                    # "Codex's source rotated here" is exactly the history a
                    # timeline exists to show.
                    for t in self.q("SELECT thread_id FROM thread_sessions"
                                    " WHERE session_id=?", (sid,)):
                        self.thread_event_record(
                            t["thread_id"], "SOURCE_MISSING", provider=provider,
                            session_id=sid, ts=now,
                            detail={"missing_sources": sorted(
                                str(sr["path"]) for sr in src_rows)},
                            commit=False)
                self.con.execute(
                    "UPDATE sessions SET source_state='SOURCE_MISSING',"
                    " source_missing_since=COALESCE(source_missing_since, ?)"
                    " WHERE id=?", (now, sid))
            else:
                # at least one source is back: clear both markers.  Per-source
                # updates instead of a giant IN clause: disk_paths can outrun
                # SQLite's variable limit at six-figure session counts (O1
                # found "too many SQL variables" at 100k sources).
                for sr in src_rows:
                    if sr["path"] in disk_paths:
                        self.con.execute(
                            "UPDATE sources SET last_seen=?, missing_since=NULL"
                            " WHERE provider=? AND sid=? AND path=?",
                            (now, provider, sid, sr["path"]))
                    else:
                        self.con.execute(
                            "UPDATE sources SET missing_since=COALESCE("
                            "missing_since, ?) WHERE provider=? AND sid=?"
                            " AND path=?",
                            (now, provider, sr["path"], sid))
                self.con.execute(
                    "UPDATE sessions SET source_state='ACTIVE_SOURCE',"
                    " source_missing_since=NULL WHERE id=?", (sid,))
                if r["source_state"] == "SOURCE_MISSING":
                    # O3: the return is a timeline fact with nowhere else to
                    # live -- clearing the marker erases the only trace of it.
                    for t in self.q("SELECT thread_id FROM thread_sessions"
                                    " WHERE session_id=?", (sid,)):
                        self.thread_event_record(
                            t["thread_id"], "SOURCE_RETURNED",
                            provider=provider, session_id=sid,
                            detail={"sources": sorted(str(p) for p in disk_paths)},
                            commit=False)
        self.con.commit()
        return len(marked)

    # -- WorkThreads (Phase 2) ---------------------------------------------

    def thread_create(self, repo_root=None, title=None, goal=None) -> str:
        import time as _time
        import uuid as _uuid
        tid = "thr_" + _uuid.uuid4().hex[:10]
        now = _time.time()
        self.con.execute(
            "INSERT INTO threads(id, repo_root, title, goal, created_at,"
            " updated_at, status) VALUES (?,?,?,?,?,?,'active')",
            (tid, repo_root, title, goal, now, now))
        self.con.commit()
        return tid

    def thread_get(self, tid: str):
        """Exact id or unambiguous prefix; returns None otherwise."""
        rows = self.q("SELECT * FROM threads WHERE id=?", (tid,))
        if rows:
            return rows[0]
        esc = tid.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.q(
            "SELECT * FROM threads WHERE id LIKE ? ESCAPE '\\' ORDER BY id",
            (esc,))
        return rows[0] if len(rows) == 1 else None

    def thread_set_status(self, tid: str, status: str) -> None:
        import time as _time
        prev = self.con.execute(
            "SELECT status FROM threads WHERE id=?", (tid,)).fetchone()
        if prev is None:
            return
        if (prev["status"] or "active") == status:
            # active → active is not a transition: the timeline records
            # changes, not restatements
            return
        now = _time.time()
        self.con.execute("UPDATE threads SET status=?, updated_at=? WHERE id=?",
                         (status, now, tid))
        self.con.commit()
        # O3: when a status changed has nowhere else to live -- `status` is a
        # single value and `updated_at` is also written by thread_touch, so it
        # cannot stand in for a transition time.
        self.thread_event_record(
            tid, _STATUS_EVENT.get(status, "THREAD_STATUS"), ts=now,
            detail={"from": prev["status"], "to": status})

    def thread_event_record(self, tid: str, kind: str, *, provider=None,
                            session_id=None, detail=None,
                            ts: Optional[float] = None,
                            commit: bool = True) -> None:
        """Append one lifecycle fact to the O3 timeline log.

        Deliberately narrow: only for facts that have no other timestamped
        home (status transitions, a source coming back).  Everything else in
        the timeline is derived from canonical columns that already carry a
        time, so nothing here duplicates them.

        `commit=False` when the caller is already inside a transaction -- a
        commit here would end that transaction early.
        """
        import time as _time
        self.con.execute(
            "INSERT INTO thread_events(thread_id, ts, kind, provider,"
            " session_id, detail_json) VALUES (?,?,?,?,?,?)",
            (tid, ts if ts is not None else _time.time(), kind, provider,
             session_id,
             json.dumps(detail, ensure_ascii=False) if detail else None))
        if commit:
            self.con.commit()

    def thread_events_list(self, tid: str) -> List[sqlite3.Row]:
        return self.q(
            "SELECT * FROM thread_events WHERE thread_id=? ORDER BY ts, id",
            (tid,))

    def thread_touch(self, tid: str) -> None:
        import time as _time
        self.con.execute("UPDATE threads SET updated_at=? WHERE id=?",
                         (_time.time(), tid))
        self.con.commit()

    def thread_attach(self, tid: str, sid: str) -> bool:
        """Attach a session; duplicates are ignored. Returns True if newly
        attached. Attaching requires the session to exist."""
        if not self.q("SELECT 1 FROM sessions WHERE id=?", (sid,)):
            return False
        import time as _time
        cur = self.con.execute(
            "SELECT MAX(ord) m FROM thread_sessions WHERE thread_id=?", (tid,))
        nxt = (cur.fetchone()["m"] or 0) + 1
        cur2 = self.con.execute(
            "INSERT OR IGNORE INTO thread_sessions VALUES (?,?,?,?)",
            (tid, sid, _time.time(), nxt))
        self.con.commit()
        self.thread_touch(tid)
        return cur2.rowcount > 0

    def thread_members(self, tid: str) -> List[sqlite3.Row]:
        """Member session rows in attach order; silently skips sessions that
        were pruned from the index."""
        return self.q(
            """SELECT s.* FROM thread_sessions t
               JOIN sessions s ON s.id = t.session_id
               WHERE t.thread_id=? ORDER BY t.ord""", (tid,))

    def live_thread_members(self, tid: str) -> List[sqlite3.Row]:
        """Members that may still feed continuity.

        A session that is not an active source (every source vanished, or the
        user explicitly archived it) is retained as history (O2), but it must
        not be compiled into a continuation context, offered as a
        native-resume candidate, or counted as live source health.  Display
        paths keep using `thread_members`, which returns it — marked, not
        hidden.
        """
        return self.q(
            """SELECT s.* FROM thread_sessions t
               JOIN sessions s ON s.id = t.session_id
               WHERE t.thread_id=?
                 AND COALESCE(s.source_state, 'ACTIVE_SOURCE')
                     = 'ACTIVE_SOURCE'
               ORDER BY t.ord""", (tid,))

    def retained_sessions(self, provider: Optional[str] = None) -> List[sqlite3.Row]:
        """Sessions retained because every one of their sources vanished."""
        if provider:
            return self.q(
                "SELECT * FROM sessions WHERE source_state='SOURCE_MISSING'"
                " AND provider=? ORDER BY source_missing_since", (provider,))
        return self.q(
            "SELECT * FROM sessions WHERE source_state='SOURCE_MISSING'"
            " ORDER BY source_missing_since")

    def retained_stats(self) -> Dict[str, Any]:
        """The cost side of O2: how much retained history is being held.

        `bytes` counts event content/tool payload columns only — it is an
        approximation of the retained payload, not of the whole database.
        """
        rows = self.q(
            """SELECT s.provider AS provider,
                      COUNT(DISTINCT s.id) AS sessions,
                      COUNT(e.id) AS events,
                      COALESCE(SUM(COALESCE(LENGTH(e.content), 0)
                                   + COALESCE(LENGTH(e.tool_input), 0)
                                   + COALESCE(LENGTH(e.tool_output), 0)), 0) AS bytes,
                      MIN(s.source_missing_since) AS oldest
               FROM sessions s LEFT JOIN events e ON e.sid = s.id
               WHERE s.source_state='SOURCE_MISSING'
               GROUP BY s.provider ORDER BY sessions DESC""")
        provs = [dict(r) for r in rows]
        return {
            "providers": provs,
            "sessions": sum(p["sessions"] for p in provs),
            "events": sum(p["events"] for p in provs),
            "bytes": sum((p["bytes"] or 0) for p in provs),
            "oldest": min((p["oldest"] for p in provs if p["oldest"]),
                          default=None),
        }

    def mark_canonical_archived(self, sid: str) -> bool:
        """Explicitly fixate a session as ARCHIVED_CANONICAL (O2).

        Only an explicit archive action may set this state: it declares that
        Voyager holds the canonical archival copy of the session's history.
        It is never set automatically (a missing source is SOURCE_MISSING,
        not an archive).  Returns True when the state actually changed.

        O3: the transition is logged to thread_events.  `source_state` keeps
        only the current value, so ACTIVE_SOURCE→ARCHIVED and
        SOURCE_MISSING→ARCHIVED would be unrecoverable otherwise.  A repeat
        archive (ARCHIVED→ARCHIVED) changes nothing and records nothing.
        """
        prev = self.con.execute(
            "SELECT provider, source_state FROM sessions WHERE id=?",
            (sid,)).fetchone()
        if prev is None:
            return False
        prev_state = prev["source_state"] or "ACTIVE_SOURCE"
        if prev_state == "ARCHIVED_CANONICAL":
            return False
        cur = self.con.execute(
            "UPDATE sessions SET source_state='ARCHIVED_CANONICAL',"
            " source_missing_since=NULL WHERE id=?", (sid,))
        if cur.rowcount:
            for t in self.q("SELECT thread_id FROM thread_sessions"
                            " WHERE session_id=?", (sid,)):
                self.thread_event_record(
                    t["thread_id"], "SOURCE_ARCHIVED",
                    provider=prev["provider"], session_id=sid,
                    detail={"from": prev_state}, commit=False)
        self.con.commit()
        return cur.rowcount > 0

    def thread_member_ids(self, tid: str) -> List[str]:
        return [r["session_id"] for r in self.q(
            "SELECT session_id FROM thread_sessions WHERE thread_id=? ORDER BY ord",
            (tid,))]
    
    def thread_member_sessions(self, tid: str) -> List[dict]:
        """Return member sessions as dicts with metadata."""
        rows = self.q(
            """SELECT s.* FROM thread_sessions ts
               JOIN sessions s ON s.id = ts.session_id
               WHERE ts.thread_id=? ORDER BY ts.ord""", (tid,))
        return [dict(r) for r in rows]

    def thread_find_by_members(self, sids: set) -> Optional[str]:
        """Return the active thread whose member set is exactly `sids`."""
        for r in self.q("SELECT id FROM threads WHERE status='active'"):
            if set(self.thread_member_ids(r["id"])) == sids:
                return r["id"]
        return None

    def thread_find_containing(self, sids) -> Optional[str]:
        """Return the active thread that contains ALL of `sids`.

        Containment, not equality: handing off *one* member of a three-member
        WorkThread must still find (and lease) that thread — which is why
        `thread_find_by_members` is not enough here.  A session that sits in
        two active threads resolves to the most recently updated one, so the
        choice is deterministic rather than row-order dependent.
        """
        sids = list(sids)
        if not sids:
            return None
        rows = self.q(
            "SELECT t.id AS id, t.updated_at AS updated_at "
            "FROM threads t JOIN thread_sessions ts ON ts.thread_id = t.id "
            "WHERE t.status='active' AND ts.session_id IN ({0}) "
            "GROUP BY t.id HAVING COUNT(ts.session_id) = ?".format(
                ", ".join("?" * len(sids))),
            tuple(sids) + (len(sids),))
        if not rows:
            return None
        return max(rows, key=lambda r: r["updated_at"] or 0)["id"]

    def thread_list(self, status: str = "active"):
        return self.q(
            """SELECT t.*, COUNT(ts.session_id) members
               FROM threads t LEFT JOIN thread_sessions ts
                 ON ts.thread_id = t.id
               WHERE t.status=?
               GROUP BY t.id ORDER BY t.updated_at DESC""", (status,))

    # -- WorkThread single-writer leases (Phase 2b / D13 / #11) -------------

    def _lease_log(self, event: str, tid: str, holder, pid, token: str) -> None:
        """Append-only audit trail for lease grants, steals and releases."""
        import time as _time
        from datetime import datetime
        line = "{0} | {1} | thread={2} | holder={3} | pid={4} | token={5}\n".format(
            datetime.fromtimestamp(_time.time()).isoformat(timespec="seconds"),
            event, tid, holder, pid, (token or "")[:8])
        try:
            with open(self.db_path.parent / "leases.log", "a",
                      encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # the lease itself is the source of truth; audit is best-effort

    def thread_lease_get(self, tid):
        rows = self.q("SELECT * FROM thread_leases WHERE thread_id=?", (tid,))
        return rows[0] if rows else None

    def thread_lease_acquire(self, tid: str, holder: str,
                             native_session_id=None, pid=None,
                             steal: bool = False) -> tuple:
        """Atomically take the single-writer lease on a WorkThread.

        Returns (granted, lease_row): the fresh row on success, the
        blocker's row on refusal, (False, None) when the thread doesn't
        exist. A live lease blocks unless steal=True; an expired one
        (stale heartbeat or dead pid) is taken over. BEGIN IMMEDIATE
        serializes concurrent acquirers on the same index.db — no second
        process can slip through between read and write.
        """
        if not self.q("SELECT 1 FROM threads WHERE id=?", (tid,)):
            return False, None
        import time as _time
        import uuid as _uuid
        try:
            self.con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # could not take the write lock at all: refuse, never race
            return False, self.thread_lease_get(tid)
        try:
            now = _time.time()
            row = self.thread_lease_get(tid)
            st = lease_state(row, now)
            event = ("acquire" if row is None
                     else "steal" if steal else "takeover-expired")
            if st["held"] and not st["expired"] and not steal:
                if row["holder"] == holder:
                    # same provider re-acquiring its own lease = transfer
                    event = "transfer"
                else:
                    self.con.execute("COMMIT")
                    return False, row
            token = _uuid.uuid4().hex
            self.con.execute(
                """INSERT OR REPLACE INTO thread_leases(
                       thread_id, holder, native_session_id, pid,
                       acquired_at, heartbeat_at, lease_token)
                   VALUES (?,?,?,?,?,?,?)""",
                (tid, holder, native_session_id, pid, now, now, token))
            self.con.execute("COMMIT")
        except Exception:
            try:
                self.con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        self._lease_log(event, tid, holder, pid, token)
        return True, self.thread_lease_get(tid)

    def thread_lease_renew(self, tid: str, token: str) -> bool:
        """Refresh the lease heartbeat; only the token holder may."""
        import time as _time
        cur = self.con.execute(
            "UPDATE thread_leases SET heartbeat_at=? "
            "WHERE thread_id=? AND lease_token=?",
            (_time.time(), tid, token))
        self.con.commit()
        return cur.rowcount > 0

    def thread_lease_release(self, tid: str, token: str,
                             reason: str = "voluntary") -> bool:
        """Give the lease up; only the token holder (or a steal via the
        same row's token) can. Returns False when token doesn't match —
        that caller must use steal semantics instead."""
        row = self.thread_lease_get(tid)
        if row is None or row["lease_token"] != token:
            return False
        self.con.execute(
            "DELETE FROM thread_leases WHERE thread_id=? AND lease_token=?",
            (tid, token))
        self.con.commit()
        self._lease_log("release-" + reason, tid, row["holder"],
                        row["pid"], token)
        return True

    def thread_lease_renew_alive(self) -> int:
        """D13: watch is the heartbeat. Renew every lease whose holder pid
        is still alive; pid-less or dead-pid leases are left to expire on
        their heartbeat, so a crashed agent never holds the thread forever."""
        import time as _time
        now = _time.time()
        renewed = 0
        for row in self.q("SELECT * FROM thread_leases"):
            if row["pid"] and _pid_alive(row["pid"]):
                self.con.execute(
                    "UPDATE thread_leases SET heartbeat_at=? WHERE thread_id=?",
                    (now, row["thread_id"]))
                renewed += 1
        if renewed:
            self.con.commit()
        return renewed

    def thread_lease_active_count(self, now=None) -> int:
        """Live (held, not expired) leases — watch uses this to keep its
        sleep interval well under the heartbeat expiry."""
        import time as _time
        now = _time.time() if now is None else now
        n = 0
        for row in self.q("SELECT * FROM thread_leases"):
            st = lease_state(row, now)
            if st["held"] and not st["expired"]:
                n += 1
        return n

    # -- pending attach records (Automatic Continuity) ---------------------

    PENDING_COLUMNS = ("thread_id", "provider", "native_session_id", "note",
                       "created_at", "repo_root", "cwd", "source_provider",
                       "source_session", "goal", "lease_token",
                       "launch_cmd", "pid", "status", "resolved_at",
                       "resolved_sid")

    def pending_record(self, thread_id: str, provider: str, **meta) -> None:
        """Record (or refresh) an open pending-attach record with the launch
        metadata the auto-resolver matches on. One open record per
        (thread_id, provider); a re-launch replaces the previous record."""
        import time as _time
        vals = {"thread_id": thread_id, "provider": provider,
                "created_at": _time.time(), "status": "open"}
        for k, v in meta.items():
            if k in self.PENDING_COLUMNS and k not in ("thread_id", "provider"):
                vals[k] = v
        cols = list(self.PENDING_COLUMNS)
        self.con.execute(
            "INSERT OR REPLACE INTO thread_pending({0}) VALUES ({1})".format(
                ", ".join(cols), ", ".join("?" * len(cols))),
            tuple(vals.get(c) for c in cols))
        self.con.commit()

    def pending_open(self, thread_id: str = None, provider: str = None) -> List[sqlite3.Row]:
        """Open (unresolved, not stale) pending-attach records."""
        sql = "SELECT rowid AS rid, * FROM thread_pending "               "WHERE COALESCE(status, 'open') = 'open'"
        args = []
        if thread_id:
            sql += " AND thread_id=?"
            args.append(thread_id)
        if provider:
            sql += " AND provider=?"
            args.append(provider)
        return self.q(sql, tuple(args))

    def pending_get_by_rowid(self, rid) -> Optional[sqlite3.Row]:
        return self.q("SELECT rowid AS rid, * FROM thread_pending WHERE rowid=?",
                      (rid,))[0]

    def pending_mark(self, rid, status: str, resolved_sid: str = None) -> None:
        import time as _time
        self.con.execute(
            "UPDATE thread_pending SET status=?, resolved_at=?, resolved_sid=? "
            "WHERE rowid=?", (status, _time.time(), resolved_sid, rid))
        self.con.commit()

    def thread_pending_list(self, tid: str) -> List[sqlite3.Row]:
        """Open pending records for display (thread show)."""
        return self.q(
            "SELECT * FROM thread_pending WHERE thread_id=? "
            "AND COALESCE(status, 'open') = 'open' ORDER BY created_at",
            (tid,))

    def thread_pending_clear(self, tid: str, provider: str) -> None:
        self.con.execute(
            "DELETE FROM thread_pending WHERE thread_id=? AND provider=?",
            (tid, provider))
        self.con.commit()

    def attached_to_any_thread(self, sid: str) -> bool:
        return bool(self.q(
            "SELECT 1 FROM thread_sessions WHERE session_id=?", (sid,)))

    def attached_to_thread(self, tid: str, sid: str) -> bool:
        """Is `sid` a member of `tid`?

        This method did not exist, yet `startup.py` called it on the
        already-attached path — so that path raised AttributeError the moment a
        session was genuinely already attached, instead of returning the state
        it was written to return.
        """
        return bool(self.q(
            "SELECT 1 FROM thread_sessions WHERE thread_id=? AND session_id=?",
            (tid, sid)))

    # -- continuity audit log (metadata only, never session content) -------

    def _continuity_log(self, event: str, **meta) -> None:
        """Append an automatic-continuity event to continuity.log.

        Only ids/providers/timestamps are logged — never user content."""
        from datetime import datetime
        meta_s = " ".join("{0}={1}".format(k, v) for k, v in meta.items())
        stamp = datetime.now().isoformat(timespec="seconds")
        try:
            with open(self.db_path.parent / "continuity.log", "a",
                      encoding="utf-8") as f:
                f.write(stamp + " | " + event + " | " + meta_s + "\n")
        except OSError:
            pass

    def pending_mark_open_resolved(self, tid: str, provider: str) -> None:
        """Mark open pending records for (thread, provider) resolved — the
        target agent's new session showed up and was attached."""
        import time as _time
        self.con.execute(
            "UPDATE thread_pending SET status='resolved', resolved_at=?, "
            "resolved_sid=(SELECT session_id FROM thread_sessions "
            "WHERE thread_id=? AND session_id LIKE ?) "
            "WHERE thread_id=? AND provider=? "
            "AND COALESCE(status, 'open') = 'open'",
            (_time.time(), tid, provider.replace("%", "") + "%", tid, provider))
        self.con.commit()

    # -- reading -----------------------------------------------------------

    def q(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    # -- meta key/value ----------------------------------------------------
    # Small persistent state that must outlive a single Store instance. The
    # startup-continuity cache lives here because callers such as the Claude
    # SessionStart hook construct a fresh Store per invocation, so an
    # in-memory cache on the instance can never be read back.

    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read a meta value. Never raises — returns `default` on any error."""
        try:
            rows = self.q("SELECT value FROM meta WHERE key=?", (key,))
        except Exception:
            return default
        return rows[0]["value"] if rows else default

    def meta_set(self, key: str, value: str) -> bool:
        """Write a meta value (upsert). Returns False if it could not be stored.

        Never raises. The return value matters: a silently dropped write would
        make a permanently-broken cache indistinguishable from a cold one.
        """
        try:
            self.con.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.con.commit()
        except Exception:
            return False
        return True

    def meta_delete(self, key: str) -> bool:
        """Delete a meta value. Returns False on failure. Never raises."""
        try:
            self.con.execute("DELETE FROM meta WHERE key=?", (key,))
            self.con.commit()
        except Exception:
            return False
        return True

    def sessions(self, provider: Optional[str] = None) -> List[sqlite3.Row]:
        if provider:
            return self.q(
                "SELECT * FROM sessions WHERE provider=? ORDER BY updated_at DESC",
                (provider,),
            )
        return self.q("SELECT * FROM sessions ORDER BY updated_at DESC")

    def session(self, id_or_prefix: str) -> tuple:
        """Resolve a session ref. Returns (row, ambiguous_candidates).

        row is None when nothing matched; ambiguous_candidates is non-empty
        when the ref matched several sessions and the caller should ask the
        user to disambiguate.
        """
        rows = self.q("SELECT * FROM sessions WHERE id=? OR native_id=?", (id_or_prefix, id_or_prefix))
        if len(rows) == 1:
            return rows[0], []
        if len(rows) > 1:
            return None, rows
        esc = id_or_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.q(
            "SELECT * FROM sessions WHERE id LIKE ? ESCAPE '\\' "
            "OR native_id LIKE ? ESCAPE '\\' ORDER BY updated_at DESC",
            (f"{esc}%", f"{esc}%"),
        )
        if len(rows) == 1:
            return rows[0], []
        if len(rows) > 1:
            return None, rows
        return None, []

    def events(self, sid: str) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM events WHERE sid=? ORDER BY seq, id", (sid,))

    def search(self, query: str, limit: int = 50,
               filters: Optional[Dict[str, Any]] = None) -> List[sqlite3.Row]:
        """Substring search over every event body, with optional filters.

        The user's text is always passed as ONE quoted FTS5 phrase: queries
        like `pytest -q`, `a:b` or `"unbalanced` are ordinary text to a human
        but operators/syntax errors to FTS5, and this is a substring search,
        not a query language.

        Filters narrow *where* to look; they never change what the phrase means.
        Recognised keys: ``providers``, ``repo``, ``since``, ``until``,
        ``kinds``, ``tool``, ``file``, ``origins``, ``human_only``.
        """
        phrase = '"' + (query or "").replace('"', '""') + '"'
        where: List[str] = []
        args: List[Any] = [phrase]
        f = filters or {}

        def _in(column: str, values) -> None:
            values = [v for v in (values or []) if v]
            if values:
                where.append("%s IN (%s)" % (column, ",".join("?" * len(values))))
                args.extend(values)

        _in("s.provider", f.get("providers"))
        if f.get("repo"):
            where.append("COALESCE(s.repo_root, s.cwd, '') LIKE ?")
            args.append("%" + str(f["repo"]) + "%")
        if f.get("since") is not None:
            where.append("e.ts >= ?")
            args.append(float(f["since"]))
        if f.get("until") is not None:
            where.append("e.ts <= ?")
            args.append(float(f["until"]))
        _in("e.kind", f.get("kinds"))
        if f.get("tool"):
            where.append("COALESCE(e.tool_name, '') LIKE ?")
            args.append("%" + str(f["tool"]) + "%")
        if f.get("file"):
            where.append("COALESCE(e.file_path, '') LIKE ?")
            args.append("%" + str(f["file"]) + "%")
        if f.get("human_only"):
            where.append("e.origin = 'human'")
        else:
            _in("e.origin", f.get("origins"))

        clause = (" AND " + " AND ".join(where)) if where else ""
        args.append(limit)
        return self.q(
            """SELECT s.*, e.kind AS _kind, e.origin AS _origin, e.ts AS _ts,
                      e.tool_name AS _tool, e.file_path AS _file,
                      f.sid AS _sid,
                      snippet(event_fts, 0, '>>>', '<<<', '…', 12) AS snippet
               FROM event_fts f
               JOIN events e ON e.id = f.rowid
               JOIN sessions s ON s.id = f.sid
               WHERE event_fts MATCH ?%s
               ORDER BY rank LIMIT ?""" % clause,
            tuple(args),
        )

    def stats(self) -> Dict[str, Any]:
        out = {"sessions": 0, "events": 0, "by_provider": {}}
        for r in self.q(
            "SELECT provider, COUNT(*) n FROM sessions GROUP BY provider"
        ):
            out["by_provider"][r["provider"]] = r["n"]
            out["sessions"] += r["n"]
        out["events"] = self.q("SELECT COUNT(*) n FROM events")[0]["n"]
        return out

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def sha256_of(path: Path, limit: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(limit))
    return h.hexdigest()
