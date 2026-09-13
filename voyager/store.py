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

CREATE TABLE IF NOT EXISTS sources (
    provider TEXT NOT NULL,
    path     TEXT NOT NULL,
    mtime    REAL,
    size     INTEGER,
    sid      TEXT,
    PRIMARY KEY (provider, path)
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
        mtime, size = self.source_fingerprint(path)
        row = self.con.execute(
            "SELECT mtime, size FROM sources WHERE provider=? AND path=?",
            (provider, str(path)),
        ).fetchone()
        return row is None or row["mtime"] != mtime or row["size"] != size

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
                "INSERT OR REPLACE INTO sources(provider, path, mtime, size, sid) VALUES (?,?,?,?,?)",
                (provider, str(source_path), mtime, size, sid),
            )
            for p in extra_sources or []:
                try:
                    m2, s2 = self.source_fingerprint(p)
                    self.con.execute(
                        "INSERT OR REPLACE INTO sources(provider, path, mtime, size, sid) VALUES (?,?,?,?,?)",
                        (provider, str(p), m2, s2, sid),
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

    def prune_missing_sessions(self, provider: str, live_ids: set) -> int:
        """Remove sessions of a provider whose sources disappeared entirely."""
        rows = self.con.execute(
            "SELECT id FROM sessions WHERE provider=?", (provider,)
        ).fetchall()
        gone = [r["id"] for r in rows if r["id"] not in live_ids]
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
        return self.q(
            """SELECT s.*, f.sid AS _sid, snippet(event_fts, 0, '>>>', '<<<', '…', 12) AS snippet
               FROM event_fts f
               JOIN sessions s ON s.id = f.sid
               WHERE event_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit),
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


def sha256_of(path: Path, limit: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(limit))
    return h.hexdigest()
