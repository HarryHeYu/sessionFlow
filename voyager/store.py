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
    raw_metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_provider ON sessions(provider);
CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repo_root);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd ON sessions(cwd);

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
    raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_sid ON events(sid);
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
    PRIMARY KEY (provider, path, sid)
);

CREATE TABLE IF NOT EXISTS thread_pending (
    thread_id  TEXT NOT NULL,
    provider   TEXT NOT NULL,
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
            for col, decl in (("repo_root", "TEXT"), ("cwd", "TEXT"),
                              ("source_provider", "TEXT"),
                              ("source_session", "TEXT"), ("goal", "TEXT"),
                              ("lease_token", "TEXT"), ("launch_cmd", "TEXT"),
                              ("pid", "INTEGER"), ("status", "TEXT DEFAULT 'open'"),
                              ("resolved_at", "REAL"), ("resolved_sid", "TEXT")):
                if col not in pend_cols:
                    self.con.execute(
                        f"ALTER TABLE thread_pending ADD COLUMN {col} {decl}")
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
        self.con.execute("BEGIN")
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
                       metadata_json, raw_metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                ),
            )
            for ev in evs:
                cur = self.con.execute(
                    """INSERT INTO events(
                           sid, ts, seq, kind, role, content,
                           tool_name, tool_call_id, tool_input, tool_output,
                           command, stdout, stderr, exit_code,
                           file_path, old_content, new_content, diff,
                           files_json, model, usage_json, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            self.con.execute(
                "INSERT OR REPLACE INTO sources(provider, path, sid, mtime, size) VALUES (?,?,?,?,?)",
                (provider, str(source_path), sid, mtime, size),
            )
            for p in extra_sources or []:
                try:
                    m2, s2 = self.source_fingerprint(p)
                    self.con.execute(
                        "INSERT OR REPLACE INTO sources(provider, path, sid, mtime, size) VALUES (?,?,?,?,?)",
                        (provider, str(p), sid, m2, s2),
                    )
                except OSError:
                    pass
            self.con.execute("COMMIT")
        except Exception:
            try:
                self.con.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # COMMIT itself failed: sqlite already rolled back
            raise

    def prune_missing_sessions(self, provider: str, disk_paths: set) -> int:
        """Drop sessions of `provider` whose source files all vanished.

        Only sessions that HAVE source rows are managed here. Sessions
        seeded without a source row (synthetic tests, manual inserts) are
        never touched — the caller cannot know their lifecycle.
        """
        rows = self.con.execute(
            "SELECT id FROM sessions WHERE provider=?", (provider,)
        ).fetchall()
        gone = []
        for r in rows:
            sid = r["id"]
            src_rows = self.q(
                "SELECT path FROM sources WHERE provider=? AND sid=?",
                (provider, sid),
            )
            if not src_rows:
                continue  # manually seeded; not ours to prune
            if any(sr["path"] in disk_paths for sr in src_rows):
                continue  # at least one source still on disk
            gone.append(sid)
        for sid in gone:
            self.con.execute(
                "DELETE FROM event_fts WHERE rowid IN (SELECT id FROM events WHERE sid=?)",
                (sid,),
            )
            self.con.execute("DELETE FROM events WHERE sid=?", (sid,))
            self.con.execute("DELETE FROM files WHERE sid=?", (sid,))
            self.con.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self.con.execute("DELETE FROM sources WHERE provider=? AND sid=?", (provider, sid))
        self.con.commit()
        return len(gone)

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
        self.con.execute("UPDATE threads SET status=?, updated_at=? WHERE id=?",
                         (status, _time.time(), tid))
        self.con.commit()

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

    PENDING_COLUMNS = ("thread_id", "provider", "note", "created_at",
                       "repo_root", "cwd", "source_provider",
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

    def search(self, query: str, limit: int = 50) -> List[sqlite3.Row]:
        """Substring search over every event body.

        The user's text is always passed as ONE quoted FTS5 phrase: queries
        like `pytest -q`, `a:b` or `"unbalanced` are ordinary text to a human
        but operators/syntax errors to FTS5, and this is a substring search,
        not a query language.
        """
        phrase = '"' + (query or "").replace('"', '""') + '"'
        return self.q(
            """SELECT s.*, f.sid AS _sid, snippet(event_fts, 0, '>>>', '<<<', '…', 12) AS snippet
               FROM event_fts f
               JOIN sessions s ON s.id = f.sid
               WHERE event_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (phrase, limit),
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
