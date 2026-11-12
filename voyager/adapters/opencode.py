"""OpenCode adapter — read-only SQLite session indexing.

Source: ``$XDG_DATA_HOME/opencode/opencode.db`` (or
``~/.local/share/opencode/opencode.db``), overridden by ``OPENCODE_DB`` and,
for tests, ``VOYAGER_OPENCODE_DB``.  One database holds many sessions, so this
adapter overrides ``scan()`` rather than ``parse()`` — the same shape as ZCode.

What was verified upstream (anomalyco/opencode, release v2.2.6 2026-11-06;
``packages/core/src/session/sql.ts`` is byte-identical to v1.19.5):

* The current tables are ``session_v2``, ``session_message``, ``session_part``
  and ``project``.  ``session_part`` has ``session_id``, ``ordinal``, ``type``
  and ``call_id`` columns — the design note in docs/audits/ only listed the
  (message_id, ordinal) primary key.
* Timestamps are ``Date.now()`` milliseconds (``database/schema.sql.ts``).
* A tool call and its result live in ONE part: ``data.state`` carries
  ``status`` (streaming/running/completed/error), ``input``, ``content`` (an
  array of ``{type:"text",text}`` / ``{type:"file",...}``) and ``error``.
  They are not separate rows, so joining by ``call_id`` is not how results
  are found here.  ``call_id`` is still recorded as the tool_call_id.
* Pre-v2 databases use ``session`` / ``message`` / ``part`` with a different
  column set (``packages/core/test/v1-migration.test.ts``).  All shapes are
  probed; none is assumed.
* **An intermediate v2.0.x shape has no ``session_part`` table at all.** Message
  content is stored *inline* in ``session_message.data``: ``user`` rows carry a
  ``text`` string; ``assistant`` rows carry a ``content`` array whose items are
  ``text`` / ``reasoning`` / ``tool`` parts, and a tool's exit code sits in
  ``state.metadata.exit`` rather than ``state.exit``.  This shape is detected
  structurally (``session_v2`` + ``session_message`` **and no** ``session_part``),
  never by a product version string.  It was found on a real install, so it is
  not a hypothetical variant.

What this adapter does NOT do:

* No native resume. ``can_resume = False`` and the capability matrix says
  NOT_FOUND. Reading someone's history is not the same as being able to hand
  an agent back its own session.
* No writes to the provider database. Every connection is ``mode=ro`` with
  ``PRAGMA query_only=ON``; WAL side files are read, never created or removed.
* No guessing. An unknown schema, a corrupt file or a locked database is
  reported by name; a missing tool result is left NULL rather than invented.

Verification level: UNIT_VERIFIED on synthetic fixtures for all three shapes,
plus a **real-install read** of a v2.0.x database (discovery, message/role
mapping, inline tool calls, timestamps) recorded in the Phase 3 gate report.
Native resume and cross-agent continuation are still ``NOT_FOUND`` /
``NOT_VERIFIED`` and must not be read as verified from discovery succeeding.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from .base import Adapter, finish_session, git_info, register
from ..model import new_event, new_session, text_of

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

#: Test/CI override. ``OPENCODE_DB`` is the upstream variable and can be a
#: filename relative to the data directory OR an absolute path; honouring it
#: here too means an install with a custom database is indexed without config.
_ENV_OVERRIDES = ("VOYAGER_OPENCODE_DB", "OPENCODE_DB")
_DB_NAME = "opencode.db"

#: Side files that must take part in the freshness fingerprint. In WAL mode
#: writes land in ``-wal`` and the main database's mtime/size may not change,
#: so a source_changed() check on the .db alone would silently skip new turns.
_SIDE_SUFFIXES = ("-wal", "-shm")


def _resolve_home(home: Optional[Path] = None) -> Path:
    """Resolve HOME at call time (not import time) so tests can redirect it."""
    if home is not None:
        return Path(home)
    for name in ("HOME", "USERPROFILE"):
        value = os.environ.get(name)
        if value:
            return Path(value)
    return Path.home()


def data_dir(home: Optional[Path] = None) -> Path:
    """The directory OpenCode resolves its database in.

    Upstream ``packages/util/src/global-roots.ts`` uses XDG_DATA_HOME, else
    ``$HOME/.local/share`` — on every platform, Windows included. There is no
    APPDATA special case, so we do not invent one.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "opencode"
    return _resolve_home(home) / ".local" / "share" / "opencode"


def _candidate_dbs(home: Optional[Path] = None) -> List[Path]:
    """Ordered, deduplicated candidate paths. Never creates anything.

    An explicit override (``VOYAGER_OPENCODE_DB`` / upstream ``OPENCODE_DB``)
    is **authoritative**: when one is set we return only the named database(s)
    and do not probe the default data directory or channel builds. That keeps
    an operator-named file from causing an unrequested read of a real HOME
    database — which is exactly what a test or CI run that sets an override
    must never do. With no override, the default database plus any
    ``opencode-<channel>.db`` beside it are candidates.
    """
    out: List[Path] = []

    def add(p: Path) -> None:
        p = Path(p)
        if p not in out:
            out.append(p)

    overridden = False
    for name in _ENV_OVERRIDES:
        value = os.environ.get(name)
        if not value or value == ":memory:":
            # :memory: is a valid upstream setting but has no file to index.
            continue
        p = Path(value).expanduser()
        add(p if p.is_absolute() else data_dir(home) / p)
        overridden = True
    if overridden:
        return out

    d = data_dir(home)
    add(d / _DB_NAME)
    # Channel builds write opencode-<channel>.db. Any such file is a candidate;
    # the schema probe decides whether it is really an OpenCode database.
    try:
        for p in sorted(d.glob("opencode-*.db")):
            # skip WAL/SHM companions: glob("...*.db") cannot match them, but
            # a file literally named opencode-wal.db would be a real database.
            add(p)
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# Schema probing
# ---------------------------------------------------------------------------

#: (table -> required columns). Three shapes: the current v2 schema (parts in a
#: separate table), the intermediate v2.0.x schema (parts inline in
#: ``session_message.data``), and the pre-v2 one that migration.ts still
#: recognises by the name "session".
SCHEMAS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "v2": {
        "session_v2": ("id", "project_id", "directory", "title",
                       "time_created", "time_updated"),
        "session_message": ("id", "session_id", "type", "seq",
                            "time_created", "data"),
        "session_part": ("message_id", "session_id", "ordinal", "type",
                         "call_id", "data"),
    },
    # v2.0.x: identical message table, but the parts live inline in `data` and
    # there is no session_part table. Required columns are the same as v2 minus
    # the parts table; `SCHEMA_FORBID` below keeps the two apart.
    "v2-inline": {
        "session_v2": ("id", "project_id", "directory", "title",
                       "time_created", "time_updated"),
        "session_message": ("id", "session_id", "type", "seq",
                            "time_created", "data"),
    },
    "legacy": {
        "session": ("id", "project_id", "directory", "title",
                    "time_created", "time_updated"),
        "message": ("id", "session_id", "time_created", "data"),
        "part": ("message_id", "session_id", "time_created", "data"),
    },
}

#: Tables whose *absence* is part of a shape's definition. Checked after the
#: required-column match, so a database that does carry `session_part` is never
#: misread as the inline shape. This is a structural discriminator, not a
#: version check: a v2.2.x database with the parts table matches "v2" first.
SCHEMA_FORBID: Dict[str, Tuple[str, ...]] = {
    "v2-inline": ("session_part",),
}

PROBE_ABSENT = "absent"
PROBE_OK = "ok"
PROBE_UNREADABLE = "unreadable"
PROBE_UNKNOWN_SCHEMA = "unknown-schema"


def _open_ro(path: Path) -> Optional[sqlite3.Connection]:
    """Read-only connection. Never ``immutable=1``: WAL means a live writer."""
    if not path.is_file():
        return None
    try:
        uri = "file:///" + quote(str(path).replace("\\", "/")) + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
        con.execute("PRAGMA query_only=ON")
        return con
    except sqlite3.Error:
        return None


def detect_schema(con: sqlite3.Connection) -> Optional[str]:
    """Name the supported schema, or None. Raises sqlite3.Error on a bad file."""
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for name, required in SCHEMAS.items():
        if not set(required).issubset(tables):
            continue
        # A shape may require the *absence* of a table (v2-inline has no
        # session_part). Structural, not version-based.
        if set(SCHEMA_FORBID.get(name, ())) & tables:
            continue
        ok = True
        for table, cols in required.items():
            present = {r[1] for r in con.execute(
                "PRAGMA table_info(%s)" % table)}
            if not set(cols).issubset(present):
                ok = False
                break
        if ok:
            return name
    return None


def probe(path: Path) -> Tuple[str, Optional[str]]:
    """(verdict, schema-name-or-None). Never raises.

    ``unreadable`` and ``unknown-schema`` are both "present on disk": treating
    either as absent would retire a history that has not gone anywhere (O2).
    """
    if not path.is_file():
        return PROBE_ABSENT, None
    con = _open_ro(path)
    if con is None:
        return PROBE_UNREADABLE, None
    try:
        # A real read, not just sqlite_master: a truncated/corrupt file can
        # answer the schema query and fail on the first table scan.
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        shape = detect_schema(con)
        if shape in ("v2", "v2-inline"):
            con.execute("SELECT count(*) FROM session_v2").fetchone()
        elif shape == "legacy":
            con.execute("SELECT count(*) FROM session").fetchone()
        else:
            return PROBE_UNKNOWN_SCHEMA, None
        return PROBE_OK, shape
    except sqlite3.Error:
        return PROBE_UNREADABLE, None
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def _side_files(db: Path) -> List[Path]:
    out = []
    for suffix in _SIDE_SUFFIXES:
        p = Path(str(db) + suffix)
        if p.is_file():
            out.append(p)
    return out


def _is_main_db(p: Path) -> bool:
    name = p.name
    return name.endswith(".db") and not name.endswith("-wal") and not name.endswith("-shm")


# ---------------------------------------------------------------------------
# JSON helpers (provider data is untrusted input)
# ---------------------------------------------------------------------------

def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ms(value: Any) -> Optional[float]:
    """Milliseconds -> seconds. Rejects nonsense rather than inventing a date."""
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n / 1000.0 if n > 0 else None


def _content_text(content: Any) -> Optional[str]:
    """Tool state content is an array of {type:text,text} / {type:file,...}."""
    if content is None:
        return None
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return text_of(content) or None
    parts: List[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif block.get("uri"):
                parts.append(str(block["uri"]))
    return "\n".join(p for p in parts if p) or None


def _inline_parts(mdata: Dict[str, Any]) -> List[dict]:
    """Parts stored inline in a v2.0.x assistant message's ``data.content``.

    Each item is already shaped like a part (``type`` plus ``text`` or
    ``state``). Only dict items are kept; anything else is dropped rather than
    guessed at. Array order is the upstream write order, which is the stable
    order for these parts.
    """
    content = mdata.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _tool_exit(state: Dict[str, Any]) -> Optional[int]:
    """A tool exit code, wherever the build put it. ``0`` is a real code."""
    candidates = (state.get("exit"), state.get("exit_code"),
                  _as_dict(state.get("metadata")).get("exit"))
    for value in candidates:
        code = _exit_code(value)
        if code is not None:
            return code
    return None


def _command_and_path(tool_input: Any) -> Tuple[Optional[str], Optional[str]]:
    d = _as_dict(tool_input)
    command = None
    for key in ("command", "cmd", "script"):
        if isinstance(d.get(key), str):
            command = d[key]
            break
    path = None
    for key in ("file_path", "filePath", "path", "filename"):
        if isinstance(d.get(key), str):
            path = d[key]
            break
    return command, path


def _model_label(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value or None
    d = _as_dict(value)
    mid = d.get("id")
    if not mid:
        return None
    provider = d.get("providerID") or d.get("providerId")
    return "%s:%s" % (provider, mid) if provider else str(mid)


def _exit_code(value: Any) -> Optional[int]:
    """Only an explicit integer is an exit code. Never infer one from text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class OpencodeAdapter(Adapter):
    provider = "opencode"

    # Reading a session is not resuming one. No resume command, hook, or
    # transcript writer is wired for this provider; do not claim otherwise.
    can_resume = False
    can_fork = False

    def discover(self) -> List[Path]:
        """Main databases plus their WAL companions.

        WAL paths are part of the returned source set ON PURPOSE: the scanner
        calls ``source_changed(provider, path)`` for every discovered source,
        so a write that only touched ``-wal`` still triggers a rescan. Without
        this, an active OpenCode session would look unchanged until a
        checkpoint rewrote the main file.
        """
        out: List[Path] = []
        for db in _candidate_dbs():
            verdict, _ = probe(db)
            if verdict != PROBE_OK:
                continue
            out.append(db)
            out.extend(_side_files(db))
        # Main databases first: scanner uses sources[0] as the fallback anchor.
        return out

    def unusable_sources(self) -> List[Path]:
        """Present on disk but not indexable this round.

        Keeping these out of `discover()` is not enough — the scanner must be
        told they still exist, or it would mark their sessions SOURCE_MISSING.
        """
        out: List[Path] = []
        for db in _candidate_dbs():
            if not _is_main_db(db):
                continue
            verdict, _ = probe(db)
            if verdict in (PROBE_UNREADABLE, PROBE_UNKNOWN_SCHEMA):
                out.append(db)
                out.extend(_side_files(db))
        return out

    def unrecognised_sources(self) -> List[Path]:
        """OpenCode-shaped databases we could not read or classify.

        A warning, never an attempt to parse. This is the "renamed artifact"
        guard that made the DSH v4 bug silent for weeks.
        """
        out: List[Path] = []
        for db in _candidate_dbs():
            if not _is_main_db(db):
                continue
            verdict, _ = probe(db)
            if verdict == PROBE_UNKNOWN_SCHEMA:
                out.append(db)
        return out

    def parse(self, source: Path) -> Optional[dict]:
        # One database -> many sessions; the base scanner expects one session
        # per artifact, so scan() is the entry point (same as ZCode).
        raise NotImplementedError

    # -- one database -----------------------------------------------------

    def _scan_db(self, db: Path, emit=None) -> List[dict]:
        verdict, shape = probe(db)
        if verdict != PROBE_OK or shape is None:
            raise ValueError("opencode: %s is %s (%s)" % (db.name, verdict, db))
        con = _open_ro(db)
        if con is None:
            raise ValueError("opencode: cannot open %s read-only" % db)
        bundles: List[dict] = []
        try:
            projects = {}
            try:
                for pid, worktree in con.execute(
                        "SELECT id, worktree FROM project"):
                    projects[pid] = worktree
            except sqlite3.Error:
                pass                    # project table is optional for us

            session_table = ("session_v2" if shape in ("v2", "v2-inline")
                             else "session")
            rows = con.execute(
                "SELECT * FROM %s ORDER BY time_created, id" % session_table
            ).fetchall()
            # row_factory is not set globally (probes use plain tuples), so
            # describe the columns explicitly for the selected shape.
            cols = [r[1] for r in con.execute(
                "PRAGMA table_info(%s)" % session_table)]
            for raw in rows:
                s = dict(zip(cols, raw))
                bundle = self._session_bundle(con, db, shape, s, projects)
                if bundle is None:
                    continue
                if emit is not None:
                    emit(bundle)
                else:
                    bundles.append(bundle)
            return bundles
        finally:
            try:
                con.close()
            except sqlite3.Error:
                pass

    def _session_bundle(self, con, db, shape, s, projects) -> Optional[dict]:
        native = s.get("id")
        if not native:
            return None
        sid = "opencode:%s" % native
        directory = s.get("directory")
        project_id = s.get("project_id")
        cwd = directory
        repo_root = projects.get(project_id)
        title = s.get("title")
        model = _model_label(_json(s.get("model")) or s.get("model"))
        parent_id = s.get("parent_id")

        events: List[dict] = []
        seq = 0
        first_user: Optional[str] = None

        def ev(**kw) -> dict:
            nonlocal seq
            seq += 1
            kw.setdefault("ts", None)
            e = new_event(sid=sid, seq=seq, raw_event=kw.pop("raw_event", None),
                          **kw)
            events.append(e)
            return e

        msg_rows, msg_cols = self._messages(con, shape, native)
        for mrow in msg_rows:
            m = dict(zip(msg_cols, mrow))
            mtype = m.get("type") or (_as_dict(_json(m.get("data"))).get("role"))
            mdata = _as_dict(_json(m.get("data")))
            mtime = _ms(m.get("time_created")) or _ms(
                _as_dict(mdata.get("time")).get("created"))
            if not model:
                model = _model_label(mdata.get("model"))
            role = "user" if mtype == "user" else "assistant"

            # The user text lives in the message data in v2; the parts table
            # only holds assistant content. Some writes keep a copy in parts,
            # so read both and de-duplicate by (text, ts) below.
            if mtype == "user":
                text = text_of(mdata.get("text")) if mdata.get("text") else ""
                if not text and shape != "v2-inline":
                    for p in self._parts(con, shape, m.get("id"), native):
                        if p.get("type") == "text" and p.get("text"):
                            text = str(p["text"])
                            break
                if text.strip():
                    if first_user is None:
                        first_user = text
                    ev(kind="user", role="user", content=text, ts=mtime,
                       origin="human" if not parent_id else "provider_synthetic",
                       raw_event={"type": mtype, "text": text})
                continue

            if mtype in ("synthetic", "system", "skill", "compaction", "idle",
                         "agent-switched", "model-switched",
                         "location-switched"):
                # Provider-generated pseudo-user text must not be promoted to a
                # human instruction (provenance safety: keep it as meta).
                # `summary` is what a v2.0.x compaction row carries; using it
                # only when present keeps this honest (no invented text).
                text = text_of(mdata.get("text") or mdata.get("description")
                               or mdata.get("summary"))
                if text:
                    ev(kind="meta", role="system", content=text, ts=mtime,
                       origin="provider_system",
                       raw_event={"type": mtype, "text": text})
                continue

            if mtype == "shell":
                d = mdata
                cmd = d.get("command")
                out = _content_text(d.get("output"))
                ev(kind="tool_call", role="assistant", ts=mtime,
                   tool_name="shell", tool_call_id=d.get("shellID"),
                   command=cmd, tool_output=out,
                   exit_code=_exit_code(d.get("exit")),
                   origin="provider_system",
                   raw_event={"type": "shell", "command": cmd,
                              "exit": d.get("exit")})
                continue

            if mtype == "assistant" or mtype is None or shape == "legacy":
                if shape == "v2-inline":
                    # v2.0.x has no session_part table: the parts array lives
                    # inline in `data.content` (text / reasoning / tool items).
                    parts = _inline_parts(mdata)
                else:
                    parts = self._parts(con, shape, m.get("id"), native)
                before = len(events)
                for p in parts:
                    ptype = p.get("type")
                    ptime = _ms(_as_dict(p.get("time")).get("created")) or mtime
                    if ptype == "text" and p.get("text"):
                        ev(kind="assistant", role="assistant",
                           content=str(p["text"]), ts=ptime, model=model,
                           origin="provider_system",
                           raw_event={"type": "assistant", "part": "text"})
                    elif ptype == "reasoning" and p.get("text"):
                        ev(kind="reasoning", role="assistant",
                           content=str(p["text"]), ts=ptime, model=model,
                           origin="provider_system",
                           raw_event={"type": "assistant", "part": "reasoning"})
                    elif ptype == "tool":
                        self._tool_events(ev, p, ptime, model, shape)
                    elif ptype == "file":
                        fp = p.get("url") or p.get("filename")
                        if fp:
                            ev(kind="file", role="user", file_path=str(fp),
                               ts=ptime, origin="provider_system",
                               raw_event={"type": "file"})

                # Some builds store assistant content inline in the message
                # data instead of (or in addition to) session_part. Only use
                # it when the parts table gave us nothing for this message —
                # otherwise the same text would be indexed twice. The inline
                # shape already read `data.content` above, so skip it there.
                if (shape != "v2-inline" and len(events) == before
                        and isinstance(mdata.get("content"), list)):
                    for p in mdata["content"]:
                        pd = _as_dict(p)
                        if pd.get("type") == "text" and pd.get("text"):
                            ev(kind="assistant", role="assistant",
                               content=str(pd["text"]), ts=mtime, model=model,
                               origin="provider_system",
                               raw_event={"type": "assistant",
                                          "part": "inline-text"})

        if not events and not title:
            return None

        # Session-level token totals are a property of the session, not of any
        # single event; do not attribute them to one message.
        tokens = {k: s.get("tokens_%s" % k) for k in
                  ("input", "output", "reasoning", "cache_read", "cache_write")}
        usage = {k: v for k, v in tokens.items() if v} or None
        session = new_session(
            id=sid, provider=self.provider, native_session_id=native,
            title=title or (first_user and " ".join(first_user.split())[:120])
                  or str(native),
            started_at=_ms(s.get("time_created")),
            updated_at=_ms(s.get("time_updated")),
            cwd=cwd, repo_root=repo_root, model=model,
            message_count=sum(1 for e in events
                              if e["kind"] in ("user", "assistant")),
            tool_count=sum(1 for e in events if e["kind"] == "tool_call"),
            can_resume=False, can_fork=False, resume_cmd=None,
            metadata={"schema": shape, "usage_totals": usage,
                      "project_id": project_id},
            raw_metadata={"parent_id": parent_id},
        )
        session = finish_session(session, git_info(cwd))
        return {"session": session, "events": events,
                "source_path": db, "extra_sources": _side_files(db)}

    def _messages(self, con, shape, native):
        if shape in ("v2", "v2-inline"):
            sql = ("SELECT * FROM session_message WHERE session_id=? "
                   "ORDER BY seq, time_created, id")
        else:
            sql = ("SELECT * FROM message WHERE session_id=? "
                   "ORDER BY time_created, id")
        cur = con.execute(sql, (native,))
        cols = [d[0] for d in cur.description]
        return cur.fetchall(), cols

    def _parts(self, con, shape, message_id, native) -> List[dict]:
        if not message_id:
            return []
        try:
            if shape == "v2":
                cur = con.execute(
                    "SELECT data, call_id FROM session_part "
                    "WHERE message_id=? ORDER BY ordinal, time_created",
                    (message_id,))
            else:
                cur = con.execute(
                    "SELECT data, NULL FROM part "
                    "WHERE message_id=? ORDER BY time_created, id",
                    (message_id,))
            out = []
            for data, call_id in cur:
                p = _as_dict(_json(data))
                if not p:
                    continue
                if call_id and not p.get("call_id"):
                    p["call_id"] = call_id
                out.append(p)
            return out
        except sqlite3.Error:
            return []

    def _tool_events(self, ev, p, ptime, model, shape) -> None:
        state = _as_dict(p.get("state"))
        inp = state.get("input")
        cmd, fp = _command_and_path(inp)
        # v2 part data uses name/id; legacy uses tool/callID.
        tool_name = p.get("name") or p.get("tool")
        call_id = p.get("call_id") or p.get("callID") or p.get("id")
        status = state.get("status")
        raw = {"type": "tool", "status": status, "tool": tool_name}
        ev(kind="tool_call", role="assistant", ts=ptime, model=model,
           tool_name=str(tool_name) if tool_name else None,
           tool_call_id=str(call_id) if call_id else None,
           tool_input=(json.dumps(inp, ensure_ascii=False, default=str)
                       if inp is not None else None),
           command=cmd, file_path=fp,
           exit_code=_tool_exit(state),
           origin="provider_system", raw_event=raw)

        # The result is in the SAME part's state, not a separate row. Emit a
        # tool_result only when upstream actually recorded one; a running or
        # streaming tool has no result and must not be given a fake one.
        output = _content_text(state.get("content") or state.get("output"))
        error = state.get("error")
        if status == "completed" and output:
            ev(kind="tool_result", role="tool", ts=ptime,
               tool_call_id=str(call_id) if call_id else None,
               tool_output=output, origin="provider_system",
               raw_event={"type": "tool", "status": status,
                          "result": True})
        elif status == "error" or error:
            detail = _as_dict(error).get("message") or (
                error if isinstance(error, str) else None) or output
            if detail:
                ev(kind="tool_result", role="tool", ts=ptime,
                   tool_call_id=str(call_id) if call_id else None,
                   tool_output=str(detail), origin="provider_system",
                   raw_event={"type": "tool", "status": status,
                              "result": True, "error": True})

    # -- scan --------------------------------------------------------------

    def scan(self, source_changed, emit=None) -> List[dict]:
        """Scan every readable database, optionally streaming each bundle.

        ``emit`` is the additive streaming extension; with ``emit=None`` the
        return value is the complete list, so the two styles index identically.
        """
        out: List[dict] = []
        # discover() deliberately returns only usable databases, so a broken
        # one would otherwise be invisible here. Name it loudly: an empty index
        # must never look like "the provider has no sessions".
        for broken in self.unusable_sources():
            if _is_main_db(broken):
                verdict, _ = probe(broken)
                print("  ! opencode: %s is %s; keeping existing index"
                      % (broken.name, verdict), file=sys.stderr)
        for p in self.discover():
            if not _is_main_db(p):
                continue                      # WAL/SHM are freshness sources
            try:
                bundles = self._scan_db(p, emit=emit)
            except (sqlite3.Error, ValueError) as e:
                print("  ! opencode: scan of %s failed (%s); keeping "
                      "existing index" % (p.name, e), file=sys.stderr)
                continue
            if emit is None:
                out.extend(bundles)
        return out


register(OpencodeAdapter())
